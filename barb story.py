"""BAR B「営業中！」ストーリー 画像の自動制作(1ファイル版)。

  python "barb story.py" create   … 今日の画像を作る
  python "barb story.py" preview  … デザイン見本を8枚作る
  python "barb story.py" selftest … 接続確認
"""
import os
from zoneinfo import ZoneInfo
from datetime import datetime
from difflib import SequenceMatcher
import numpy as np
from PIL import Image
import colorsys
import random
from PIL import Image, ImageDraw, ImageFilter, ImageFont
import csv
import io
from datetime import date
import requests
import base64
import time
import json
import re
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
GEMINI_MODEL = os.environ.get("GEMINI_MODEL") or "gemini-flash-latest"

# ---- 保存先(GitHubリポジトリ) ----
REPO = os.environ.get("GITHUB_REPOSITORY", "")       # owner/repo
BRANCH = os.environ.get("GITHUB_REF_NAME") or "main"
DATA_DIR = os.environ.get("DATA_DIR", "data")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "media")

# ---- 2026/9/14以降にMetricoolから投稿されたストーリー画像(営業中かどうかは初回にGeminiが判定し、営業中だけを比較対象にする) ----
LEGACY_STORY_URLS = [
    "https://static.metricool.com/planner/202609/6954500-file-9138319819706982859.jpeg",
    "https://static.metricool.com/planner/202609/6954500-file-15231133520621534251.png",
    "https://static.metricool.com/planner/202609/6954500-file-5834038180325544990.png",
    "https://static.metricool.com/planner/202609/6954500-file-10892143037199406272.png",
    "https://static.metricool.com/planner/202609/6954500-file-13349175336319722121.png",
    "https://static.metricool.com/planner/202609/6954500-file-14449577558380708967.png",
    "https://static.metricool.com/planner/202609/6954500-file-432646783773462852.png",
    "https://static.metricool.com/planner/202609/6954500-file-3577081295170920546.png",
    "https://static.metricool.com/planner/202609/6954500-file-17975550308338442184.jpeg",
    "https://static.metricool.com/planner/202609/6954500-file-3417229298306381597.png",
    "https://static.metricool.com/planner/202609/6954500-file-17983440749469538826.png",
    "https://static.metricool.com/planner/202610/6954500-file-10365170830739524773.png",
    "https://static.metricool.com/planner/202610/6954500-file-10691966045742038693.png",
    "https://static.metricool.com/planner/202610/6954500-file-4542850437711646019.png",
    "https://static.metricool.com/planner/202610/6954500-file-5221170614118843320.png",
    "https://static.metricool.com/planner/202610/6954500-file-5612299093167100290.png",
    "https://static.metricool.com/planner/202610/6954500-file-10841523785648445762.png",
]

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
# 背景写真に必須表示を重ねてストーリー画像(1080x1920)を作る。
# 文字は画像生成AIに描かせず、ここで設定値の文字列をそのまま描画する。
# 毎回「レイアウト × 書体 × アクセント色」の組み合わせを変えて、見た目に変化をつける。
# 戻り値の report には実際に描画した文字列・位置・コントラスト比が入り、最終照合に使う。
# 



W, H = 1080, 1920
FONT_DIR = "/usr/share/fonts/opentype/noto"
FONTS = {
    # 書体セット名: (見出し用, 本文用)
    "gothic": ("NotoSansCJK-Black.ttc", "NotoSansCJK-Bold.ttc"),
    "gothic_soft": ("NotoSansCJK-Bold.ttc", "NotoSansCJK-Medium.ttc"),
    "mincho": ("NotoSerifCJK-Black.ttc", "NotoSerifCJK-Bold.ttc"),
    "mincho_light": ("NotoSerifCJK-Bold.ttc", "NotoSerifCJK-SemiBold.ttc"),
}
ACCENTS = {
    "gold": (255, 210, 120),
    "champagne": (240, 222, 190),
    "neon_pink": (255, 110, 190),
    "neon_cyan": (110, 230, 255),
    "white": (255, 255, 255),
    "rose": (255, 170, 160),
}
LAYOUTS = ["classic", "left_editorial", "center_card", "frame", "neon", "band", "vertical_title", "split"]


# ------------------------------------------------------------------ 基本部品
def _font_path(name):
    p = os.path.join(FONT_DIR, name)
    return p if os.path.exists(p) else os.path.join(FONT_DIR, "NotoSansCJK-Bold.ttc")


def _font(name, size):
    return ImageFont.truetype(_font_path(name), size=size, index=FONT_INDEX_JP)


def _cover(img, w, h):
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    nw, nh = round(img.width * scale), round(img.height * scale)
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _luminance(rgb):
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * c[..., 0] + 0.7152 * c[..., 1] + 0.0722 * c[..., 2]


def _contrast(text_rgb, region):
    lt = float(_luminance(np.array(text_rgb)))
    lb = float(np.percentile(_luminance(np.array(region.convert("RGB"))), 95))
    hi, lo = max(lt, lb), min(lt, lb)
    return (hi + 0.05) / (lo + 0.05)


