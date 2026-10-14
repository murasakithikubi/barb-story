"""BAR B「営業中！」ストーリー自動投稿(1ファイル版)。

使い方:
  python barb_story.py create   … 制作〜Metricool予約登録〜登録内容の再取得確認
  python barb_story.py verify   … 公開予定時刻後に実公開を確認し、エラーなら再登録
  python barb_story.py selftest … キーとAPIの疎通確認だけを行う(投稿しない)
"""
import os
from zoneinfo import ZoneInfo
from datetime import datetime
from difflib import SequenceMatcher
import numpy as np
from PIL import Image
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import csv
import io
from datetime import date
import requests
import time
from datetime import datetime, timedelta
import base64
import json
import subprocess
from pathlib import Path
import hashlib
import sys
import traceback

# ======================================================================
# BAR B 営業告知ストーリー 自動投稿の設定値。
# 必須表示の文字列はここだけで定義し、画像合成と最終照合の両方がこの値を使う。
# 

JST = ZoneInfo("Asia/Tokyo")
TIMEZONE_NAME = "Asia/Tokyo"

# ---- 必須表示(完全一致) ----
OPEN_TEXT = "営業中！"
HOURS_TEXT = "営業時間 21:00〜06:00"
SERVICE_ITEMS = [
    "豊富なドリンクメニュー",
    "二次会・貸切大歓迎",
    "関内駅からアクセス良好",
    "幹事様特典有り",
]
WEEKDAYS_JA = ["月", "火", "水", "木", "金", "土", "日"]


def date_text(d) -> str:
    """その日の営業日の日付。例: 10月14日(水)"""
    return f"{d.month}月{d.day}日({WEEKDAYS_JA[d.weekday()]})"


# ---- 一言コピーの制約 ----
COPY_MIN_LEN = 6
COPY_MAX_LEN = 22

# ---- 投稿スケジュール ----
PUBLISH_HOUR = 15
PUBLISH_MINUTE = 0
LATE_MARGIN_MIN = 10  # 予定時刻を過ぎて実行された場合、今から何分後に公開するか

# ---- 類似チェック ----
HISTORY_START = datetime(2026, 9, 14, 0, 0, tzinfo=JST)  # この日時以降の営業中ストーリーと比較
PHASH_MIN_DISTANCE = 14       # 背景同士のpHash距離がこれ未満なら類似(64bit中)
COLOR_HIST_MAX_SIM = 0.92     # 色ヒストグラムの類似度がこれ以上なら類似
COPY_MAX_RATIO = 0.6          # 一言コピーの文字列類似度がこれ以上なら類似
THEME_MAX_RATIO = 0.7         # テーマ文の類似度がこれ以上なら類似
MAX_ATTEMPTS = 6              # 1回の実行で画像を作り直す最大回数(Cloudflare無料枠内)

# ---- 品質 ----
QUALITY_MIN_SCORE = 7         # Geminiの品質採点(10点満点)の合格ライン
TEXT_MIN_CONTRAST = 4.5       # 文字と背景のコントラスト比の下限(WCAG AA)

# ---- 画像 ----
STORY_W, STORY_H = 1080, 1920
GEN_W, GEN_H = 768, 1344      # 生成サイズ(約1MP: Cloudflare無料枠で1日6回以上作れる)
CF_MODEL = "@cf/black-forest-labs/flux-2-klein-9b"
FONT_BOLD = os.environ.get("FONT_BOLD", "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")
FONT_BLACK = os.environ.get("FONT_BLACK", "/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc")
FONT_INDEX_JP = 0  # ttc内のJapaneseフェイス番号(Noto CJK: 0=JP)

# ---- 外部サービス ----
METRICOOL_BASE = "https://app.metricool.com/api"
METRICOOL_BLOG_ID = os.environ.get("METRICOOL_BLOG_ID") or "6954500"
METRICOOL_USER_ID = os.environ.get("METRICOOL_USER_ID") or "5363321"
METRICOOL_TOKEN = os.environ.get("METRICOOL_TOKEN", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_API_TOKEN = os.environ.get("CF_API_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-2.5-flash"

# ---- 保存先(GitHubリポジトリ) ----
REPO = os.environ.get("GITHUB_REPOSITORY", "")       # owner/repo
BRANCH = os.environ.get("GITHUB_REF_NAME") or "main"
DATA_DIR = os.environ.get("DATA_DIR", "data")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "media")

# ======================================================================
# 画像・文章の類似度をプログラムで数値判定する(AIの自己申告に頼らない部分)。


def _dct_matrix(n):
    k = np.arange(n)
    m = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2 * n)) * np.sqrt(2 / n)
    m[0] /= np.sqrt(2)
    return m


_D32 = _dct_matrix(32)


def phash(img: Image.Image) -> str:
    """64bitの知覚ハッシュ(16進)。構図・明暗の配置が似ていると距離が小さくなる。"""
    g = np.asarray(img.convert("L").resize((32, 32), Image.LANCZOS), dtype=np.float64)
    d = _D32 @ g @ _D32.T
    low = d[:8, :8].flatten()
    med = np.median(low[1:])
    bits = (low > med).astype(int)
    return "".join(f"{int(''.join(map(str, bits[i:i + 4])), 2):x}" for i in range(0, 64, 4))


def phash_distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def color_hist(img: Image.Image) -> list:
    """HSVの色ヒストグラム(色相12×彩度3×明度3=108次元、正規化済み)。"""
    hsv = np.asarray(img.convert("RGB").resize((128, 224)).convert("HSV"), dtype=np.int32)
    h = hsv[..., 0] * 12 // 256
    s = hsv[..., 1] * 3 // 256
    v = hsv[..., 2] * 3 // 256
    idx = (h * 9 + s * 3 + v).flatten()
    hist = np.bincount(idx, minlength=108).astype(np.float64)
    hist /= hist.sum()
    return [round(float(x), 5) for x in hist]


