"""
カード画像の取得・フォントの共通部品
====================================
共有画像（card_og_image.py のカードOG画像・replay_og_image.py のリプレイOG画像）が使う、
日本語フォントの取得とカード画像のダウンロード（ディスクキャッシュ付き）。

export:
    _get_font(size, bold) -> ImageFont
    _download_card_image(card_name, url) -> PIL.Image | None

経緯: もとはデッキ画像（/api/deck-image）をサーバーで合成するモジュールだった。
2026-10-20 にデッキ画像はブラウザ内の合成（static/shared/deck-image.js）へ移し、
合成処理（generate_deck_image・透かし・枚数バッジ）は削除した。モジュール名は利用側の import を保つため残している。
"""

import io
import os
import hashlib
import tempfile
import threading

import requests as _requests
from PIL import Image, ImageFont

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

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
        weight = "Bold" if bold else "Regular"
        candidates = [
            # リポジトリ同梱フォント
            os.path.join(here, "assets", "fonts", f"NotoSansJP-{weight}.otf"),
            os.path.join(here, "assets", "fonts", "NotoSansJP-Regular.otf"),
            # apt install fonts-noto-cjk 後のパス（Debian/Ubuntu）
            f"/usr/share/fonts/opentype/noto/NotoSansCJKjp-{weight}.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
            f"/usr/share/fonts/opentype/noto/NotoSansCJK-{weight}.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
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
# _v2: 画像URL解決方式の変更（x_poster.get_card_image_path 依存 → app.py resolver 経由に刷新、
# コミット 8983439 2026-06-08）に伴い旧v1キャッシュと形式が変わるため名前を分けた。
# v2にした具体的理由（キャッシュ内容の非互換性の詳細）はコミットメッセージに記録なし。
_CARD_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cardprice_card_imgs_v2")
os.makedirs(_CARD_CACHE_DIR, exist_ok=True)


def _card_cache_path(card_name: str) -> str:
    return os.path.join(_CARD_CACHE_DIR,
                        hashlib.md5(card_name.encode()).hexdigest() + ".webp")


def _download_card_image(card_name: str, url: str):
    """指定URLからカード画像をDLし、ディスクキャッシュ経由でPIL Imageを返す。失敗時はNone。"""
    cache_path = _card_cache_path(card_name)
    if os.path.exists(cache_path):
        try:
            return Image.open(cache_path).convert("RGB")
        except Exception:
            pass
    try:
        resp = _requests.get(url, timeout=8, headers={"User-Agent": _UA})
        # 1024バイト閾値: コミット 8983439（2026-06-08）で x_poster.get_card_image_path 経由の
        # ファイル存在チェックから直接HTTP DLに切り替えた際に追加。エラーページ等の小容量レスポンスを
        # 弾く簡易ガードと見られるが、1024という具体的な数値の根拠はコミットメッセージに記録なし。
        if resp.status_code == 200 and len(resp.content) > 1024:
            img = Image.open(io.BytesIO(resp.content)).convert("RGB")
            img.save(cache_path, "WEBP", quality=80)
            return img
    except Exception:
        pass
    return None