def _shade(size, alpha_top, alpha_bottom, y0, y1, color=(0, 0, 0)):
    w, h = size
    ys = np.arange(h, dtype=np.float32)
    t = np.clip((ys - y0) / max(1, (y1 - y0)), 0, 1)
    col = alpha_top + (alpha_bottom - alpha_top) * t
    a = np.repeat(col[:, None], w, axis=1).astype(np.uint8)
    layer = Image.new("RGBA", size, color + (0,))
    layer.putalpha(Image.fromarray(a))
    return layer


def _dominant_hue(img):
    small = np.asarray(img.convert("RGB").resize((64, 112)), dtype=np.float64) / 255.0
    r, g, b = small[..., 0].mean(), small[..., 1].mean(), small[..., 2].mean()
    return colorsys.rgb_to_hsv(r, g, b)


class Canvas:
    def __init__(self, bg):
        self.im = _cover(bg, W, H).convert("RGBA")
        self.drawn = []

    def overlay(self, layer):
        self.im.alpha_composite(layer)

    def rect(self, box, fill, radius=0, outline=None, width=0):
        layer = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        if radius:
            d.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)
        else:
            d.rectangle(box, fill=fill, outline=outline, width=width)
        self.im.alpha_composite(layer)

    def blur_region(self, box, radius=18):
        region = self.im.crop(box).filter(ImageFilter.GaussianBlur(radius))
        self.im.paste(region, box[:2])

    def text(self, role, text, font, fill, x=None, y=0, align="center", glow=None, shadow=True, max_w=None):
        """文字を1行描く。x=None で中央、align='left'/'right' のときは x を基準にする。"""
        d = ImageDraw.Draw(self.im)
        bbox = d.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if align == "center":
            px = (W - tw) // 2 - bbox[0] if x is None else x - tw // 2 - bbox[0]
        elif align == "left":
            px = x - bbox[0]
        else:
            px = x - tw - bbox[0]
        box = (max(0, px + bbox[0] - 6), max(0, y + bbox[1] - 6), min(W, px + bbox[2] + 6), min(H, y + bbox[3] + 6))
        region = self.im.crop(box)  # 文字を置く前の背景(帯や影を含む)で判定する
        self._effects(px, y, text, font, glow, shadow)
        ImageDraw.Draw(self.im).text((px, y), text, font=font, fill=fill + (255,))
        self.drawn.append({"role": role, "text": text, "box": box, "height": th,
                           "contrast": round(_contrast(fill, region), 2)})
        return box

    def vtext(self, role, text, font, fill, x, y, glow=None, shadow=True, gap=0):
        """縦書き(1文字ずつ下へ)。"""
        d = ImageDraw.Draw(self.im)
        size = font.size
        boxes, cy = [], y
        for ch in text:
            b = d.textbbox((0, 0), ch, font=font)
            cw = b[2] - b[0]
            px = x - cw // 2 - b[0]
            boxes.append((px + b[0], cy + b[1], px + b[2], cy + b[3]))
            cy += size + gap
        box = (max(0, min(b[0] for b in boxes) - 6), max(0, boxes[0][1] - 6),
               min(W, max(b[2] for b in boxes) + 6), min(H, boxes[-1][3] + 6))
        region = self.im.crop(box)
        cy = y
        for ch in text:
            b = d.textbbox((0, 0), ch, font=font)
            px = x - (b[2] - b[0]) // 2 - b[0]
            self._effects(px, cy, ch, font, glow, shadow)
            ImageDraw.Draw(self.im).text((px, cy), ch, font=font, fill=fill + (255,))
            cy += size + gap
        self.drawn.append({"role": role, "text": text, "box": box, "height": cy - y,
                           "contrast": round(_contrast(fill, region), 2)})
        return box

    def _effects(self, px, y, text, font, glow, shadow):
        if glow:
            g = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
            ImageDraw.Draw(g).text((px, y), text, font=font, fill=glow + (230,))
            self.im.alpha_composite(g.filter(ImageFilter.GaussianBlur(14)))
            self.im.alpha_composite(g.filter(ImageFilter.GaussianBlur(5)))
        if shadow:
            s = Image.new("RGBA", self.im.size, (0, 0, 0, 0))
            ImageDraw.Draw(s).text((px + 3, y + 4), text, font=font, fill=(0, 0, 0, 190))
            self.im.alpha_composite(s.filter(ImageFilter.GaussianBlur(4)))


def _fit(fontname, text, size, max_w):
    """幅に収まるまで文字サイズを下げる。"""
    while size > 20:
        f = _font(fontname, size)
        b = ImageDraw.Draw(Image.new("RGB", (1, 1))).textbbox((0, 0), text, font=f)
        if b[2] - b[0] <= max_w:
            return f
        size -= 2
    return _font(fontname, size)


# ------------------------------------------------------------------ レイアウト
# 安全域: 上端250px・下端280pxはInstagramのUI(アカウント名・返信欄)と重なるので文字を置かない
TOP, BOTTOM = 260, H - 300


def _info_block(cv, s, y, align="center", x=None, size=44, gap=64, hours_size=50, max_w=960):
    for item in SERVICE_ITEMS:
        cv.text("item", item, _fit(s["body"], item, size, max_w), (255, 255, 255), x=x, y=y, align=align)
        y += gap
    cv.text("hours", HOURS_TEXT, _fit(s["body"], HOURS_TEXT, hours_size, max_w), s["accent"], x=x, y=y + 16, align=align)
    return y + 16 + hours_size