def hist_similarity(a: list, b: list) -> float:
    """ヒストグラム交差(0〜1。1で完全一致)。"""
    return float(np.minimum(np.array(a), np.array(b)).sum())


def central_region(img: Image.Image) -> Image.Image:
    """ストーリー画像のうち文字が乗らない中央部分(写真部分)を切り出す。
    レイアウトの違う過去画像とも、写真同士で比べるために使う。"""
    w, h = img.size
    return img.crop((0, int(h * 0.40), w, int(h * 0.70)))


def text_ratio(a: str, b: str) -> float:
    norm = lambda s: "".join(ch for ch in s if ch.isalnum())
    return SequenceMatcher(None, norm(a), norm(b)).ratio()


def fingerprint(img: Image.Image) -> dict:
    c = central_region(img)
    return {"phash": phash(img), "phash_center": phash(c), "hist": color_hist(img), "hist_center": color_hist(c)}

# ======================================================================
# 背景画像に必須表示を重ねてストーリー画像(1080x1920)を作る。
# 文字は画像生成AIに描かせず、ここで設定値の文字列をそのまま描画する。
# 戻り値には「実際に描画した文字列」と各文字のコントラスト比を含め、最終照合に使う。
# 



def _font(path, size):
    return ImageFont.truetype(path, size=size, index=FONT_INDEX_JP)


def _cover(img: Image.Image, w: int, h: int) -> Image.Image:
    """縦横比を保って全面を覆うように拡大し、中央を切り出す。"""
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    nw, nh = round(img.width * scale), round(img.height * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _vertical_shade(size, top_alpha, bottom_alpha, y0, y1):
    """y0〜y1で透明度が変わる黒のグラデーション層。"""
    w, h = size
    a = np.zeros((h, w), dtype=np.float32)
    ys = np.arange(h, dtype=np.float32)
    t = np.clip((ys - y0) / max(1, (y1 - y0)), 0, 1)
    col = top_alpha + (bottom_alpha - top_alpha) * t
    col[ys < y0] = top_alpha
    col[ys > y1] = bottom_alpha
    a[:] = col[:, None]
    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    layer.putalpha(Image.fromarray(a.astype(np.uint8)))
    return layer


def _luminance(rgb):
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def _contrast(text_rgb, region: Image.Image) -> float:
    """文字色と、文字の背後の領域(明るい側95パーセンタイル)とのコントラスト比。"""
    lt = float(_luminance(np.array(text_rgb)))
    lb = float(np.percentile(_luminance(np.array(region.convert("RGB"))), 95))
    hi, lo = max(lt, lb), min(lt, lb)
    return (hi + 0.05) / (lo + 0.05)


def _draw_centered(draw, canvas, y, text, font, fill, shadow=True):
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (canvas.width - tw) // 2 - bbox[0]
    box = (max(0, x + bbox[0] - 6), max(0, y - 6), min(canvas.width, x + bbox[2] + 6), min(canvas.height, y + th + bbox[1] + 6))
    region = canvas.crop(box)  # 描画前の背景で判定する
    if shadow:
        sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
        ImageDraw.Draw(sh).text((x + 3, y + 4), text, font=font, fill=(0, 0, 0, 200))
        sh = sh.filter(ImageFilter.GaussianBlur(4))
        canvas.alpha_composite(sh)
        draw = ImageDraw.Draw(canvas)
    draw.text((x, y), text, font=font, fill=fill)
    return {"text": text, "box": box, "height": th, "contrast": round(_contrast(fill[:3], region), 2)}


def compose(background: Image.Image, the_date, one_copy: str) -> tuple[Image.Image, dict]:
    W, H = STORY_W, STORY_H
    canvas = _cover(background, W, H).convert("RGBA")

    # 文字の背後を暗くして可読性を確保(上部と下部のみ。中央は写真を見せる)
    canvas.alpha_composite(_vertical_shade((W, H), 195, 0, 700, 920))
    canvas.alpha_composite(_vertical_shade((W, H), 0, 205, 1080, 1360))

    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)
    gold = (255, 214, 120, 255)

    drawn = []
    # 上部: 営業中! / 日付 / 一言コピー  (上端250pxはInstagramのUIと重なるので避ける)
    drawn.append(_draw_centered(draw, canvas, 270, OPEN_TEXT, _font(FONT_BLACK, 150), gold) | {"role": "open"})
    draw = ImageDraw.Draw(canvas)
    drawn.append(_draw_centered(draw, canvas, 470, date_text(the_date), _font(FONT_BOLD, 66), white) | {"role": "date"})
    draw = ImageDraw.Draw(canvas)
    drawn.append(_draw_centered(draw, canvas, 580, one_copy, _font(FONT_BOLD, 54), white) | {"role": "copy"})
    draw = ImageDraw.Draw(canvas)

    # 下部: 4項目 / 営業時間  (下端250pxは返信欄と重なるので避ける)
    y = 1380
    for item in SERVICE_ITEMS:
        drawn.append(_draw_centered(draw, canvas, y, item, _font(FONT_BOLD, 46), white) | {"role": "item"})
        draw = ImageDraw.Draw(canvas)
        y += 66
    drawn.append(_draw_centered(draw, canvas, y + 14, HOURS_TEXT, _font(FONT_BOLD, 52), gold) | {"role": "hours"})

    report = {"drawn": drawn, "size": [W, H]}
    return canvas.convert("RGB"), report


