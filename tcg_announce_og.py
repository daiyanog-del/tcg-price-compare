"""海外先行の予告カードの OG 画像（X などのリンクプレビュー用・1200x630 PNG）。

投稿は画像を添付せず、リンク先のまとめページ（/tcg-preview/<slug>）の OG 画像で見せる（未発売カードの投稿と同じ方式）。
タイムラインで目を止めるため、絵を主役にする（2026-10-29 ユーザー選択「A（商品写真）+B（ゆかりのカードの絵）」）:
  - 弾の画像: 左に商品（ブースターボックス）の写真、右に弾の名前・発売予定・「あのカードゆかりの新カードが判明」と
    ゆかりのカード（宣伝文が Yugipedia でリンクしている既存カード）の絵を最大4枚
  - カードの画像: 左にカード名と効果、右に「ゆかりのカードの絵 ▶ ? の新カード」、右下に商品の写真を小さく
  材料が無いとき（商品の写真がまだ無い・ゆかりのカードが無い）は、その部分を外した形で描く。
新カードの絵は使わない（公式の画像はまだ無く、AI 画像は 2026-10-29 に保留）。

書体は X の値動き投稿の画像（staple_post）と同じ: 和文 M PLUS 2・英字のカード名と数字 Outfit（assets/fonts）。
M PLUS 2 に無い字（★・※・ギリシャ文字など）は Noto Sans CJK（deck_image._get_font）で補う。★は図形で描く。
材料の画像はこのモジュールでは取りに行かない（呼び出し側 tcg_announce_routes が取って PIL 画像で渡す）。
"""
import hashlib
import io
import json
import math
import os
import threading
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import card_og_image as cog
from deck_image import _get_font

W, H = cog.W, cog.H
DESIGN_VERSION = "b7"   # 見た目を変えたら上げる

_TEXT, _SUB, _GOLD = cog._TEXT, cog._SUB, cog._GOLD
_PANEL = (0x20, 0x23, 0x2b)       # --bg-card
_PANEL_LINE = (0x35, 0x38, 0x43)  # --border
_TCG = (0xae, 0xc5, 0xff)         # TCG先行の札（new-cards.css の .is-tcg と同じ色）
_TAG_BG = (0x30, 0x2d, 0x24)      # 予告の札の地（new-cards.css の未発売の札と同じ）
_STRIP = (0x1d, 0x1f, 0x26)
_LEFT = 64

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
_JP_PATH = os.path.join(_FONT_DIR, "MPLUS2.ttf")
_LATIN_PATH = os.path.join(_FONT_DIR, "Outfit.ttf")

_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_MAX = 64
_font_cache: dict = {}
_font_lock = threading.Lock()


# ──────────────────────────────────────────────
# 書体（無い字は Noto Sans CJK で補う）
# ──────────────────────────────────────────────