def layout_classic(cv, s):
    cv.overlay(_shade((W, H), 200, 0, 720, 940))
    cv.overlay(_shade((W, H), 0, 215, 1080, 1360))
    cv.text("open", OPEN_TEXT, _font(s["head"], 150), s["accent"], y=TOP + 10)
    cv.text("date", s["date"], _font(s["body"], 64), (255, 255, 255), y=TOP + 215)
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 54, 960), (255, 255, 255), y=TOP + 320)
    _info_block(cv, s, 1320)


def layout_left_editorial(cv, s):
    cv.overlay(_shade((W, H), 0, 225, 900, 1250))
    cv.overlay(_shade((W, H), 175, 0, 560, 760))
    x = 90
    cv.rect((x - 30, TOP + 20, x - 22, TOP + 330), s["accent"] + (255,))
    cv.text("open", OPEN_TEXT, _font(s["head"], 140), s["accent"], x=x, y=TOP, align="left")
    cv.text("date", s["date"], _font(s["body"], 60), (255, 255, 255), x=x, y=TOP + 190, align="left")
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 56, 900), (255, 255, 255), x=x, y=1180, align="left")
    cv.rect((x, 1275, x + 120, 1281), s["accent"] + (255,))
    _info_block(cv, s, 1312, align="left", x=x, size=42, gap=60)


def layout_center_card(cv, s):
    cv.overlay(_shade((W, H), 160, 0, 620, 820))
    cv.text("open", OPEN_TEXT, _font(s["head"], 150), s["accent"], y=TOP + 30)
    cv.text("date", s["date"], _font(s["body"], 62), (255, 255, 255), y=TOP + 230)
    card = (80, 1100, W - 80, 1600)
    cv.blur_region(card, 22)
    cv.rect(card, (12, 12, 18, 175), radius=36, outline=s["accent"] + (200,), width=3)
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 52, 840), s["accent"], y=1145)
    cv.rect((W // 2 - 60, 1232, W // 2 + 60, 1236), (255, 255, 255, 180))
    _info_block(cv, s, 1262, size=40, gap=56, hours_size=46, max_w=840)


def layout_frame(cv, s):
    cv.overlay(_shade((W, H), 190, 0, 700, 920))
    cv.overlay(_shade((W, H), 0, 210, 1060, 1340))
    cv.rect((40, 200, W - 40, H - 230), None, outline=s["accent"] + (220,), width=3)
    cv.rect((56, 216, W - 56, H - 246), None, outline=s["accent"] + (110,), width=1)
    cv.text("date", s["date"], _font(s["body"], 56), (255, 255, 255), y=TOP + 20)
    cv.text("open", OPEN_TEXT, _font(s["head"], 160), s["accent"], y=TOP + 100)
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 52, 900), (255, 255, 255), y=TOP + 330)
    _info_block(cv, s, 1320, size=42, gap=60)


def layout_neon(cv, s):
    neon = s["neon"]
    cv.overlay(_shade((W, H), 215, 0, 720, 960))
    cv.overlay(_shade((W, H), 0, 225, 1060, 1340))
    cv.text("open", OPEN_TEXT, _font(s["head"], 160), (255, 255, 255), y=TOP + 20, glow=neon, shadow=False)
    cv.text("date", s["date"], _font(s["body"], 62), neon, y=TOP + 240, shadow=True)
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 52, 960), (255, 255, 255), y=TOP + 340)
    cv.rect((140, 1285, W - 140, 1289), neon + (220,))
    _info_block(cv, s, 1318)


def layout_band(cv, s):
    cv.overlay(_shade((W, H), 150, 0, 520, 700))
    pill_font = _font(s["head"], 96)
    b = ImageDraw.Draw(cv.im).textbbox((0, 0), OPEN_TEXT, font=pill_font)
    pw = b[2] - b[0] + 100
    cv.rect((70, TOP + 10, 70 + pw, TOP + 170), (10, 10, 14, 215), radius=80, outline=s["accent"] + (255,), width=4)
    cv.text("open", OPEN_TEXT, pill_font, s["accent"], x=70 + pw // 2, y=TOP + 28, shadow=False)
    cv.text("date", s["date"], _font(s["body"], 60), (255, 255, 255), x=90, y=TOP + 205, align="left")
    band = (0, 1180, W, 1640)
    cv.rect(band, (8, 8, 12, 190))
    cv.rect((0, 1180, W, 1186), s["accent"] + (255,))
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 52, 960), s["accent"], y=1220)
    f = _fit(s["body"], "関内駅からアクセス良好", 40, 470)
    for i, item in enumerate(SERVICE_ITEMS):
        cx = W // 4 if i % 2 == 0 else W * 3 // 4
        cv.text("item", item, f, (255, 255, 255), x=cx, y=1325 + (i // 2) * 72)
    cv.text("hours", HOURS_TEXT, _font(s["body"], 50), s["accent"], y=1490)


def layout_vertical_title(cv, s):
    cv.overlay(_shade((W, H), 0, 225, 1000, 1300))
    side = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    a = np.zeros((H, W), dtype=np.uint8)
    a[:, W - 360:W - 260] = np.linspace(0, 230, 100, dtype=np.uint8)[None, :]
    a[:, W - 260:] = 230
    side.putalpha(Image.fromarray(a))
    cv.overlay(side)
    cv.vtext("open", OPEN_TEXT, _font(s["head"], 150), s["accent"], x=W - 140, y=TOP + 10)
    cv.text("date", s["date"], _font(s["body"], 58), (255, 255, 255), x=90, y=1170, align="left")
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 50, 760), (255, 255, 255), x=90, y=1250, align="left")
    _info_block(cv, s, 1345, align="left", x=90, size=38, gap=54, hours_size=46, max_w=760)