def check_composition(report: dict, the_date, one_copy: str) -> list[str]:
    """描画結果を必須表示と文字単位で照合する。問題があれば理由のリストを返す(空なら合格)。"""
    errors = []
    texts = {d["role"]: [] for d in report["drawn"]}
    for d in report["drawn"]:
        texts[d["role"]].append(d["text"])

    if texts.get("open") != [OPEN_TEXT]:
        errors.append(f"営業中表示が不一致: {texts.get('open')}")
    if texts.get("date") != [date_text(the_date)]:
        errors.append(f"日付が不一致: {texts.get('date')} / 期待値 {date_text(the_date)}")
    if texts.get("hours") != [HOURS_TEXT]:
        errors.append(f"営業時間が不一致: {texts.get('hours')}")
    if texts.get("item") != SERVICE_ITEMS:
        errors.append(f"4項目が不一致: {texts.get('item')}")
    if texts.get("copy") != [one_copy] or not one_copy.strip():
        errors.append(f"一言コピーが不一致または空: {texts.get('copy')}")

    W = report["size"][0]
    for d in report["drawn"]:
        x0, _, x1, _ = d["box"]
        if x0 <= 20 or x1 >= W - 20:
            errors.append(f"文字が画面端からはみ出し: {d['text']}")
        if d["contrast"] < TEXT_MIN_CONTRAST:
            errors.append(f"文字のコントラスト不足({d['contrast']}): {d['text']}")
    return errors

# ======================================================================
# 営業日判定(日本時間)。日曜・日本の祝日(振替休日・国民の休日を含む)は投稿しない。
# 祝日は2つの情報源で確認する: 内閣府の祝日CSV(公的情報) と holidays ライブラリ。
# どちらかが祝日と判定すれば祝日扱い。両方とも確認できなければ「確認不能」として投稿しない。
# 

CAO_CSV = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"


def _cao_holidays():
    r = requests.get(CAO_CSV, timeout=30)
    r.raise_for_status()
    text = r.content.decode("cp932")
    out = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) >= 2 and "/" in row[0]:
            y, m, d = map(int, row[0].split("/"))
            out[date(y, m, d)] = row[1]
    return out


def check(d: date) -> dict:
    """戻り値: {"business_day": bool|None, "reason": str, "sources": {...}}"""
    if d.weekday() == 6:
        return {"business_day": False, "reason": "日曜日", "sources": {}}

    sources, names = {}, []
    try:
        cao = _cao_holidays()
        covered = any(h.year == d.year for h in cao)
        if covered:
            sources["cao"] = cao.get(d) or "平日"
            if d in cao:
                names.append(cao[d])
        else:
            sources["cao"] = "対象年のデータなし"
    except Exception as e:  # noqa: BLE001
        sources["cao"] = f"取得失敗: {e}"

    try:
        import holidays
        jp = holidays.JP(years=d.year)
        sources["holidays_lib"] = jp.get(d) or "平日"
        if d in jp:
            names.append(jp.get(d))
    except Exception as e:  # noqa: BLE001
        sources["holidays_lib"] = f"取得失敗: {e}"

    if names:
        return {"business_day": False, "reason": f"祝日({names[0]})", "sources": sources}
    ok = [k for k, v in sources.items() if v == "平日"]
    if not ok:
        return {"business_day": None, "reason": "祝日を確認できないため投稿しない", "sources": sources}
    return {"business_day": True, "reason": "営業日", "sources": sources}

# ======================================================================
# Metricool API(予約投稿の取得・作成・画像の登録)。



class MetricoolError(RuntimeError):
    pass


def _params(extra=None):
    p = {"blogId": METRICOOL_BLOG_ID, "userId": METRICOOL_USER_ID}
    if extra:
        p.update(extra)
    return p


def _headers():
    if not METRICOOL_TOKEN:
        raise MetricoolError("METRICOOL_TOKEN が設定されていません")
    return {"X-Mc-Auth": METRICOOL_TOKEN, "Content-Type": "application/json"}


def _request(method, path, params=None, json=None, retries=4):
    url = f"{METRICOOL_BASE}{path}"
    last = None
    for i in range(retries):
        try:
            r = requests.request(method, url, params=_params(params), json=json, headers=_headers(), timeout=60)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                time.sleep(5 * (i + 1))
                continue
            if r.status_code >= 400:
                raise MetricoolError(f"{method} {path} → HTTP {r.status_code}: {r.text[:500]}")
            return r.json() if r.text else {}
        except requests.RequestException as e:
            last = str(e)
            time.sleep(5 * (i + 1))
    raise MetricoolError(f"{method} {path} が{retries}回失敗: {last}")


def list_posts(start: datetime, end: datetime) -> list[dict]:
    """期間内の予約・公開済み投稿を取得する。大きい期間は7日ずつ分割して全区間取得する。"""
    out, seen = [], set()
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=7), end)
        data = _request("GET", "/v2/scheduler/posts", params={
            "start": cur.strftime("%Y-%m-%dT%H:%M:%S"),
            "end": nxt.strftime("%Y-%m-%dT%H:%M:%S"),
            "timezone": TIMEZONE_NAME,
            "extendedRange": "false",
        })
        items = data.get("data", data if isinstance(data, list) else [])
        for p in items:
            key = p.get("id") or p.get("uuid")
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        cur = nxt
    return out


def is_instagram_story(p: dict) -> bool:
    nets = {pr.get("network") for pr in p.get("providers", [])}
    return "instagram" in nets and (p.get("instagramData") or {}).get("type") == "STORY"


def provider(p: dict, network="instagram") -> dict:
    for pr in p.get("providers", []):
        if pr.get("network") == network:
            return pr
    return {}


def normalize_image(public_url: str) -> str:
    """公開URLの画像をMetricoolに取り込み、投稿に使えるURLを返す。"""
    data = _request("GET", "/actions/normalize/image/url", params={"url": public_url})
    if isinstance(data, str):
        return data
    for k in ("url", "data", "mediaUrl", "normalizedUrl"):
        v = data.get(k) if isinstance(data, dict) else None
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict) and isinstance(v.get("url"), str):
            return v["url"]
    raise MetricoolError(f"画像の取り込み結果からURLを取得できません: {str(data)[:300]}")