def _var_font(path: str, size: int, weight: int):
    key = (path, size, weight)
    with _font_lock:
        if key in _font_cache:
            return _font_cache[key]
    f = ImageFont.truetype(path, size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass  # 可変でない書体はそのまま
    with _font_lock:
        _font_cache[key] = f
    return f


def jp(size: int, bold: bool = False):
    """和文（M PLUS 2）。無ければ Noto Sans CJK。"""
    if os.path.isfile(_JP_PATH):
        return _var_font(_JP_PATH, size, 800 if bold else 500)
    return _get_font(size, bold)


def latin(size: int, bold: bool = False):
    """英字のカード名・数字（Outfit）。無ければ和文の書体。"""
    if os.path.isfile(_LATIN_PATH):
        return _var_font(_LATIN_PATH, size, 700 if bold else 500)
    return jp(size, bold)


_missing_cache: dict = {}


def _has_glyph(font, ch: str) -> bool:
    """その書体に字があるか。描いた形が「無い字の印（豆腐）」と同じなら無い（fontTools を使わず Pillow だけで見る）。"""
    key = (id(font), ch)
    if key in _missing_cache:
        return _missing_cache[key]
    if ch.isspace() and ch != "　":
        ok = True
    else:
        mask = font.getmask(ch)
        tofu = font.getmask("\U0010fffd")
        ok = not (mask.size == tofu.size and bytes(mask) == bytes(tofu))
    _missing_cache[key] = ok
    return ok


def _runs(text: str, font, size: int, bold: bool):
    """字ごとに、書体に無ければ予備（Noto Sans CJK）に分けた (文字列, 書体) の並び。"""
    fb = _get_font(size, bold)
    runs: list[list] = []
    for ch in text:
        f = font if _has_glyph(font, ch) else fb
        if runs and runs[-1][1] is f:
            runs[-1][0] += ch
        else:
            runs.append([ch, f])
    return runs


def text_width(d, text: str, font, size: int, bold: bool) -> float:
    return sum(d.textlength(t, font=f) for t, f in _runs(text, font, size, bold))


def draw_text(d, xy, text: str, fill, *, size: int, bold: bool = False, kind: str = "jp", anchor: str = "lm") -> float:
    """無い字を補いながら1行を描く。anchor は l/m/r と m（縦の中央）の組み合わせだけ。戻り値は幅。"""
    font = (latin if kind == "latin" else jp)(size, bold)
    runs = _runs(text, font, size, bold)
    width = sum(d.textlength(t, font=f) for t, f in runs)
    x, y = xy
    if anchor[0] == "m":
        x -= width / 2
    elif anchor[0] == "r":
        x -= width
    for t, f in runs:
        d.text((x, y), t, fill=fill, font=f, anchor="lm")
        x += d.textlength(t, font=f)
    return width


def wrap(d, text: str, max_w: float, max_lines: int, *, size: int, bold: bool = False, kind: str = "jp") -> list[str]:
    """1字ずつ詰めて折り返す。行頭に置かない字（card_og_image と同じ一覧）は前の行に付ける。溢れたら「…」で切る。"""
    font = (latin if kind == "latin" else jp)(size, bold)
    lines, cur = [], ""
    for ch in text:
        if text_width(d, cur + ch, font, size, bold) > max_w and cur and ch not in cog._NO_LINE_START:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
        else:
            cur += ch
    else:
        if cur:
            lines.append(cur)
        # 最後の行が数字だけになる折り返し（「装備す／る。」）は、前の行から字を回して避ける
        if len(lines) >= 2 and len(lines[-1]) < cog._MIN_LAST_LINE_CHARS:
            prev = lines[-2]
            k = cog._MIN_LAST_LINE_CHARS - len(lines[-1])
            while k < len(prev) - 1 and prev[-k] in cog._NO_LINE_START:
                k += 1  # 回した先の行頭が「。」などにならないよう、もう1字回す
            if len(prev) > k:
                lines[-2], lines[-1] = prev[:-k], prev[-k:] + lines[-1]
        return lines
    last = lines[-1]
    while last and text_width(d, last + "…", font, size, bold) > max_w:
        last = last[:-1]
    lines[-1] = last + "…"
    return lines


def wrap_words(d, text: str, max_w: float, max_lines: int, *, size: int, bold: bool = True) -> list[str]:
    """英語のカード名を単語の切れ目で折り返す（1語が長すぎるときだけ字で切る）。"""
    font = latin(size, bold)
    lines, cur = [], ""
    for w in text.split():
        cand = (cur + " " + w).strip()
        if text_width(d, cand, font, size, bold) <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip() + "…"
    return lines


def star(d, cx: float, cy: float, r: float, fill) -> None:
    """★を図形で描く（M PLUS 2 に ★ の字が無いため）。"""
    pts = []
    for i in range(10):
        rr = r if i % 2 == 0 else r * 0.45
        a = -math.pi / 2 + i * math.pi / 5
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=fill)


# ──────────────────────────────────────────────
# 文字の組み立て
# ──────────────────────────────────────────────

