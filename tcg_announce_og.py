"""海外先行の予告カードの OG 画像（X などのリンクプレビュー用・1200x630 PNG）。

投稿は画像を添付せず、リンク先のまとめページ（/tcg-preview/<slug>）の OG 画像で見せる（未発売カードの投稿と同じ方式）。
  - 弾の画像: 弾の名前・英語版の発売予定・判明したカード（最大4枚）を並べる
  - カードの画像: 左にカード名・種類・効果の概要、右に予告カードの札（属性・レベル・攻守。画像は未公開）、
    下の帯に同じ弾で判明した他のカード名

カードの絵は使わない（公式の画像はまだ無く、AI 画像は 2026-10-29 に保留）。配色・下地・書体は card_og_image と同じ。
生成結果はメモリに少しだけ持つ（キーに中身のハッシュを含めるので、更新されたら作り直す）。
"""
import hashlib
import io
import json
import threading
from collections import OrderedDict

from PIL import Image, ImageDraw

import card_og_image as cog
from deck_image import _get_font

W, H = cog.W, cog.H
DESIGN_VERSION = "a3"   # 見た目を変えたら上げる

_TEXT, _SUB, _GOLD, _BTN_FG = cog._TEXT, cog._SUB, cog._GOLD, cog._BTN_FG
_PANEL = (0x20, 0x23, 0x2b)      # --bg-card
_PANEL_LINE = (0x35, 0x38, 0x43)  # --border
_TCG = (0xae, 0xc5, 0xff)        # TCG先行の札（new-cards.css の .is-tcg と同じ色）
_LEFT = 72

_cache: "OrderedDict[str, bytes]" = OrderedDict()
_cache_lock = threading.Lock()
_CACHE_MAX = 64


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


def format_release(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = str(iso)[:10].split("-")
    return f"{int(y)}/{int(m)}/{int(d)}"


def stats_line(card: dict) -> str:
    """「闇・機械族｜融合 ★8　3000 / 2200」のような1行（分からない欄は出さない）。"""
    head = "・".join(x for x in (card.get("attribute"), card.get("race")) if x)
    ctype = card.get("card_type") or ""
    kind = ctype.removesuffix("モンスター").replace("・効果", "").removesuffix("効果") if ctype.endswith("モンスター") else ctype
    stars = ""
    for key, mark in (("level", "★"), ("rank", "☆"), ("link_val", "LINK-")):
        if card.get(key):
            stars = f"{mark}{card[key]}"
            break
    left = "｜".join(x for x in (head, " ".join(x for x in (kind, stars) if x)) if x)
    atk, df = card.get("atk") or "", card.get("def") or ""
    if atk or df:
        nums = f"{atk or '?'} / {df or '?'}" if card.get("def") not in (None, "") or not card.get("link_val") else f"{atk}"
        return f"{left}　{nums}" if left else nums
    return left


def _background() -> Image.Image:
    return cog._background()


def _logo(canvas: Image.Image, d: ImageDraw.ImageDraw, y: int = 64) -> None:
    try:
        logo = Image.open(cog._LOGO_PATH).convert("RGBA")
        lh = 48
        logo = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
        canvas.alpha_composite(logo, (_LEFT, y))
    except Exception:
        d.text((_LEFT, y + 24), "GREEDY LAB", fill=_TEXT, font=_get_font(34, True), anchor="lm")


def _badges(d: ImageDraw.ImageDraw, x: int, y: int, labels: list[tuple[str, tuple]], size: int = 22) -> int:
    """小さな札を横に並べる。戻り値は右端の x。"""
    f = _get_font(size, True)
    for text, color in labels:
        w = d.textlength(text, font=f) + 24
        d.rounded_rectangle([x, y, x + w, y + size + 16], radius=6, outline=color, width=2)
        d.text((x + w / 2, y + (size + 16) / 2), text, fill=color, font=f, anchor="mm")
        x += w + 10
    return x


def _wrap(d, text: str, font, max_w: float, max_lines: int) -> list[str]:
    """1字ずつ詰めて折り返す（日本語向け）。溢れたら最後の行を「…」で切る。
    行の頭に置かない文字（「。」「、」小書き文字など。card_og_image と同じ一覧）は、少しはみ出しても前の行に付ける。"""
    lines, cur = [], ""
    for ch in text:
        if d.textlength(cur + ch, font=font) > max_w and cur and ch not in cog._NO_LINE_START:
            lines.append(cur)
            cur = ch
            if len(lines) == max_lines:
                break
        else:
            cur += ch
    else:
        if cur:
            lines.append(cur)
        return lines
    last = lines[-1]
    while last and d.textlength(last + "…", font=font) > max_w:
        last = last[:-1]
    lines[-1] = last + "…"
    return lines


def _mini_card(d, x: int, y: int, w: int, h: int, card: dict) -> None:
    """予告カードの札（カードの絵の代わり）。"""
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=_PANEL, outline=_PANEL_LINE, width=2)
    cx = x + w / 2
    f_small = _get_font(22, True)
    head = "・".join(v for v in (card.get("attribute"), card.get("race")) if v) or (card.get("card_type") or "")
    d.text((cx, y + 40), head, fill=_SUB, font=f_small, anchor="mm")
    lv = card.get("level") or card.get("rank")
    if lv:
        d.text((cx, y + 80), ("★" if card.get("level") else "☆") * min(int(lv), 12), fill=_GOLD,
               font=_get_font(22 if int(lv) <= 8 else 17, True), anchor="mm")
    d.text((cx, y + h / 2 + 10), "?", fill=_PANEL_LINE, font=_get_font(120, True), anchor="mm")
    d.text((cx, y + h / 2 + 90), "画像は未公開", fill=_SUB, font=_get_font(20), anchor="mm")
    if card.get("atk") or card.get("def"):
        d.text((cx, y + h - 36), f"{card.get('atk') or '?'} / {card.get('def') or '?'}", fill=_TEXT,
               font=_get_font(28, True), anchor="mm")