def create_story(media_url: str, publish_at: datetime) -> dict:
    body = {
        "publicationDate": {"dateTime": publish_at.strftime("%Y-%m-%dT%H:%M:00"), "timezone": TIMEZONE_NAME},
        "text": "",
        "providers": [{"network": "instagram"}],
        "media": [media_url],
        "autoPublish": True,
        "draft": False,
        "shortener": False,
        "saveExternalMediaFiles": True,
        "instagramData": {"type": "STORY", "autoPublish": True},
    }
    data = _request("POST", "/v2/scheduler/posts", json=body)
    return data.get("data", data) if isinstance(data, dict) else data


def get_post(post_id) -> dict:
    data = _request("GET", f"/v2/scheduler/posts/{post_id}")
    return data.get("data", data) if isinstance(data, dict) else data

# ======================================================================
# Cloudflare Workers AI(FLUX)で背景画像を生成する。画像内に文字は描かせない。



class CloudflareError(RuntimeError):
    pass


NEGATIVE_HINT = (" No text, no letters, no words, no signage, no logos, no watermark, "
                 "no people's faces in focus.")


def generate(prompt: str, retries=3) -> Image.Image:
    if not (CF_ACCOUNT_ID and CF_API_TOKEN):
        raise CloudflareError("CF_ACCOUNT_ID / CF_API_TOKEN が設定されていません")
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/{CF_MODEL}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    files = {
        "prompt": (None, prompt + NEGATIVE_HINT),
        "width": (None, str(GEN_W)),
        "height": (None, str(GEN_H)),
    }
    last = None
    for i in range(retries):
        try:
            r = requests.post(url, headers=headers, files=files, timeout=180)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(10 * (i + 1))
            continue
        ctype = r.headers.get("content-type", "")
        if r.status_code == 200 and ctype.startswith("image/"):
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        if r.status_code == 200:
            data = r.json()
            img_b64 = (data.get("result") or {}).get("image")
            if img_b64:
                return Image.open(io.BytesIO(base64.b64decode(img_b64))).convert("RGB")
            last = f"画像が含まれない応答: {str(data)[:300]}"
        else:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (401, 403):
                break  # 認証エラーは再試行しても直らない
            if r.status_code == 429 or "neuron" in r.text.lower():
                break  # 無料枠の上限。課金はされず、その日はこれ以上作れない
        time.sleep(10 * (i + 1))
    raise CloudflareError(f"画像生成に失敗: {last}")

# ======================================================================
# Gemini API(テーマ判断・一言コピー作成・画像の判定)。
# 返答はJSON形式で受け取り、必要な項目がそろっているかをプログラムで検査する。
# 



class GeminiError(RuntimeError):
    pass


def _img_part(img: Image.Image, max_side=768):
    im = img.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


def ask_json(prompt: str, images=(), required_keys=(), retries=4) -> dict:
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY が設定されていません")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    parts = [{"text": prompt}] + [_img_part(i) for i in images]
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.9}}
    last = None
    for i in range(retries):
        try:
            r = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=120)
        except requests.RequestException as e:
            last = str(e)
            time.sleep(8 * (i + 1))
            continue
        if r.status_code != 200:
            last = f"HTTP {r.status_code}: {r.text[:300]}"
            if r.status_code in (400, 401, 403):
                break
            time.sleep(15 * (i + 1))
            continue
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(text)
        except (KeyError, IndexError, ValueError) as e:
            last = f"JSONとして読めない応答: {e}"
            continue
        missing = [k for k in required_keys if k not in data or data[k] is None or data[k] == ""]
        if missing:
            last = f"必須項目の欠落: {missing}"
            continue  # 抜けがあれば作り直させる
        return data
    raise GeminiError(f"Geminiの応答取得に失敗: {last}")


def plan_story(the_date, past: list[dict], avoid_notes: list[str]) -> dict:
    """その日のテーマ・背景画像の指示・一言コピーを決める。"""
    history = "\n".join(f"- テーマ: {p.get('theme','')} / 一言: {p.get('copy','')} / 画像: {p.get('description','')}"
                        for p in past[-60:]) or "(なし)"
    avoid = "\n".join(f"- {n}" for n in avoid_notes) or "(なし)"
    prompt = f"""あなたは横浜・関内のカラオケバー「BAR B」のInstagramストーリー担当です。
今日({the_date.isoformat()}、{date_text(the_date)})の「営業中！」告知ストーリーの背景画像と一言コピーを考えてください。

条件:
- 背景画像は写真風。バー/カラオケ/お酒/夜の横浜・関内などBAR Bの世界観に合うもの。人物の顔は写さない。
- 画像の上下に文字を重ねるので、画面中央に主役を置き、上下は落ち着いた余白にする。
- 画像内に文字・看板・ロゴを入れない。
- 季節や曜日感を自然に取り入れてよい(今日の日付を参考に)。
- 過去の投稿と、主役被写体・構図・背景・配色・テーマが似ないこと。色や小物だけの変更は不可。
- 一言コピーは日本語で{COPY_MIN_LEN}〜{COPY_MAX_LEN}文字。店名・料金・サービス案内を書かない。未確認の設備・特典・イベントを書かない。

過去の投稿(新しいものが下):
{history}

今回避けること:
{avoid}

次のJSONだけを返してください:
{{"theme": "今回のテーマ(日本語・短く)",
  "subject": "主役被写体", "composition": "構図", "background": "背景", "palette": "配色",
  "image_prompt": "FLUX向けの英語の画像生成指示(写真風、縦長、中央に主役、上下に暗めの余白)",
  "copy": "一言コピー"}}"""
    return ask_json(prompt, required_keys=("theme", "subject", "composition", "background", "palette", "image_prompt", "copy"))