def format_release(iso: str | None) -> str:
    """「2027/3/19」（ページの数字の並びに使う）。"""
    if not iso:
        return ""
    y, m, d = str(iso)[:10].split("-")
    return f"{int(y)}/{int(m)}/{int(d)}"


def format_release_ja(iso: str | None) -> str:
    """「2027年3月19日」（画像・投稿の文に使う）。"""
    if not iso:
        return ""
    y, m, d = str(iso)[:10].split("-")
    return f"{int(y)}年{int(m)}月{int(d)}日"


def kind_label(card: dict) -> str:
    """「闇属性・機械族・融合」「永続罠」のような種類の言葉（分からない欄は出さない）。"""
    ctype = card.get("card_type") or ""
    if ctype.endswith("モンスター"):
        kinds = [k for k in ctype.removesuffix("モンスター").split("・") if k and k not in ("効果", "通常")]
        attr = f"{card['attribute']}属性" if card.get("attribute") else ""
        return "・".join(x for x in [attr, card.get("race") or ""] + kinds if x)
    return ctype


def level_text(card: dict) -> tuple[str, str]:
    """(印, 数): レベルは ("★", "8")、ランクは ("☆", "4")、リンクは ("LINK-", "2")。無ければ ("", "")。"""
    for key, mark in (("level", "★"), ("rank", "☆"), ("link_val", "LINK-")):
        if card.get(key):
            return mark, str(card[key])
    return "", ""


def atk_def(card: dict) -> str:
    atk, df = card.get("atk") or "", card.get("def") or ""
    if not (atk or df):
        return ""
    if card.get("link_val"):
        return atk or "?"
    return f"{atk or '?'} / {df or '?'}"


def stats_line(card: dict) -> str:
    """ページ・投稿の本文用の1行「闇属性・機械族・融合　★8　3000 / 2200」。"""
    mark, num = level_text(card)
    parts = [kind_label(card), f"{mark}{num}" if mark else "", atk_def(card)]
    return "　".join(p for p in parts if p)


def headline(card: dict, set_name: str = "") -> str:
    """カードの見出し（画像・ページ・投稿で共通）。2026-10-29 ユーザー合意の4つの出し分け:
    ゆかりのカード（宣伝文が名指しする既存カード）→ 既存テーマの一員・サポート → 英語版が先に作った新テーマ → 弾の名前。"""
    if card.get("related_ja"):
        return f"《{card['related_ja'][0]}》ゆかりの新カード"
    kind = card.get("theme_kind") or ""
    theme = card.get("theme_ja") or (f"「{card['theme_en']}」" if card.get("theme_en") else "")
    if kind == "member" and theme:
        return f"【{card['theme_ja']}】の新規カード" if card.get("theme_ja") else f"{theme}の新規カード"
    if kind == "support" and theme:
        return f"【{card['theme_ja']}】をサポートする新カード" if card.get("theme_ja") else f"{theme}をサポートする新カード"
    if kind == "new_theme" and card.get("theme_en"):
        return f"新テーマ「{card['theme_en']}」の新カード"
    return f"「{set_name}」の新カード" if set_name else "英語版の新カード"


def set_headline(cards: list[dict]) -> str:
    """弾の画像の見出し（既存カードの絵を並べるとき）。ゆかりのカードがあれば「あのカード」、テーマだけならテーマ名。"""
    if any(c.get("related_ja") for c in cards):
        return "あのカードゆかりの新カードが判明"
    themes = [c["theme_ja"] for c in cards if c.get("theme_kind") in ("member", "support") and c.get("theme_ja")]
    if themes:
        first = themes[0]
        return f"【{first}】の新規カードが判明" if len(set(themes)) == 1 else f"【{first}】などの新規カードが判明"
    new = [c["theme_en"] for c in cards if c.get("theme_kind") == "new_theme" and c.get("theme_en")]
    if new:
        return f"新テーマ「{new[0]}」の新カードが判明"
    return "新カードの効果が判明"