def layout_split(cv, s):
    block_top = 1180
    base = s["block_color"]
    cv.rect((0, block_top, W, H), base + (255,))
    cv.overlay(_shade((W, H), 175, 0, 560, 760))
    cv.text("date", s["date"], _font(s["body"], 60), (255, 255, 255), y=TOP + 20)
    cv.text("copy", s["copy"], _fit(s["body"], s["copy"], 54, 960), (255, 255, 255), y=TOP + 120)
    pill_font = _font(s["head"], 120)
    b = ImageDraw.Draw(cv.im).textbbox((0, 0), OPEN_TEXT, font=pill_font)
    pw, ph = b[2] - b[0] + 120, 170
    cv.rect(((W - pw) // 2, block_top - ph // 2, (W + pw) // 2, block_top + ph // 2), s["accent"] + (255,), radius=30)
    cv.text("open", OPEN_TEXT, pill_font, base, y=block_top - ph // 2 + 12, shadow=False)
    _info_block(cv, s, block_top + 130, size=42, gap=60)


RENDERERS = {name: globals()[f"layout_{name}"] for name in LAYOUTS}


# ------------------------------------------------------------------ 組み合わせの決定
def choose_style(bg, the_date, recent: list[dict], seed=None):
    """直近と同じ組み合わせを避けてレイアウト・書体・色を選ぶ。"""
    rng = random.Random(seed if seed is not None else the_date.toordinal())
    recent_layouts = [r.get("layout") for r in recent[-3:]]
    recent_fonts = [r.get("fontset") for r in recent[-1:]]
    recent_accents = [r.get("accent") for r in recent[-2:]]
    layout = rng.choice([l for l in LAYOUTS if l not in recent_layouts] or LAYOUTS)
    fontset = rng.choice([f for f in FONTS if f not in recent_fonts] or list(FONTS))
    accent = rng.choice([a for a in ACCENTS if a not in recent_accents] or list(ACCENTS))
    neon = ACCENTS[rng.choice(["neon_pink", "neon_cyan"])]
    h, s_, v = _dominant_hue(bg)
    block = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(h, min(0.55, s_ + 0.15), 0.16))
    return {"layout": layout, "fontset": fontset, "accent_name": accent, "accent": ACCENTS[accent],
            "neon": neon, "block_color": block, "head": FONTS[fontset][0], "body": FONTS[fontset][1]}


def compose(background, the_date, one_copy: str, style: dict):
    s = dict(style, date=date_text(the_date), copy=one_copy)
    cv = Canvas(background)
    RENDERERS[s["layout"]](cv, s)
    report = {"drawn": cv.drawn, "size": [W, H],
              "style": {k: style[k] for k in ("layout", "fontset", "accent_name")}}
    return cv.im.convert("RGB"), report


def check_composition(report: dict, the_date, one_copy: str) -> list[str]:
    """描画結果を必須表示と文字単位で照合する。問題があれば理由のリストを返す(空なら合格)。"""
    errors = []
    texts = {}
    for d in report["drawn"]:
        texts.setdefault(d["role"], []).append(d["text"])
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
    for d in report["drawn"]:
        x0, y0, x1, y1 = d["box"]
        if x0 <= 20 or x1 >= W - 20:
            errors.append(f"文字が画面端に近すぎる: {d['text']}")
        if y0 < 240 or y1 > H - 250:
            errors.append(f"文字がInstagramの表示と重なる位置にある: {d['text']}")
        if d["contrast"] < TEXT_MIN_CONTRAST:
            errors.append(f"文字のコントラスト不足({d['contrast']}): {d['text']}")
    # 文字同士の重なり
    boxes = [d["box"] for d in report["drawn"]]
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if a[0] < b[2] - 8 and b[0] < a[2] - 8 and a[1] < b[3] - 8 and b[1] < a[3] - 8:
                errors.append(f"文字が重なっている: {report['drawn'][i]['text']} / {report['drawn'][j]['text']}")
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
# 無料枠は1モデルあたり1日の回数が少ないため、
#  1. 呼び出し回数を減らす(画像の判定・審査はまとめて1回)
#  2. 上限に達したら、使える別の無料Flashモデルへ自動で切り替える
# 返答はJSON形式で受け取り、必要な項目がそろっているかをプログラムで検査する。
# 


API = "https://generativelanguage.googleapis.com/v1beta"
_EXHAUSTED = set()   # この実行中に1日の上限に達したモデル
_MODELS = None
CALLS = {"count": 0, "by_model": {}}


class GeminiError(RuntimeError):
    pass


def _img_part(img: Image.Image, max_side=768):
    im = img.convert("RGB")
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buf.getvalue()).decode()}}