def is_open_notice(img: Image.Image) -> dict:
    """過去ストーリーが「営業中！」の営業告知かどうかを判定する。"""
    prompt = """この画像はInstagramストーリーです。画像内に「営業中」という文字が含まれ、
お店の営業告知になっていますか。次のJSONだけを返してください:
{"is_open_notice": true または false, "found_text": "画像内で読めた主な文字",
 "description": "主役被写体・構図・背景・配色を日本語で1文", "theme": "訴求テーマを短く"}"""
    return ask_json(prompt, images=[img], required_keys=("is_open_notice", "description"))


def compare_with_past(candidate: Image.Image, past_imgs: list[Image.Image]) -> dict:
    """候補画像と過去画像を項目別に比べる。2項目以上が実質同一なら類似とする。"""
    prompt = f"""1枚目が今回の候補画像、2枚目以降({len(past_imgs)}枚)が過去に投稿したストーリーです。
重ねた文字は無視して、写真部分について過去の各画像と比べてください。
項目: 主役被写体 / 構図 / 背景 / 配色 / 訴求テーマ
色や小物だけ違うものは「同じ」と扱ってください。
次のJSONだけを返してください:
{{"most_similar_index": 過去画像の番号(2枚目を1とする。なければ0),
  "same_items": ["最も似ている過去画像と実質同一の項目名"],
  "reason": "日本語で1文"}}"""
    return ask_json(prompt, images=[candidate] + past_imgs, required_keys=("most_similar_index", "same_items", "reason"))


def quality_score(img: Image.Image) -> dict:
    prompt = """これはカラオケバー「BAR B」の営業告知ストーリーです。次の観点で10点満点で厳しく採点してください:
写真の質感(破綻・不自然な歪み・崩れた物体がないか)、バーらしい世界観、構図、文字の読みやすさ、来店したくなるか。
次のJSONだけを返してください:
{"score": 整数, "problems": ["問題点(なければ空)"]}"""
    return ask_json(prompt, images=[img], required_keys=("score",))

# ======================================================================
# 履歴(data/history.json)・各回の記録(data/runs/)・画像の保存と、GitHubへの保存(push)。
# 画像はリポジトリに保存し、公開URL(raw.githubusercontent.com)をMetricoolに取り込ませる。
# 


HISTORY = Path(DATA_DIR) / "history.json"
RUNS = Path(DATA_DIR) / "runs"


def load_history() -> dict:
    if HISTORY.exists():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return {"stories": [], "legacy_checked": {}}


def save_history(h: dict):
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=1), encoding="utf-8")


def run_log_path(day, slot="story") -> Path:
    RUNS.mkdir(parents=True, exist_ok=True)
    return RUNS / f"{day.isoformat()}_{slot}.json"


def load_run(day) -> dict:
    p = run_log_path(day)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def save_run(day, rec: dict):
    run_log_path(day).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")


def media_path(name: str) -> Path:
    p = Path(MEDIA_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p / name


def public_url(path: Path) -> str:
    if not REPO:
        raise RuntimeError("GITHUB_REPOSITORY が不明のため画像の公開URLを作れません")
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{path.as_posix()}"


def _git(*args):
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True)


def commit_and_push(message: str, paths=(".",)):
    """変更をGitHubに保存する。保存に失敗した場合は例外にして、保存済みと扱わない。"""
    if os.environ.get("NO_GIT") == "1":
        return
    _git("config", "user.name", "barb-story-bot")
    _git("config", "user.email", "barb-story-bot@users.noreply.github.com")
    _git("add", *paths)
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    _git("commit", "-m", message)
    for i in range(4):
        try:
            _git("pull", "--rebase", "origin", BRANCH)
            _git("push", "origin", f"HEAD:{BRANCH}")
            return
        except subprocess.CalledProcessError as e:
            err = e.stderr
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"GitHubへの保存(push)に失敗: {err}")


def wait_public(url: str, timeout=180):
    """push直後は公開URLにすぐ反映されないことがあるため、取得できるまで待つ。"""
    import requests
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                return True
        except requests.RequestException:
            pass
        time.sleep(10)
    raise RuntimeError(f"画像の公開URLが取得できません: {url}")

# ======================================================================
# BAR B「営業中！」ストーリー自動投稿。
# 
# 使い方:
#   python -m src.story create   … 制作〜Metricool予約登録〜登録内容の再取得確認(14時前に実行)
#   python -m src.story verify   … 公開予定時刻後に実公開を確認し、未公開なら復旧(15時以降に実行)
#   python -m src.story selftest … キーとAPIの疎通確認だけを行う(投稿しない)
# 
# 終了コード 0=完了または対象外の日 / 1=未完了(GitHubから失敗通知メールが届く)
# 



THUMBS = Path(DATA_DIR) / "thumbs"


def log(rec, step, status, **detail):
    entry = {"time": datetime.now(JST).isoformat(timespec="seconds"), "step": step, "status": status}
    entry.update(detail)
    rec.setdefault("log", []).append(entry)
    print(f"[{entry['time']}] {step}: {status} " + (json.dumps(detail, ensure_ascii=False)[:600] if detail else ""), flush=True)


def jp_time(dt_str: str) -> str:
    d = datetime.fromisoformat(dt_str)
    return f"{d.month}月{d.day}日 {d.hour}時{d.minute:02d}分"


def _download(url) -> Image.Image:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _thumb_path(key: str) -> Path:
    THUMBS.mkdir(parents=True, exist_ok=True)
    return THUMBS / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg")


def _save_thumb(img, key) -> str:
    p = _thumb_path(key)
    im = img.copy()
    im.thumbnail((360, 640))
    im.save(p, quality=85)
    return p.as_posix()


