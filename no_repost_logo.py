"""公式サイトのカード画像に入っている「YU-GI-OH!.jp 無断転載禁止」ロゴの検出と、イラスト部分のぼかし。

背景（docs/decisions.md 2026-10-16）: 公式画像のイラスト右下にロゴが入っているものと無いものがある。
X のリンク画像（OG 画像）では、ロゴが入っている画像のイラスト部分だけをぼかして出す。

検出はテンプレート照合（opencv）。画像の幅を基準寸法（BASE_WIDTH）にそろえてから、
「赤みの強さ」の濃淡図どうしで照合する（背景のイラストの明暗・色に左右されにくくするため）。
テンプレートは id 607（不死なる太陽神）のロゴ部分で、data/no_repost_logo_template.png に置く。
"""
import io
import logging
import os

import cv2
import numpy as np
from PIL import Image, ImageFilter

logger = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(_HERE, "data", "no_repost_logo_template.png")

# 照合の前に画像をこの幅にそろえる（テンプレートは幅407の画像から切り出している）
BASE_WIDTH = 407
# 探索範囲（正規化後の画像に対する割合。イラスト右下付近）
SEARCH_X = (0.30, 1.0)
SEARCH_Y = (0.50, 0.80)
# スケールの違い（画像の枠の余白・解像度の差）に耐えるための倍率
SCALES = (0.88, 0.94, 1.0, 1.06, 1.12)
# この値以上ならロゴありと判定する
MATCH_THRESHOLD = 0.65   # TODO: calibrate from data

# 検出の仕組み（テンプレート・特徴量・探索範囲・倍率）を変えたら上げる。保存済みの判定はこの版が違えば捨てて再計算する。
# 閾値だけの変更なら上げなくてよい（保存済みの score を現行の閾値で判定し直す）
DETECT_VERSION = "1"

_template_feature = None


def _feature(rgb: np.ndarray) -> np.ndarray:
    """赤みの強さ（R - (G+B)/2、0以上）の濃淡図。ロゴの赤い文字を拾い、背景の明暗を落とす。"""
    f = rgb.astype(np.float32)
    red = f[..., 0] - (f[..., 1] + f[..., 2]) / 2.0
    return np.clip(red, 0, 255)


def _get_template_feature():
    global _template_feature
    if _template_feature is None:
        with Image.open(TEMPLATE_PATH) as im:
            _template_feature = _feature(np.asarray(im.convert("RGB")))
    return _template_feature


def _to_pil(image) -> Image.Image:
    if isinstance(image, Image.Image):
        return image.convert("RGB")
    return Image.open(io.BytesIO(image)).convert("RGB")


def is_detected(score) -> bool:
    """保存済みの score を現行の閾値で判定し直す。"""
    return isinstance(score, (int, float)) and not isinstance(score, bool) and score >= MATCH_THRESHOLD


def detect(image) -> tuple[bool, float]:
    """(ロゴありか, 照合スコア)。image は PIL.Image か画像バイト列。スコアは最大の正規化相関（-1〜1）。"""
    im = _to_pil(image)
    w, h = im.size
    nh = round(h * BASE_WIDTH / w)
    im = im.resize((BASE_WIDTH, nh), Image.LANCZOS)
    feat = _feature(np.asarray(im))
    x0, x1 = int(BASE_WIDTH * SEARCH_X[0]), int(BASE_WIDTH * SEARCH_X[1])
    y0, y1 = int(nh * SEARCH_Y[0]), int(nh * SEARCH_Y[1])
    region = feat[y0:y1, x0:x1]
    tpl0 = _get_template_feature()
    best = -1.0
    for s in SCALES:
        tpl = cv2.resize(tpl0, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        if tpl.shape[0] > region.shape[0] or tpl.shape[1] > region.shape[1]:
            continue
        res = cv2.matchTemplate(region, tpl, cv2.TM_CCOEFF_NORMED)
        res = np.nan_to_num(res, nan=-1.0, posinf=-1.0, neginf=-1.0)
        best = max(best, float(res.max()))
    return is_detected(best), round(best, 4)


# ── イラスト部分のぼかし ──
# イラスト枠（暗い縁を含む）の位置。カード画像の幅・高さに対する割合 (左, 上, 右, 下)。
# 実画像（公開中の未発売カード71枚）に重ねて確かめた値。ロゴは枠の下端にかかるため、枠ごと含める。
# TODO: calibrate from data（実画像は通常63・リンク7・ペンデュラム1枚のみ。ペンデュラム・リンクのロゴ入りは未確認）
ART_BOX = {
    "normal": (0.105, 0.168, 0.905, 0.713),     # 通常・効果・儀式・融合・シンクロ・エクシーズ・魔法・罠
    "pendulum": (0.055, 0.168, 0.945, 0.655),   # ペンデュラム（横長の広い枠）
    "link": (0.088, 0.168, 0.948, 0.757),       # リンク（枠が下に長い）
}
# ぼかしの強さ（ガウスぼかしの半径。画像の幅に対する割合）。ロゴの文字が読めなくなり、絵の雰囲気は残る程度
BLUR_RADIUS_RATIO = 0.022   # TODO: calibrate from data


def art_kind(card_type) -> str:
    """card_type（例「ペンデュラム・効果モンスター」）からイラスト枠の種類を決める。不明は通常。"""
    t = card_type or ""
    if "ペンデュラム" in t:
        return "pendulum"
    if "リンク" in t:
        return "link"
    return "normal"


def blur_art(image: Image.Image, card_type=None) -> Image.Image:
    """イラスト部分だけにガウスぼかしをかけたコピーを返す（カード名・枠・効果欄はそのまま）。"""
    im = image.convert("RGB")
    w, h = im.size
    l, t, r, b = ART_BOX[art_kind(card_type)]
    box = (round(w * l), round(h * t), round(w * r), round(h * b))
    region = im.crop(box).filter(ImageFilter.GaussianBlur(w * BLUR_RADIUS_RATIO))
    out = im.copy()
    out.paste(region, box[:2])
    return out