def models() -> list[str]:
    """使える無料Flash系モデルの一覧(優先順)。設定したモデルを先頭にする。"""
    global _MODELS
    if _MODELS is not None:
        return _MODELS
    names = []
    try:
        r = requests.get(f"{API}/models", params={"key": GEMINI_API_KEY, "pageSize": 200}, timeout=30)
        for m in r.json().get("models", []):
            n = m["name"].split("/", 1)[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in n or any(x in n for x in ("image", "tts", "live", "audio", "embedding", "transcribe", "omni")):
                continue
            names.append(n)
    except Exception:  # noqa: BLE001
        pass

    def key(n):
        nums = [int(x) for x in re.findall(r"\d+", n.split("-")[1] if "-" in n else "0")] or [0]
        preview = "preview" in n or "exp" in n
        return (preview, "lite" in n, [-x for x in nums], n)

    names.sort(key=key)
    first = [GEMINI_MODEL] if GEMINI_MODEL else []
    _MODELS = first + [n for n in names if n not in first] + [n for n in ("gemini-flash-latest", "gemini-flash-lite-latest") if n not in names and n not in first]
    return _MODELS


def _retry_delay(text: str) -> float:
    m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', text)
    return float(m.group(1)) if m else 20.0


def ask_json(prompt: str, images=(), required_keys=(), temperature=0.9) -> dict:
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY が設定されていません")
    parts = [{"text": prompt}] + [_img_part(i) for i in images]
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature}}
    errors = []
    for model in models():
        if model in _EXHAUSTED:
            continue
        for attempt in range(3):
            try:
                r = requests.post(f"{API}/models/{model}:generateContent", params={"key": GEMINI_API_KEY},
                                  json=body, timeout=180)
            except requests.RequestException as e:
                errors.append(f"{model}: 通信エラー {e}")
                time.sleep(10)
                continue
            CALLS["count"] += 1
            CALLS["by_model"][model] = CALLS["by_model"].get(model, 0) + 1
            if r.status_code == 429:
                t = r.text
                if "PerDay" in t or "per day" in t.lower() or "limit: 0" in t:
                    _EXHAUSTED.add(model)
                    errors.append(f"{model}: 1日の無料上限に到達")
                    break  # 次のモデルへ
                wait = min(_retry_delay(t) + 2, 65)
                errors.append(f"{model}: 1分あたりの上限。{wait:.0f}秒待って再試行")
                time.sleep(wait)
                continue
            if r.status_code in (400, 403, 404):
                errors.append(f"{model}: HTTP {r.status_code} {r.text[:200]}")
                if r.status_code == 403 and "API_KEY" in r.text:
                    raise GeminiError("GeminiのAPIキーが無効です: " + r.text[:300])
                break  # このモデルは使えない
            if r.status_code != 200:
                errors.append(f"{model}: HTTP {r.status_code} {r.text[:200]}")
                time.sleep(10)
                continue
            try:
                text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                data = json.loads(text)
            except (KeyError, IndexError, ValueError) as e:
                errors.append(f"{model}: JSONとして読めない応答 {e}")
                continue
            if isinstance(data, dict):
                missing = [k for k in required_keys if k not in data or data[k] is None or data[k] == ""]
                if missing:
                    errors.append(f"{model}: 必須項目の欠落 {missing}")
                    continue  # 抜けがあれば作り直させる
            data_model = data if isinstance(data, dict) else {"items": data}
            data_model.setdefault("_model", model)
            return data_model
    raise GeminiError("Geminiの応答を取得できませんでした:\n" + "\n".join(errors[-8:]))


def plan_story(the_date, past: list[dict], avoid_notes: list[str]) -> dict:
    """その日のテーマ・背景画像の指示・一言コピーを決める。(1回の呼び出し)"""
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


def plan_variety(the_date, n: int) -> list[dict]:
    """見本用: 互いに違うテーマをn個まとめて考える。(1回の呼び出し)"""
    prompt = f"""横浜・関内のカラオケバー「BAR B」の「営業中！」告知ストーリー用に、
互いに主役被写体・構図・背景・配色・テーマがまったく違う案を{n}個考えてください。
背景は写真風で人物の顔なし、画像内に文字なし、中央に主役、上下に暗めの余白。
一言コピーは日本語で{COPY_MIN_LEN}〜{COPY_MAX_LEN}文字、店名・料金・サービス案内なし。
次のJSONだけを返してください:
{{"plans": [{{"theme": "", "subject": "", "composition": "", "background": "", "palette": "",
  "image_prompt": "FLUX向けの英語の画像生成指示", "copy": "一言コピー"}}]}}"""
    data = ask_json(prompt, required_keys=("plans",))
    plans = [p for p in data["plans"] if all(p.get(k) for k in ("theme", "image_prompt", "copy"))]
    if len(plans) < n:
        raise GeminiError(f"案が{n}個そろいませんでした({len(plans)}個)")
    return plans[:n]