# ------------------------------------------------------------------ 過去ストーリーの取得
def collect_past(rec, history, now):
    """9/14以降のMetricool上のストーリーを全区間取得し、「営業中！」告知だけを比較対象にする。"""
    posts = list_posts(HISTORY_START, now + timedelta(days=1))
    stories = [p for p in posts if is_instagram_story(p)]
    own_urls = {s.get("media_url") for s in history["stories"]}
    legacy = history.setdefault("legacy_checked", {})
    unconfirmed, newly = [], 0

    for p in stories:
        for url in p.get("media") or []:
            if url in own_urls:
                continue
            item = legacy.get(url)
            if item and item.get("is_open") is not None:
                continue
            try:
                img = _download(url)
            except Exception as e:  # noqa: BLE001
                legacy[url] = {"is_open": None, "error": f"画像取得失敗: {e}", "date": p["publicationDate"]["dateTime"]}
                unconfirmed.append(url)
                continue
            try:
                j = is_open_notice(img)
                is_open = bool(j["is_open_notice"])
                desc, theme = j.get("description", ""), j.get("theme", "")
            except Exception as e:  # noqa: BLE001
                is_open, desc, theme = None, "", ""
                unconfirmed.append(url)
                log(rec, "過去ストーリー判定", "未確認", url=url, error=str(e))
            legacy[url] = {"is_open": is_open, "description": desc, "theme": theme,
                           "date": p["publicationDate"]["dateTime"], "fp": fingerprint(img),
                           "thumb": _save_thumb(img, url)}
            newly += 1

    compare = []
    for s in history["stories"]:
        compare.append({"source": "own", "date": s["date"], "theme": s.get("theme", ""), "copy": s.get("copy", ""),
                        "description": s.get("description", ""), "fp": s["fp"], "thumb": s.get("thumb")})
    for url, item in legacy.items():
        # 「営業中」と判定できたもの + 判定できなかったもの(安全側で比較対象に含める)
        if item.get("is_open") is False or "fp" not in item:
            continue
        compare.append({"source": "legacy", "date": item.get("date"), "theme": item.get("theme", ""), "copy": "",
                        "description": item.get("description", ""), "fp": item["fp"], "thumb": item.get("thumb")})
    compare.sort(key=lambda x: x.get("date") or "")

    log(rec, "過去ストーリー取得", "完了", 取得期間=f"{HISTORY_START.isoformat()}〜{now.isoformat(timespec='minutes')}",
        Metricool投稿数=len(posts), ストーリー数=len(stories), 今回新たに判定=newly,
        比較対象=len(compare), 判定できず比較対象に含めた数=len(unconfirmed))
    return compare


# ------------------------------------------------------------------ 類似チェック
def numeric_similar(fp, past):
    for p in past:
        d = phash_distance(fp["phash_center"], p["fp"]["phash_center"])
        hs = hist_similarity(fp["hist_center"], p["fp"]["hist_center"])
        if d < PHASH_MIN_DISTANCE or (hs >= COLOR_HIST_MAX_SIM and d < PHASH_MIN_DISTANCE + 8):
            return p, d, round(hs, 3)
    return None, None, None


def text_similar(plan, past):
    for p in past:
        if p.get("copy") and (plan["copy"] == p["copy"] or text_ratio(plan["copy"], p["copy"]) >= COPY_MAX_RATIO):
            return f"一言コピーが過去({p['date']})と類似: {p['copy']}"
        if p.get("theme") and text_ratio(plan["theme"], p["theme"]) >= THEME_MAX_RATIO:
            return f"テーマが過去({p['date']})と類似: {p['theme']}"
    return None


# ------------------------------------------------------------------ 制作
def produce(rec, today, past):
    avoid, image_attempts, plan_attempts = [], 0, 0
    while image_attempts < MAX_ATTEMPTS and plan_attempts < MAX_ATTEMPTS * 3:
        plan_attempts += 1
        plan = plan_story(today, past, avoid)
        copy = plan["copy"].strip()
        if not (COPY_MIN_LEN <= len(copy) <= COPY_MAX_LEN):
            avoid.append(f"一言コピーは{COPY_MIN_LEN}〜{COPY_MAX_LEN}文字にする(前回{len(copy)}文字)")
            log(rec, "企画", "FAIL", 理由="一言コピーの文字数", copy=copy)
            continue
        if any(w in copy for w in ("BAR B", "円", "飲み放題", "特典")):
            avoid.append("一言コピーに店名・料金・サービス案内を入れない")
            log(rec, "企画", "FAIL", 理由="一言コピーに禁止内容", copy=copy)
            continue
        why = text_similar(plan, past)
        if why:
            avoid.append(why)
            log(rec, "企画", "FAIL", 理由=why)
            continue

        image_attempts += 1
        bg = generate(plan["image_prompt"])
        final, report = compose(bg, today, copy)
        errors = check_composition(report, today, copy)
        if errors:
            avoid.append("背景の上部と下部は暗く落ち着いたトーンにする(文字が読めるように)")
            log(rec, "必須表示照合", "FAIL", 試行=image_attempts, 理由=errors)
            continue
        log(rec, "必須表示照合", "PASS", 試行=image_attempts, 描画文字=[d["text"] for d in report["drawn"]],
            コントラスト=[d["contrast"] for d in report["drawn"]])

        fp = fingerprint(final)
        hit, dist, hs = numeric_similar(fp, past)
        if hit:
            note = f"過去({hit['date']})の画像と似ている: {hit.get('description','')}"
            avoid.append(note)
            log(rec, "類似チェック(数値)", "FAIL", 試行=image_attempts, 距離=dist, 色類似度=hs, 相手=hit["date"])
            continue
        log(rec, "類似チェック(数値)", "PASS", 試行=image_attempts, 比較件数=len(past))

        thumbs = [Image.open(p["thumb"]) for p in past if p.get("thumb") and Path(p["thumb"]).exists()][-40:]
        if thumbs:
            cmp = compare_with_past(final, thumbs)
            same = cmp.get("same_items") or []
            if len(same) >= 2:
                avoid.append(f"過去画像と{'・'.join(same)}が同じだった。{cmp.get('reason','')}")
                log(rec, "類似チェック(項目別)", "FAIL", 試行=image_attempts, 同一項目=same, 理由=cmp.get("reason"))
                continue
            log(rec, "類似チェック(項目別)", "PASS", 試行=image_attempts, 比較枚数=len(thumbs), 同一項目=same)
        else:
            log(rec, "類似チェック(項目別)", "対象なし", 試行=image_attempts)

        q = quality_score(final)
        if int(q.get("score", 0)) < QUALITY_MIN_SCORE:
            avoid.append(f"品質不足の指摘: {q.get('problems')}")
            log(rec, "品質審査", "FAIL", 試行=image_attempts, 点数=q.get("score"), 問題=q.get("problems"))
            continue
        log(rec, "品質審査", "PASS", 試行=image_attempts, 点数=q.get("score"))
        return plan, copy, final, fp
    raise RuntimeError(f"合格する画像を作れませんでした(画像{image_attempts}回・企画{plan_attempts}回)")


