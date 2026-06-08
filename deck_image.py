"""
デッキ画像生成モジュール
========================
Pillow を使用してデッキリストのカード画像グリッドを合成し、
デッキ名・合計額・フッターを付けた PNG を生成する。

export:
    generate_deck_image(deck_name, cards, total, site_url) -> bytes
"""

import os
import io
import hashlib
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from PIL import Image, ImageDraw, ImageFont

# ── レイアウト定数 ──
_CARD_W = 100          # カード1枚の幅（px）
_CARD_H = 140          # カード1枚の高さ（px）
_COLS = 10             # 1行あたりの最大枚数
_GAP = 5               # カード間の隙間
_MARGIN = 10           # キャンバス外周の余白
_HEADER_H = 74         # ヘッダー部分の高さ（デッキ名 + 合計額）
_FOOTER_H = 34         # フッター部分の高さ（サービス名 + URL）
_BG = (18, 18, 28)
_TEXT = (235, 235, 240)
_PRICE = (255, 200, 60)
_FOOTER = (110, 110, 130)
_PLACEHOLDER_BG = (38, 38, 52)
_PLACEHOLDER_FG = (160, 160, 180)
_BADGE_BG = (210, 45, 45)

# ── フォントキャッシュ（プロセス内シングルトン） ──
_font_lock = threading.Lock()
_font_cache: dict = {}   # (size, bold) -> ImageFont


def _get_font(size: int = 16, bold: bool = False):
    """日本語対応フォントを返す。chart_renderer.py と同じフォント探索順序。"""
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    with _font_lock:
        if key in _font_cache:
            return _font_cache[key]
        here = os.path.dirname(os.path.abspath(__file__))
        # 1. リポジトリ同梱（chart_renderer と共通）
        # 2. apt install fonts-noto-cjk 後のシステムフォント
        candidates = [
            os.path.join(here, "assets", "fonts",
                         "NotoSansJP-Bold.otf" if bold else "NotoSansJP-Regular.otf"),
            os.path.join(here, "assets", "fonts", "NotoSansJP-Regular.otf"),
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansJP-Regular.ttf",
        ]
        font = None
        for path in candidates:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:
                    continue
        if font is None:
            font = ImageFont.load_default()
        _font_cache[key] = font
        return font


# ── カード画像ディスクキャッシュ ──
_CARD_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cardprice_card_imgs_v1")
_DECK_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cardprice_deck_imgs_v1")
os.makedirs(_CARD_CACHE_DIR, exist_ok=True)
os.makedirs(_DECK_CACHE_DIR, exist_ok=True)


def _card_cache_path(card_name: str) -> str:
    return os.path.join(_CARD_CACHE_DIR,
                        hashlib.md5(card_name.encode()).hexdigest() + ".webp")


def _fetch_card_image(card_name: str):
    """カード画像をディスクキャッシュ経由で取得。失敗時は None。

    x_poster.py の get_card_image_path() を利用（YGOResources 優先の多段フォールバック）。
    """
    cache_path = _card_cache_path(card_name)
    if os.path.exists(cache_path):
        try:
            return Image.open(cache_path).convert("RGB")
        except Exception:
            pass
    try:
        from x_poster import get_card_image_path
        src = get_card_image_path(card_name)
        if src and os.path.exists(src):
            img = Image.open(src).convert("RGB")
            img.save(cache_path, "WEBP", quality=80)
            return img
    except Exception:
        pass
    return None