def classify_stories(imgs: list[Image.Image]) -> list[dict]:
    """過去ストーリーが「営業中！」の営業告知かどうかを、まとめて1回で判定する。"""
    prompt = f"""これは{len(imgs)}枚のInstagramストーリー画像です(1枚目を1番とする)。
各画像について、画像内に「営業中」という文字があり、お店の営業告知になっているか判定してください。
次のJSONだけを返してください(画像の枚数と同じ{len(imgs)}件):
{{"results": [{{"index": 1, "is_open_notice": true または false, "found_text": "読めた主な文字",
  "description": "主役被写体・構図・背景・配色を日本語で1文", "theme": "訴求テーマを短く"}}]}}"""
    data = ask_json(prompt, images=imgs, required_keys=("results",), temperature=0.1)
    res = {int(r.get("index", 0)): r for r in data["results"] if isinstance(r, dict)}
    if sorted(res) != list(range(1, len(imgs) + 1)):
        raise GeminiError(f"判定結果の件数が合いません({len(res)}/{len(imgs)})")
    return [res[i] for i in range(1, len(imgs) + 1)]


def review(candidate: Image.Image, past_imgs: list[Image.Image]) -> dict:
    """類似チェック(項目別)と品質審査を1回でまとめて行う。"""
    if past_imgs:
        compare = f"""2枚目以降({len(past_imgs)}枚)は過去に投稿したストーリーです。重ねた文字は無視して、
写真部分について最も似ている過去画像と比べ、実質同一の項目を挙げてください。
項目: 主役被写体 / 構図 / 背景 / 配色 / 訴求テーマ (色や小物だけ違うものは「同じ」と扱う)"""
    else:
        compare = "過去画像はありません。same_items は空にしてください。"
    prompt = f"""1枚目は、カラオケバー「BAR B」の今回の営業告知ストーリー候補です。
{compare}

さらに1枚目を10点満点で厳しく採点してください:
写真の質感(破綻・不自然な歪み・崩れた物体がないか)、バーらしい世界観、構図、文字の読みやすさ、来店したくなるか。

次のJSONだけを返してください:
{{"most_similar_index": 最も似ている過去画像の番号(2枚目を1とする。なければ0),
  "same_items": ["実質同一の項目名"], "similar_reason": "日本語で1文",
  "score": 整数, "problems": ["問題点(なければ空)"]}}"""
    return ask_json(prompt, images=[candidate] + past_imgs, required_keys=("same_items", "score"), temperature=0.2)

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
# BAR B「営業中！」ストーリー 画像の自動制作。
# 
# このプログラムの担当: 営業日判定 → 過去画像との比較 → 企画 → 背景生成 → 文字合成 →
# 必須表示照合 → 類似チェック → 品質審査 → 画像と data/today.json をリポジトリに保存。
# 投稿(15時のストーリー予約)は、別の定時タスクのClaudeが data/today.json を読んで行う。
# 
# 使い方:
#   python barb_story.py create    … 今日の画像を作る
#   python barb_story.py preview   … デザイン見本を8枚作る(履歴には入れない・投稿しない)
#   python barb_story.py selftest  … キーとAPIの接続確認だけ行う
# 
# 終了コード 0=完了または対象外の日 / 1=未完了(GitHubから失敗通知メールが届く)
# 



THUMBS = Path(DATA_DIR) / "thumbs"
TODAY = Path(DATA_DIR) / "today.json"


def log(rec, step, status, **detail):
    entry = {"time": datetime.now(JST).isoformat(timespec="seconds"), "step": step, "status": status}
    entry.update(detail)
    rec.setdefault("log", []).append(entry)
    print(f"[{entry['time']}] {step}: {status} " + (json.dumps(detail, ensure_ascii=False)[:600] if detail else ""), flush=True)


def _download(url) -> Image.Image:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def _save_thumb(img, key) -> str:
    THUMBS.mkdir(parents=True, exist_ok=True)
    p = THUMBS / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".jpg")
    im = img.copy()
    im.thumbnail((360, 640))
    im.save(p, quality=85)
    return p.as_posix()


# ------------------------------------------------------------------ 比較対象の準備
def collect_past(rec, history):
    """比較対象 = 9/14以降の営業中ストーリー(過去分) + このプログラムが作った全画像(積み重ね)。"""
    legacy = history.setdefault("legacy_checked", {})
    unconfirmed = []
    todo = [u for u in LEGACY_STORY_URLS
            if not (legacy.get(u) and legacy[u].get("is_open") is not None and "fp" in legacy[u])]
    fetched = []
    for url in todo:
        try:
            fetched.append((url, _download(url)))
        except Exception as e:  # noqa: BLE001
            unconfirmed.append(url)
            log(rec, "過去ストーリー取得", "画像を取得できず", url=url, error=str(e))
    for i in range(0, len(fetched), 9):  # 9枚ずつまとめて1回で判定(無料枠の節約)
        chunk = fetched[i:i + 9]
        try:
            results = classify_stories([img for _, img in chunk])
        except Exception as e:  # noqa: BLE001
            results = [None] * len(chunk)
            log(rec, "過去ストーリー判定", "判定できず(安全側で比較対象に含める)", error=str(e)[:500])
        for (url, img), j in zip(chunk, results):
            if j is None:
                unconfirmed.append(url)
            legacy[url] = {"is_open": (bool(j["is_open_notice"]) if j else None),
                           "description": (j or {}).get("description", ""), "theme": (j or {}).get("theme", ""),
                           "found_text": (j or {}).get("found_text", ""),
                           "fp": fingerprint(img), "thumb": _save_thumb(img, url)}

    past = []
    for url, item in legacy.items():
        if item.get("is_open") is False or "fp" not in item:
            continue
        past.append({"source": "過去", "date": url.rsplit("/", 1)[-1], "theme": item.get("theme", ""), "copy": "",
                     "description": item.get("description", ""), "fp": item["fp"], "thumb": item.get("thumb")})
    for s in history["stories"]:
        past.append({"source": "自動", "date": s["date"], "theme": s.get("theme", ""), "copy": s.get("copy", ""),
                     "description": s.get("description", ""), "fp": s["fp"], "fp_bg": s.get("fp_bg"),
                     "style": s.get("style"), "thumb": s.get("thumb")})
    n_legacy = sum(1 for p in past if p["source"] == "過去")
    log(rec, "比較対象の準備", "完了", 過去の営業中ストーリー=n_legacy,
        営業中以外として除外=sum(1 for v in legacy.values() if v.get("is_open") is False),
        自動作成分=len(history["stories"]), 比較対象の合計=len(past), 取得または判定できなかった数=len(unconfirmed))
    return past