def picture_card(card: dict) -> str:
    """画像に出す既存カード（ゆかりのカード → テーマの代表カード）。無ければ空文字。"""
    if card.get("related_ja"):
        return card["related_ja"][0]
    if card.get("theme_kind") in ("member", "support", "new_theme"):
        return card.get("theme_card_ja") or ""   # 新テーマでも一員の既存カードがあれば（Heavy Metal → 鋼鉄の襲撃者）
    return ""


def summary_of(card: dict) -> dict:
    """要約を {intro, effects: [{label, text}]} で返す（2026-10-29 の作り直し前の「文の配列」も読む）。"""
    s = card.get("summary_ja") or {}
    if isinstance(s, list):
        return {"intro": "", "effects": [{"label": "", "text": t} for t in s]}
    return {"intro": s.get("intro") or "", "effects": [e for e in s.get("effects") or [] if e.get("text")]}


# ──────────────────────────────────────────────
# 部品
# ──────────────────────────────────────────────

def _background() -> Image.Image:
    return cog._background()


def _glow(canvas: Image.Image, cx, cy, rx, ry, strength: float) -> None:
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    a = strength * np.exp(-(((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2) * 2.2)
    layer = np.zeros((H, W, 4), np.uint8)
    layer[..., :3] = _GOLD
    layer[..., 3] = np.clip(a * 255, 0, 255).astype(np.uint8)
    canvas.alpha_composite(Image.fromarray(layer, "RGBA"))


def _shadowed(canvas: Image.Image, img: Image.Image, xy, blur=14, off=8, alpha=160) -> None:
    sh = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    m = img.split()[-1].point(lambda v: alpha if v > 0 else 0)
    sh.paste((0, 0, 0, 255), (int(xy[0]), int(xy[1]) + off), m)
    canvas.alpha_composite(sh.filter(ImageFilter.GaussianBlur(blur)))
    canvas.alpha_composite(img, (int(xy[0]), int(xy[1])))


def cutout(img: Image.Image) -> Image.Image:
    """商品写真の白い背景を透明にして、余白を切る（商品写真は白地のことが多い）。"""
    im = img.convert("RGBA")
    a = np.array(im)
    white = a[..., :3].min(axis=2) > 235
    alpha = np.where(white, 0, a[..., 3]).astype(np.uint8)
    im.putalpha(Image.fromarray(alpha).filter(ImageFilter.GaussianBlur(1)))
    box = im.getbbox()
    return im.crop(box) if box else im


def _fit_w(img: Image.Image, w: int) -> Image.Image:
    return img.resize((w, max(1, round(img.height * w / img.width))), Image.LANCZOS)


def _fit_h(img: Image.Image, h: int) -> Image.Image:
    return img.convert("RGBA").resize((max(1, round(img.width * h / img.height)), h), Image.LANCZOS)


def _logo(canvas: Image.Image, x: int, y: int, h: int) -> None:
    try:
        logo = Image.open(cog._LOGO_PATH).convert("RGBA")
        canvas.alpha_composite(_fit_h(logo, h), (x, y))
    except Exception:
        d = ImageDraw.Draw(canvas)
        draw_text(d, (x, y + h / 2), "GREEDY LAB", _TEXT, size=round(h * 0.7), bold=True, kind="latin")


def _badges(d, x: int, y: int, labels: list[tuple[str, tuple]], size: int = 21) -> int:
    for text, color in labels:
        w = text_width(d, text, jp(size, True), size, True) + 24
        d.rounded_rectangle([x, y, x + w, y + size + 18], radius=7, outline=color, width=2)
        draw_text(d, (x + w / 2, y + (size + 18) / 2), text, color, size=size, bold=True, anchor="mm")
        x += w + 10
    return x


def _proxy(w: int, h: int, card: dict) -> Image.Image:
    """新カードの札（絵の代わり）。金の枠・属性種族・レベルの星・?・攻守。"""
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=12, fill=_PANEL, outline=_GOLD, width=3)
    head = "・".join(x for x in (card.get("attribute"), card.get("race")) if x) or (card.get("card_type") or "")
    draw_text(d, (w / 2, 34), head, _SUB, size=20, bold=True, anchor="mm")
    mark, num = level_text(card)
    if mark in ("★", "☆") and num.isdigit():
        n = min(int(num), 12)
        step = min(22, (w - 40) / max(n, 1))
        for i in range(n):
            star(d, w / 2 + (i - (n - 1) / 2) * step, 68, min(9, step * 0.42), _GOLD)
    d.text((w / 2, h / 2 + 4), "?", fill=_GOLD, font=latin(130, True), anchor="mm")
    draw_text(d, (w / 2, h / 2 + 92), "イラスト未公開", _SUB, size=17, anchor="mm")
    nums = atk_def(card)
    if nums:
        draw_text(d, (w / 2, h - 34), nums, _TEXT, size=30, bold=True, kind="latin", anchor="mm")
    return im


def _png(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# ──────────────────────────────────────────────
# 弾の画像
# ──────────────────────────────────────────────

def render_set_png(s: dict, cards: list[dict], *, box: Image.Image | None = None,
                   related: list[tuple[str, Image.Image]] | None = None) -> bytes:
    """弾のまとめの画像。box は商品写真、related は（ゆかりのカード名, 絵）を最大4枚。"""
    related = (related or [])[:4]
    canvas = _background()
    if box is not None:
        _glow(canvas, 300, 330, 330, 300, 0.22)
        b = _fit_w(cutout(box), 500)
        if b.height > 540:
            b = _fit_h(b, 540)
        _shadowed(canvas, b, (40 + (500 - b.width) // 2, 330 - b.height // 2 + 10), blur=16, off=10, alpha=170)
        x0, right = 580, W - 48
    else:
        x0, right = _LEFT, W - _LEFT
    d = ImageDraw.Draw(canvas)
    _badges(d, x0, 56, [("TCG先行", _TCG), ("予告", _GOLD)])
    name = s.get("name_en") or ""
    size = 56
    while text_width(d, name, latin(size, True), size, True) > right - x0 and size > 34:
        size -= 4
    draw_text(d, (x0, 136), name, _TEXT, size=size, bold=True, kind="latin")
    sub = "遊戯王TCG（英語版）"
    if s.get("tcg_release_date"):
        sub += f"　{format_release_ja(s['tcg_release_date'])}発売予定"
    draw_text(d, (x0, 186), sub, _SUB, size=24)

    if related:
        draw_text(d, (x0, 242), set_headline(cards), _GOLD, size=36, bold=True)
        ch = 190   # 4枚が右の余白に収まる高さ（196 だと4枚目が数px はみ出して落ちた）
        x = x0
        for _, img in related:
            im = _fit_h(img, ch)
            if x + im.width > right:
                break
            _shadowed(canvas, im, (x, 284), blur=8, off=6, alpha=150)
            x += im.width + 12
        y_note = 516
    else:
        # ゆかりのカードが無い弾は、判明したカード名を2列×2段で並べる
        draw_text(d, (x0, 242), "新カードの効果が判明", _GOLD, size=36, bold=True)
        cell_w = (right - x0 - 16) // 2
        for i, c in enumerate(cards[:4]):
            cx, cy = x0 + (i % 2) * (cell_w + 16), 290 + (i // 2) * 104
            d.rounded_rectangle([cx, cy, cx + cell_w, cy + 90], radius=12, fill=_PANEL, outline=_PANEL_LINE, width=2)
            nm = wrap_words(d, c.get("name_en") or "", cell_w - 36, 1, size=28)[0]
            draw_text(d, (cx + 18, cy + 32), nm, _TEXT, size=28, bold=True, kind="latin")
            draw_text(d, (cx + 18, cy + 66), kind_label(c), _SUB, size=19)
        y_note = 516
    # 名前だけ判明したカードは数えない（効果を紹介できている枚数）
    n = len([c for c in cards if summary_of(c)["effects"]])
    if n:
        draw_text(d, (x0, y_note), f"判明した{n}枚の効果を日本語で紹介しています", _TEXT, size=23)
    _logo(canvas, W - 200, 572, 34)
    return _png(canvas)


# ──────────────────────────────────────────────
# カードの画像
# ──────────────────────────────────────────────

def render_card_png(s: dict, card: dict, others: list[str], *, box: Image.Image | None = None,
                    related: tuple[str, Image.Image] | None = None) -> bytes:
    """カードごとの画像。others は同じ弾で判明した他のカード名、related は（ゆかりのカード名, 絵）。"""
    canvas = _background()
    _glow(canvas, 860, 300, 360, 300, 0.18)
    d = ImageDraw.Draw(canvas)
    # ゆかりのカードの絵があるときは右に絵と札が並ぶので狭く、無いときは札だけなので広く使う
    x0, text_w = _LEFT, (528 if related else 690)
    head = "効果が判明" if card.get("lore_kind") == "blurb" else "英語のテキストが判明"
    _badges(d, x0, 44, [("TCG先行", _TCG), (head, _GOLD)])

    y = 112
    head = headline(card, s.get("name_en") or "")
    fit = next((sz for sz in (28, 26, 24) if text_width(d, head, jp(sz, True), sz, True) <= text_w), None)
    if fit:
        draw_text(d, (x0, y), head, _GOLD, size=fit, bold=True)   # 1行に収まるなら1行（効果の場所を空ける）
        y += 54
    else:
        for ln in wrap(d, head, text_w, 2, size=26, bold=True):
            draw_text(d, (x0, y), ln, _GOLD, size=26, bold=True)
            y += 34
        y += 20
    for ln in wrap_words(d, card.get("name_en") or "", text_w, 2, size=50):
        draw_text(d, (x0, y), ln, _TEXT, size=50, bold=True, kind="latin")
        y += 52
    # 種類・レベル・攻守（★は図形）
    kl = kind_label(card)
    x = x0 + draw_text(d, (x0, y + 4), kl, _SUB, size=22) if kl else x0
    mark, num = level_text(card)
    if mark:
        x += 18
        if mark == "★":
            star(d, x + 9, y + 5, 9, _SUB)
            x += 22
        else:
            x += draw_text(d, (x, y + 4), mark, _SUB, size=22)
        x += draw_text(d, (x, y + 4), num, _SUB, size=24, kind="latin")
    nums = atk_def(card)
    if nums:
        draw_text(d, (x + 20, y + 4), nums, _SUB, size=24, kind="latin")
    y += 50

    # 効果: 共通の説明 → 効果ごとの行（名前があれば札）。入らない分は「…」
    summ = summary_of(card)
    fs, lh = 20, 29          # 効果の字の大きさと行の間隔
    bottom = 546             # 効果の文字を置ける下の端（その下に注記）
    if summ["intro"]:
        for ln in wrap(d, summ["intro"], text_w, 3, size=fs):
            draw_text(d, (x0, y), ln, _TEXT, size=fs)
            y += lh
        y += 6
    for e in summ["effects"]:
        if y + lh / 2 > bottom:
            break
        label = (e.get("label") or "").strip()
        tx = x0
        if label:
            lw = max(text_width(d, label, latin(16, True), 16, True) + 20, 40)
            d.rounded_rectangle([x0, y - 14, x0 + lw, y + 14], radius=6, fill=_TAG_BG)
            draw_text(d, (x0 + lw / 2, y), label, _GOLD, size=16, bold=True, kind="latin", anchor="mm")
            tx = x0 + lw + 12
        else:
            d.ellipse([x0 + 4, y - 4, x0 + 12, y + 4], fill=_GOLD)
            tx = x0 + 22
        # この行の後に置ける行数（1行目の中心から下の端まで）。入らない分は「…」
        room = max(1, min(2, int((bottom - (y + lh / 2)) // lh) + 1))
        for ln in wrap(d, e["text"], text_w - (tx - x0), room, size=fs):
            draw_text(d, (tx, y), ln, _TEXT, size=fs)
            y += lh
        y += 4
    note = {"blurb": "販売店の商品説明をもとにした非公式の要約です",
            "official": "英語のカードテキストをもとにした要約です"}.get(card.get("lore_kind"), "")
    if note:
        draw_text(d, (x0, 562), note, _SUB, size=16)

    # 右: ゆかりのカードの絵 ▶ ? の新カード（無ければ ? の札だけを大きく）
    if related:
        old = _fit_h(related[1], 350)
        _shadowed(canvas, old, (610, 48))
        draw_text(d, (610 + old.width / 2, 422), f"《{related[0]}》", _SUB, size=19, anchor="mm")
        ax = 610 + old.width + 30
        d.polygon([(ax - 12, 205), (ax + 14, 223), (ax - 12, 241)], fill=_GOLD)
        px = 610 + old.width + 62
        p = _proxy(min(241, W - 40 - px), 350, card)
        _shadowed(canvas, p, (px, 48))
        draw_text(d, (px + p.width / 2, 422), "英語版の新カード", _GOLD, size=19, bold=True, anchor="mm")
    else:
        p = _proxy(270, 370, card)
        _shadowed(canvas, p, (790, 40))
        draw_text(d, (790 + 135, 436), "英語版の新カード", _GOLD, size=19, bold=True, anchor="mm")
    # 右下: 収録商品（写真があれば小さく添える）
    tx = W - 44
    if box is not None:
        b = _fit_w(cutout(box), 118)
        _shadowed(canvas, b, (W - b.width - 44, 458), blur=6, off=4)
        tx = W - 44 - b.width - 14
    draw_text(d, (tx, 488), f"収録：{s.get('name_en') or ''}", _SUB, size=18, anchor="rm")
    if s.get("tcg_release_date"):
        draw_text(d, (tx, 516), f"{format_release_ja(s['tcg_release_date'])}発売予定", _SUB, size=18, anchor="rm")

    # 下の帯: 同じパックで判明した他のカード
    d.rectangle([0, 578, W, H - 8], fill=_STRIP)
    if others:
        shown = " ／ ".join(others[:3]) + (f" ほか{len(others) - 3}枚" if len(others) > 3 else "")
        line = wrap(d, "同じパックで判明：" + shown, W - x0 * 2, 1, size=19)[0]
        draw_text(d, (x0, 600), line, _SUB, size=19)
    return _png(canvas)


# ──────────────────────────────────────────────
# キャッシュ
# ──────────────────────────────────────────────

def _key(kind: str, payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return f"{kind}:{DESIGN_VERSION}:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cached(key: str, render):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    png = render()
    with _cache_lock:
        _cache[key] = png
        while len(_cache) > _CACHE_MAX:
            _cache.popitem(last=False)
    return png


_CARD_KEYS = ("name_en", "attribute", "race", "card_type", "level", "rank", "link_val", "atk", "def",
              "summary_ja", "lore_kind", "related_ja", "theme_kind", "theme_en", "theme_ja", "theme_card_ja")


def set_png(s: dict, cards: list[dict], *, box=None, box_key: str = "", related=None, related_keys=()) -> bytes:
    """box_key・related_keys は材料の画像の URL（キャッシュの鍵に入れて、差し替えで作り直す）。"""
    payload = [s, [{k: c.get(k) for k in _CARD_KEYS} for c in cards], box_key, list(related_keys)]
    return _cached(_key("set", payload), lambda: render_set_png(s, cards, box=box, related=related))


def card_png(s: dict, card: dict, others: list[str], *, box=None, box_key: str = "", related=None,
             related_key: str = "") -> bytes:
    payload = [s.get("name_en"), s.get("tcg_release_date"), {k: card.get(k) for k in _CARD_KEYS}, others,
               box_key, related_key]
    return _cached(_key("card", payload), lambda: render_card_png(s, card, others, box=box, related=related))