# ------------------------------------------------------------------ 登録内容の確認
def verify_registration(post, expected_media, publish_at):
    problems = []
    pr = provider(post)
    if not pr:
        problems.append("Instagramが投稿先に入っていない")
    if (post.get("instagramData") or {}).get("type") != "STORY":
        problems.append(f"投稿形式がSTORYでない: {(post.get('instagramData') or {}).get('type')}")
    if not post.get("media"):
        problems.append("画像が登録されていない")
    elif expected_media not in post["media"]:
        problems.append("登録された画像が今回の画像と違う")
    if post.get("autoPublish") is not True:
        problems.append("自動公開がONでない")
    if post.get("draft") is not False:
        problems.append("下書き状態")
    dt = (post.get("publicationDate") or {}).get("dateTime", "")
    if dt[:16] != publish_at.strftime("%Y-%m-%dT%H:%M"):
        problems.append(f"公開予定日時が違う: {dt}")
    if not (post.get("id") or post.get("uuid")):
        problems.append("投稿IDがない")
    return problems


def find_post(post_id, day):
    try:
        return get_post(post_id)
    except MetricoolError:
        start = datetime(day.year, day.month, day.day, tzinfo=JST)
        for p in list_posts(start, start + timedelta(days=1)):
            if str(p.get("id")) == str(post_id):
                return p
    return None


def register(rec, today, media_url, publish_at):
    for attempt in range(1, 4):
        created = create_story(media_url, publish_at)
        post_id = created.get("id") if isinstance(created, dict) else None
        log(rec, "Metricool登録", "送信済み", 試行=attempt, 投稿ID=post_id)
        rec.update({"post_id": post_id, "publish_at": publish_at.isoformat(), "media_url": media_url})
        save_run(today, rec)
        post = find_post(post_id, today) if post_id else None
        if not post:
            log(rec, "登録の再取得", "FAIL", 試行=attempt, 理由="予約データが見つからない")
            continue
        problems = verify_registration(post, media_url, publish_at)
        if problems:
            log(rec, "登録内容の照合", "FAIL", 試行=attempt, 理由=problems)
            continue
        log(rec, "登録内容の照合", "PASS", 投稿ID=post.get("id"), uuid=post.get("uuid"),
            形式="STORY", 自動公開=True, 下書き=False, 公開予定日時=post["publicationDate"]["dateTime"])
        return post
    raise RuntimeError("Metricoolへの予約登録を確認できませんでした")


# ------------------------------------------------------------------ メイン処理
def cmd_create():
    now = datetime.now(JST)
    today = now.date()
    rec = load_run(today) or {"date": today.isoformat(), "kind": "営業中ストーリー"}
    history = load_history()
    try:
        bd = check(today)
        log(rec, "営業日判定", "営業日" if bd["business_day"] else "対象外", 理由=bd["reason"], 情報源=bd["sources"])
        if bd["business_day"] is not True:
            rec["result"] = "対象外の日" if bd["business_day"] is False else "確認不能"
            return 0 if bd["business_day"] is False else 1

        if rec.get("post_id"):
            post = find_post(rec["post_id"], today)
            if post and not verify_registration(post, rec["media_url"], datetime.fromisoformat(rec["publish_at"])):
                log(rec, "二重登録防止", "登録済みを確認したため制作しない", 投稿ID=rec["post_id"])
                return 0

        past = collect_past(rec, history, now)
        plan, copy, final, fp = produce(rec, today, past)

        img_path = media_path(f"{today.isoformat()}_story_{now.strftime('%H%M%S')}.png")
        final.save(img_path, optimize=True)
        sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
        rec.update({"final_file": img_path.as_posix(), "final_sha256": sha, "theme": plan["theme"], "copy": copy})
        log(rec, "最終ファイル確定", "完了", ファイル=img_path.as_posix(), sha256=sha[:16])
        save_run(today, rec)
        commit_and_push(f"story {today}: 最終画像", [MEDIA_DIR, DATA_DIR])

        url = public_url(img_path)
        wait_public(url)
        # 公開URLの画像が最終ファイルと同一か照合
        if hashlib.sha256(requests.get(url, timeout=60).content).hexdigest() != sha:
            raise RuntimeError("公開URLの画像が最終ファイルと一致しない")
        media_url = normalize_image(url)
        log(rec, "素材受け渡し", "完了", 公開URL=url, MetricoolURL=media_url)

        publish_at = now.replace(hour=PUBLISH_HOUR, minute=PUBLISH_MINUTE, second=0, microsecond=0)
        if now > publish_at - timedelta(minutes=5):
            publish_at = (now + timedelta(minutes=LATE_MARGIN_MIN)).replace(second=0, microsecond=0)
            log(rec, "公開時刻", "予定時刻を過ぎて実行されたため変更", 公開予定=publish_at.isoformat())

        post = register(rec, today, media_url, publish_at)
        history["stories"].append({
            "date": today.isoformat(), "post_id": post.get("id"), "uuid": post.get("uuid"), "media_url": media_url,
            "file": img_path.as_posix(), "theme": plan["theme"], "copy": copy,
            "description": f"{plan['subject']} / {plan['composition']} / {plan['background']} / {plan['palette']}",
            "fp": fp, "thumb": _save_thumb(final, media_url)})
        rec["result"] = "予約登録済み(公開待ち)"
        print(f"\n現在の投稿状態: 予約済み(公開待ち)\n公開予定日時: {jp_time(post['publicationDate']['dateTime'])}公開予定\n")
        return 0
    except Exception as e:  # noqa: BLE001
        log(rec, "エラー", "未完了", error=str(e), trace=traceback.format_exc()[-1500:])
        rec["result"] = "未完了"
        print(f"\n現在の投稿状態: {'予約未登録' if not rec.get('post_id') else '登録確認未完了'}"
              f"\n公開予定日時: {'未設定' if not rec.get('post_id') else rec.get('publish_at')}\n")
        return 1
    finally:
        save_run(today, rec)
        save_history(history)
        try:
            commit_and_push(f"story {today}: 記録", [MEDIA_DIR, DATA_DIR])
        except Exception as e:  # noqa: BLE001
            print(f"記録の保存に失敗: {e}")