# ------------------------------------------------------------------ 類似チェック
def numeric_similar(bg_fp, final_fp, past):
    for p in past:
        if p.get("fp_bg"):  # 自動作成分: 背景写真どうしを比べる
            d = phash_distance(bg_fp["phash"], p["fp_bg"]["phash"])
            hs = hist_similarity(bg_fp["hist"], p["fp_bg"]["hist"])
        else:  # 過去分: 文字の少ない中央部分どうしを比べる
            d = phash_distance(final_fp["phash_center"], p["fp"]["phash_center"])
            hs = hist_similarity(final_fp["hist_center"], p["fp"]["hist_center"])
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
def produce(rec, today, past, history, seed_base=0, fixed_layout=None, skip_checks=False):
    avoid, image_attempts, plan_attempts = [], 0, 0
    recent_styles = [s.get("style") or {} for s in history["stories"]]
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
        why = None if skip_checks else text_similar(plan, past)
        if why:
            avoid.append(why)
            log(rec, "企画", "FAIL", 理由=why)
            continue

        image_attempts += 1
        bg = generate(plan["image_prompt"])
        style = choose_style(bg, today, recent_styles, seed=today.toordinal() * 10 + seed_base + image_attempts)
        if fixed_layout:
            style["layout"] = fixed_layout
        final, report = compose(bg, today, copy, style)
        errors = check_composition(report, today, copy)
        if errors:
            avoid.append("背景は上部と下部を暗く落ち着いたトーンにし、主役は画面中央に置く")
            log(rec, "必須表示照合", "FAIL", 試行=image_attempts, デザイン=report["style"], 理由=errors)
            continue
        log(rec, "必須表示照合", "PASS", 試行=image_attempts, デザイン=report["style"],
            描画文字=[d["text"] for d in report["drawn"]], コントラスト=[d["contrast"] for d in report["drawn"]])

        bg_fp = fingerprint(_cover(bg, STORY_W, STORY_H))
        fp = fingerprint(final)
        if not skip_checks:
            hit, dist, hs = numeric_similar(bg_fp, fp, past)
            if hit:
                avoid.append(f"過去({hit['date']})の画像と似ている: {hit.get('description', '')}")
                log(rec, "類似チェック(数値)", "FAIL", 試行=image_attempts, 距離=dist, 色類似度=hs, 相手=hit["date"])
                continue
            log(rec, "類似チェック(数値)", "PASS", 試行=image_attempts, 比較件数=len(past))

        thumbs = [] if skip_checks else [Image.open(p["thumb"]) for p in past
                                          if p.get("thumb") and Path(p["thumb"]).exists()][-40:]
        rv = review(final, thumbs)
        same = rv.get("same_items") or []
        if not skip_checks:
            if len(same) >= 2:
                avoid.append(f"過去画像と{'・'.join(same)}が同じだった。{rv.get('similar_reason', '')}")
                log(rec, "類似チェック(項目別)", "FAIL", 試行=image_attempts, 同一項目=same, 理由=rv.get("similar_reason"))
                continue
            log(rec, "類似チェック(項目別)", "PASS", 試行=image_attempts, 比較枚数=len(thumbs), 同一項目=same)
        if int(rv.get("score", 0)) < QUALITY_MIN_SCORE:
            avoid.append(f"品質不足の指摘: {rv.get('problems')}")
            log(rec, "品質審査", "FAIL", 試行=image_attempts, 点数=rv.get("score"), 問題=rv.get("problems"))
            continue
        q = rv
        log(rec, "品質審査", "PASS", 試行=image_attempts, 点数=q.get("score"))
        return plan, copy, final, fp, bg_fp, report["style"]
    raise RuntimeError(f"合格する画像を作れませんでした(画像{image_attempts}回・企画{plan_attempts}回)")


def _publish_file(img_path: Path, sha: str):
    url = public_url(img_path)
    wait_public(url)
    got = hashlib.sha256(requests.get(url, timeout=60).content).hexdigest()
    if got != sha:
        raise RuntimeError("公開URLの画像が最終ファイルと一致しない")
    return url