def _placeholder(card_name: str) -> Image.Image:
    """画像取得失敗時のプレースホルダカード（暗色矩形 + カード名テキスト）"""
    img = Image.new("RGB", (_CARD_W, _CARD_H), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(img)
    font = _get_font(9)
    max_ch = 8
    lines = []
    buf = ""
    for ch in card_name:
        buf += ch
        if len(buf) >= max_ch:
            lines.append(buf)
            buf = ""
    if buf:
        lines.append(buf)
    line_h = 12
    y = max(4, (_CARD_H - len(lines) * line_h) // 2)
    for line in lines:
        draw.text((4, y), line, fill=_PLACEHOLDER_FG, font=font)
        y += line_h
    return img


def _draw_badge(img: Image.Image, qty: int) -> Image.Image:
    """右上に枚数バッジを描画して返す（img を in-place 変更）"""
    draw = ImageDraw.Draw(img)
    font = _get_font(12, bold=True)
    text = str(qty)
    bw, bh = 22, 18
    x, y = _CARD_W - bw - 2, 2
    draw.rounded_rectangle([x, y, x + bw, y + bh], radius=3, fill=_BADGE_BG)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text((x + (bw - tw) // 2, y + (bh - th) // 2), text, fill="white", font=font)
    return img


def generate_deck_image(deck_name: str, cards: list, total: int,
                         site_url: str = "") -> bytes:
    """
    デッキ画像を生成して PNG バイト列を返す。

    Args:
        deck_name : デッキ名（例: "巳剣"）
        cards     : [{"name": str, "qty": int}, ...] 順序はメイン → EX
        total     : 合計金額（円）
        site_url  : フッターに表示するサービスURL

    Returns:
        PNG バイト列
    """
    # ── キャッシュチェック ──
    sig = "|".join(f"{c['name']}:{c['qty']}" for c in cards)
    cache_key = hashlib.md5(f"{deck_name}|{total}|{sig}".encode()).hexdigest()
    cache_path = os.path.join(_DECK_CACHE_DIR, f"{cache_key}.png")
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return f.read()

    # ── カード画像を並列取得 ──
    unique_names = list(dict.fromkeys(c["name"] for c in cards))
    card_imgs: dict = {}
    with ThreadPoolExecutor(max_workers=min(len(unique_names), 10)) as ex:
        futs = {ex.submit(_fetch_card_image, name): name for name in unique_names}
        for fut in as_completed(futs, timeout=45):
            name = futs[fut]
            try:
                card_imgs[name] = fut.result()
            except Exception:
                card_imgs[name] = None

    # ── レイアウト計算 ──
    n = len(cards)
    cols = min(n, _COLS)
    rows = (n + cols - 1) // cols
    cell_w = _CARD_W + _GAP
    cell_h = _CARD_H + _GAP

    canvas_w = _MARGIN * 2 + cols * cell_w - _GAP
    canvas_h = _HEADER_H + rows * cell_h - _GAP + _MARGIN + _FOOTER_H
    canvas = Image.new("RGB", (canvas_w, canvas_h), _BG)
    draw = ImageDraw.Draw(canvas)

    # ── ヘッダー描画 ──
    font_title = _get_font(22, bold=True)
    font_price = _get_font(18)
    title_text = deck_name or "デッキ"
    price_text = f"合計 ¥{total:,}"
    draw.text((_MARGIN, 8), title_text, fill=_TEXT, font=font_title)
    draw.text((_MARGIN, 42), price_text, fill=_PRICE, font=font_price)

    # ── カードグリッド描画 ──
    for i, card in enumerate(cards):
        col = i % cols
        row = i // cols
        x = _MARGIN + col * cell_w
        y = _HEADER_H + row * cell_h

        raw = card_imgs.get(card["name"])
        if raw:
            cell = raw.resize((_CARD_W, _CARD_H), Image.LANCZOS).copy()
        else:
            cell = _placeholder(card["name"])
        if card["qty"] > 1:
            _draw_badge(cell, card["qty"])
        canvas.paste(cell, (x, y))

    # ── フッター描画 ──
    font_footer = _get_font(12)
    footer_text = f"カード相場  {site_url}" if site_url else "カード相場"
    fy = canvas_h - _FOOTER_H + 10
    draw.text((_MARGIN, fy), footer_text, fill=_FOOTER, font=font_footer)

    # ── PNG 出力 & キャッシュ保存 ──
    buf = io.BytesIO()
    canvas.save(buf, "PNG", optimize=True)
    data = buf.getvalue()
    with open(cache_path, "wb") as f:
        f.write(data)
    return data