def cmd_verify():
    now = datetime.now(JST)
    today = now.date()
    rec = load_run(today)
    if not rec:
        bd = check(today)
        if bd["business_day"] is False:
            print(f"対象外の日({bd['reason']})")
            return 0
        print("現在の投稿状態: 予約未登録\n公開予定日時: 未設定")
        return 1
    if rec.get("result") == "対象外の日":
        print("対象外の日")
        return 0
    try:
        if not rec.get("post_id"):
            log(rec, "公開確認", "FAIL", 理由="予約が登録されていない")
            return 1
        post = find_post(rec["post_id"], today)
        if not post:
            log(rec, "公開確認", "FAIL", 理由="予約データが見つからない")
            return 1
        pr = provider(post)
        status = pr.get("status")
        publish_at = datetime.fromisoformat(rec["publish_at"])
        if status == "PUBLISHED" and pr.get("publicUrl"):
            same = rec["media_url"] in (post.get("media") or [])
            type_ok = (post.get("instagramData") or {}).get("type") == "STORY" and "/stories/" in pr["publicUrl"]
            if not (same and type_ok):
                log(rec, "公開確認", "FAIL", 理由="公開物が登録内容と一致しない", 画像一致=same, STORY形式=type_ok)
                return 1
            rec.update({"result": "Instagramストーリー公開完了", "public_url": pr["publicUrl"],
                        "published_at": post["publicationDate"]["dateTime"]})
            log(rec, "公開確認", "完了", 公開URL=pr["publicUrl"], 公開日時=post["publicationDate"]["dateTime"])
            print(f"\nInstagramストーリー公開完了\n公開日時: {jp_time(post['publicationDate']['dateTime'])}\nURL: {pr['publicUrl']}\n")
            return 0
        if status not in ("ERROR", "FAILED", "REJECTED") and now > publish_at + timedelta(minutes=40):
            # 「処理中」のまま長時間止まっている場合は、再登録すると二重投稿になる恐れがあるため通知のみ
            log(rec, "公開確認", "FAIL", 状態=status, 詳細=pr.get("detailedStatus"),
                理由="公開予定時刻から40分以上たっても公開されていない(二重投稿防止のため自動再登録はしない)")
            return 1
        if status in ("ERROR", "FAILED", "REJECTED"):
            if rec.get("recovery_count", 0) >= 2:
                log(rec, "公開確認", "FAIL", 状態=status, 詳細=pr.get("detailedStatus"), 理由="復旧を2回行っても公開されない")
                return 1
            rec["recovery_count"] = rec.get("recovery_count", 0) + 1
            log(rec, "復旧", "再登録を実行", 状態=status, 詳細=pr.get("detailedStatus"))
            new_at = (now + timedelta(minutes=LATE_MARGIN_MIN)).replace(second=0, microsecond=0)
            register(rec, today, rec["media_url"], new_at)
            rec["result"] = "再登録済み(公開待ち)"
            print(f"\n現在の投稿状態: 再登録済み(公開待ち)\n公開予定日時: {jp_time(new_at.isoformat())}公開予定\n")
            return 1  # 公開まで確認できていないので未完了として通知する
        log(rec, "公開確認", "公開待ち", 状態=status, 公開予定=rec["publish_at"])
        print(f"\n現在の投稿状態: {status}\n公開予定日時: {jp_time(rec['publish_at'])}公開予定\n")
        return 0  # まだ公開待ち。次の確認で再チェックする
    except Exception as e:  # noqa: BLE001
        log(rec, "エラー", "未完了", error=str(e), trace=traceback.format_exc()[-1500:])
        return 1
    finally:
        save_run(today, rec)
        try:
            commit_and_push(f"story {today}: 公開確認", [DATA_DIR])
        except Exception as e:  # noqa: BLE001
            print(f"記録の保存に失敗: {e}")


def cmd_selftest():
    ok = True
    now = datetime.now(JST)
    for name, fn in [
        ("営業日判定", lambda: check(now.date())),
        ("Metricool 取得", lambda: f"{len(list_posts(now - timedelta(days=3), now))}件"),
        ("Gemini", lambda: ask_json('{"ok": true} とだけ返してください', required_keys=("ok",))),
        ("Cloudflare 画像生成", lambda: generate("a quiet cocktail glass on a bar counter, night").size),
    ]:
        try:
            print(f"OK  {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"NG  {name}: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"create": cmd_create, "verify": cmd_verify, "selftest": cmd_selftest}.get(cmd, lambda: (print(__doc__), 2)[1])())