def render_set_png(s: dict, cards: list[dict]) -> bytes:
    """弾のまとめの画像。cards は表示順（効果の分かるカードを先に）。"""
    canvas = _background()
    d = ImageDraw.Draw(canvas)
    _logo(canvas, d)
    _badges(d, _LEFT, 132, [("TCG先行", _TCG), ("予告", _GOLD)])
    name_font = _get_font(60, True)
    name = s.get("name_en") or ""
    while d.textlength(name, font=name_font) > W - _LEFT * 2 and name_font.size > 36:
        name_font = _get_font(name_font.size - 4, True)
    d.text((_LEFT, 222), name, fill=_TEXT, font=name_font, anchor="lm")
    sub = "遊戯王TCG 英語版"
    if s.get("tcg_release_date"):
        sub += f" {format_release(s['tcg_release_date'])} 発売予定"
    sub += f"｜判明 {len(cards)} 枚" + (f" / 収録 {s['size']} 枚" if s.get("size") else "")
    d.text((_LEFT, 286), sub, fill=_SUB, font=_get_font(28), anchor="lm")

    # 判明したカードを 2列×2段（カード名が折れないよう4列にはしない）
    cell_w, cell_h, gap = (W - _LEFT * 2 - 24) // 2, 120, 24
    nf, sf = _get_font(30, True), _get_font(22)
    for i, c in enumerate(cards[:4]):
        cx = _LEFT + (i % 2) * (cell_w + gap)
        cy = 330 + (i // 2) * (cell_h + 18)
        d.rounded_rectangle([cx, cy, cx + cell_w, cy + cell_h], radius=12, fill=_PANEL, outline=_PANEL_LINE, width=2)
        nm = c.get("name_en") or ""
        while d.textlength(nm, font=nf) > cell_w - 40 and len(nm) > 4:
            nm = nm[:-2] + "…" if not nm.endswith("…") else nm[:-2] + "…"
        d.text((cx + 20, cy + 40), nm, fill=_TEXT, font=nf, anchor="lm")
        d.text((cx + 20, cy + 86), cog._ellipsize(d, stats_line(c), sf, cell_w - 40), fill=_SUB, font=sf, anchor="lm")
    if len(cards) > 4:
        d.text((W - _LEFT, 610 - 18), f"ほか {len(cards) - 4} 枚", fill=_SUB, font=_get_font(22), anchor="rm")
    return _png(canvas)


def render_card_png(s: dict, card: dict, others: list[str]) -> bytes:
    """カードごとの画像。others は同じ弾で判明した他のカード名。"""
    canvas = _background()
    d = ImageDraw.Draw(canvas)
    _logo(canvas, d, y=56)
    text_w = 700
    x = _badges(d, _LEFT, 124, [("TCG先行", _TCG), ("予告", _GOLD)])
    d.text((x + 6, 143), s.get("name_en") or "", fill=_SUB, font=_get_font(22), anchor="lm")

    nf = _get_font(52, True)
    name_lines = _wrap(d, card.get("name_en") or "", nf, text_w, 2)
    y = 200
    for ln in name_lines:
        d.text((_LEFT, y), ln, fill=_TEXT, font=nf, anchor="lm")
        y += 62
    d.text((_LEFT, y + 2), cog._ellipsize(d, stats_line(card), _get_font(26), text_w), fill=_SUB,
           font=_get_font(26), anchor="lm")
    y += 50

    # 投稿用に作った1〜2文（summary_short_ja）を載せる。箇条書きの先頭数行だと、肝心の効果が後ろの行にあって切れるため
    lf = _get_font(28)
    text = card.get("summary_short_ja") or "".join(card.get("summary_ja") or [])
    if not text:
        text = "効果は未公開です。判明したら更新します。"
    for wl in _wrap(d, text, lf, text_w, 4 if len(name_lines) == 1 else 3):
        d.text((_LEFT, y + 6), wl, fill=_TEXT, font=lf, anchor="lm")
        y += 44
    note = {"blurb": "販売店の説明をもとにした非公式の概要",
            "official": "英語のカードテキストをもとにした概要"}.get(card.get("lore_kind"), "")
    if note:
        d.text((_LEFT, 548), note, fill=_SUB, font=_get_font(20), anchor="lm")

    _mini_card(d, 830, 56, 300, 440, card)

    # 下の帯: 同じ弾で判明した他のカード
    d.rectangle([0, 578, W, H - 8], fill=(0x1d, 0x1f, 0x26))
    if others:
        shown = others[:3]
        tail = f" ほか {len(others) - 3} 枚" if len(others) > 3 else ""
        strip = "同じ弾で判明：" + " ／ ".join(shown) + tail
        d.text((_LEFT, 600), cog._ellipsize(d, strip, _get_font(20), W - _LEFT * 2), fill=_SUB,
               font=_get_font(20), anchor="lm")
    return _png(canvas)


def _png(canvas: Image.Image) -> bytes:
    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def set_png(s: dict, cards: list[dict]) -> bytes:
    payload = [s, [{k: c.get(k) for k in ("name_en", "attribute", "race", "card_type", "level", "rank", "link_val",
                                         "atk", "def")} for c in cards]]
    return _cached(_key("set", payload), lambda: render_set_png(s, cards))


def card_png(s: dict, card: dict, others: list[str]) -> bytes:
    return _cached(_key("card", [s.get("name_en"), card, others]), lambda: render_card_png(s, card, others))