# ------------------------------------------------------------------ コマンド
def cmd_create():
    now = datetime.now(JST)
    today = now.date()
    rec = {"date": today.isoformat(), "kind": "営業中ストーリー"}
    history = load_history()
    if TODAY.exists():
        t = json.loads(TODAY.read_text(encoding="utf-8"))
        if t.get("date") == today.isoformat() and t.get("status") == "ready":
            print(f"今日の画像は作成済みです: {t['image_url']}")
            return 0
    try:
        bd = check(today)
        log(rec, "営業日判定", "営業日" if bd["business_day"] else "対象外", 理由=bd["reason"], 情報源=bd["sources"])
        if bd["business_day"] is not True:
            today_json = {"date": today.isoformat(), "status": "skip", "reason": bd["reason"]}
            TODAY.write_text(json.dumps(today_json, ensure_ascii=False, indent=1), encoding="utf-8")
            rec["result"] = "対象外の日" if bd["business_day"] is False else "確認不能"
            return 0 if bd["business_day"] is False else 1

        past = collect_past(rec, history)
        plan, copy, final, fp, bg_fp, style = produce(rec, today, past, history)

        img_path = media_path(f"{today.isoformat()}_story.png")
        final.save(img_path, optimize=True)
        sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
        history["stories"].append({
            "date": today.isoformat(), "file": img_path.as_posix(), "theme": plan["theme"], "copy": copy,
            "description": f"{plan['subject']} / {plan['composition']} / {plan['background']} / {plan['palette']}",
            "style": style, "fp": fp, "fp_bg": bg_fp, "thumb": _save_thumb(final, img_path.as_posix())})
        save_history(history)
        commit_and_push(f"story {today}: 画像", [MEDIA_DIR, DATA_DIR])

        url = _publish_file(img_path, sha)
        today_json = {"date": today.isoformat(), "date_text": date_text(today), "status": "ready",
                      "image_url": url, "sha256": sha, "publish_time": f"{PUBLISH_HOUR:02d}:{PUBLISH_MINUTE:02d}",
                      "copy": copy, "design": style, "created_at": now.isoformat(timespec="seconds")}
        TODAY.write_text(json.dumps(today_json, ensure_ascii=False, indent=1), encoding="utf-8")
        log(rec, "画像の保存と公開URL照合", "完了", 公開URL=url, sha256=sha[:16])
        rec["result"] = "画像作成完了(投稿待ち)"
        log(rec, "Geminiの使用回数", "記録", 回数=CALLS)
        print(f"\n今日の画像: {url}\n投稿予定: {date_text(today)} 15:00(Claudeの定時タスクが予約します)\n")
        return 0
    except Exception as e:  # noqa: BLE001
        log(rec, "エラー", "未完了", error=str(e), trace=traceback.format_exc()[-1500:])
        rec["result"] = "未完了"
        return 1
    finally:
        save_run(today, rec)
        save_history(history)
        try:
            commit_and_push(f"story {today}: 記録", [MEDIA_DIR, DATA_DIR])
        except Exception as e:  # noqa: BLE001
            print(f"記録の保存に失敗: {e}")


def cmd_preview():
    """デザイン見本: 背景4枚 × レイアウト2種 = 8枚。Geminiは1回だけ使う。履歴・投稿には使わない。"""
    now = datetime.now(JST)
    today = now.date()
    rec = {"date": today.isoformat(), "kind": "見本"}
    out = Path(MEDIA_DIR) / "preview"
    out.mkdir(parents=True, exist_ok=True)
    made, results = [], []
    plans = plan_variety(today, 4)
    for i, plan in enumerate(plans):
        try:
            bg = generate(plan["image_prompt"])
        except Exception as e:  # noqa: BLE001
            log(rec, "見本の背景生成", "FAIL", 案=plan["theme"], error=str(e)[:300])
            continue
        for k, layout in enumerate(LAYOUTS[i * 2:i * 2 + 2]):
            style = choose_style(bg, today, [], seed=1000 + i * 10 + k)
            style["layout"] = layout
            copy = plan["copy"].strip()
            final, report = compose(bg, today, copy, style)
            errs = check_composition(report, today, copy)
            p = out / f"{i * 2 + k + 1}_{layout}.png"
            final.save(p, optimize=True)
            made.append(p.as_posix())
            results.append({"file": p.name, "theme": plan["theme"], "copy": copy, "design": report["style"],
                            "check": errs or "合格"})
            log(rec, "見本", "作成", ファイル=p.name, 照合=errs or "合格")
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    commit_and_push(f"見本 {today}", [MEDIA_DIR])
    print(f"Geminiの使用回数: {CALLS}")
    return 0 if made else 1


def cmd_selftest():
    ok = True
    now = datetime.now(JST)
    for name, fn in [
        ("営業日判定", lambda: check(now.date())),
        ("Gemini(使えるモデル)", lambda: models()[:6]),
        ("Gemini", lambda: ask_json('{"ok": true} とだけ返してください', required_keys=("ok",)).get("_model")),
        ("Cloudflare 画像生成", lambda: generate("a quiet cocktail glass on a bar counter, night").size),
        ("過去ストーリー画像の取得", lambda: _download(LEGACY_STORY_URLS[-1]).size),
    ]:
        try:
            print(f"OK  {name}: {fn()}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"NG  {name}: {e}")
    return 0 if ok else 1


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"create": cmd_create, "preview": cmd_preview, "selftest": cmd_selftest}.get(cmd)
    if not fn:
        print(__doc__)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
