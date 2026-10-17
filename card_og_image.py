"""未発売カードごとの OG 画像（X などのリンクプレビュー用・1200x630 PNG）。

X の自動投稿（unreleased_x_post.py）は画像を添付せず、リンク先（/solitaire?newcards=1&focus=カード名）の
OG 画像で誘導する。この画像は「カード名・収録商品・発売日・シミュレーターへの誘導ボタン＋カード画像」を並べたもの。

- 公開中（approved/linked かつ hidden=false）で公開画像のあるカードだけが対象。それ以外は呼び出し側が汎用画像を返す
- 公式画像に「無断転載禁止」ロゴが入っているものは、設定 OG_BLUR_NO_REPOST（行なし=有効）が有効ならイラスト部分をぼかす
  （検出は no_repost_logo.py。結果は extraction_raw.watermark_no_repost に保存）
- 生成結果はディスクにキャッシュする。キーに「カード名・商品名・発売日・画像の取得時刻とURL・ぼかし設定・デザイン版」を含めるので、
  編集・画像差し替え・設定切替で古い画像が残らない
"""
import hashlib
import io
import logging
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timezone

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter

import no_repost_logo
import replay_og_image
from deck_image import _get_font

logger = logging.getLogger(__name__)

W, H = 1200, 630
DESIGN_VERSION = "v3"   # 見た目を変えたら上げる（古い画像を使わない）
SETTING_KEY = "OG_BLUR_NO_REPOST"

# ── 配色（replay_og_image と og-solitaire.png に合わせる）──
_BG = (0x0b, 0x12, 0x21)
_TEXT = (0xef, 0xf3, 0xfb)
_SUB = (0xa8, 0xb3, 0xc7)
_GOLD = (0xf3, 0xce, 0x46)
_BTN_FG = (0x14, 0x18, 0x22)
_GLOW = (0x2a, 0x3d, 0x68)

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_HERE, "static", "og-logo.png")

_LEFT = 72
_TEXT_MAX_W = 620            # 左カラムの文字の最大幅（カード画像は x=733 から）
_CARD_H = 520
_CARD_X = 733
_CARD_Y = 50
HEADLINE = "遊戯王OCG 新カード"   # 金色の見出し（ユーザー指定 2026-10-16）
_NAME_SIZES = (56, 48, 42)  # 収まらなければ順に小さくする
_NAME_TOP = 195
_BTN_W, _BTN_H = 546, 68

# 行頭に置かない文字（小書き文字・長音・中黒・約物）
_NO_LINE_START = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶーｰ・、。，．）」』】〕!?！？…‥々")
_MIN_LAST_LINE_CHARS = 3     # 2行のとき、最後の行がこれより短くなる折り返しは避ける
_MIN_DOT_BREAK_RATIO = 0.4   # 「・」の直後で折るとき、1行目がこの割合より短くなるなら使わない

_IMAGE_MAX_BYTES = 10 * 1024 * 1024
_IMAGE_MAX_PIXELS = 20_000_000
_IMAGE_TIMEOUT_SEC = 8   # TODO: calibrate from data

# ── キャッシュ・描画枠 ──
_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cardprice_card_og_v1")
os.makedirs(_CACHE_DIR, exist_ok=True)
_CACHE_MAX_FILES = 300   # TODO: calibrate from data
_CACHE_PRUNE_TO = 200   # TODO: calibrate from data
_render_sem = threading.BoundedSemaphore(2)
# 描画に失敗した key を短時間だけ覚え、同じ画像の再描画（画像取得のタイムアウト待ち等）を連打で繰り返さない
FAILURE_MEMO_SEC = 60.0   # TODO: calibrate from data
_FAILURE_MEMO_MAX = 64
_failure_lock = threading.Lock()
_failures: dict = {}   # key -> 期限（time.monotonic）
RENDER_WAIT_SEC = 3.0    # TODO: calibrate from data

_PUBLIC_STATUSES = ("approved", "linked")


# ──────────────────────────────────────────────
# カードの取得・キー
# ──────────────────────────────────────────────

def fetch_public_card(sb, card_id: int) -> dict | None:
    """公開中（approved/linked かつ hidden=false）で、見える公開画像があるカードを返す。無ければ None。

    戻り値: {id, name, product_name, release_date, card_type, image_url, image_key, watermark}
    image_key は画像の取得時刻とURL（画像の差し替えを検出するため）。例外はそのまま投げる。
    """
    rows = (sb.table("unreleased_cards")
            .select("id, name, product_name, release_date, card_type, status, hidden, watermark_no_repost:extraction_raw->watermark_no_repost")
            .eq("id", card_id).limit(1).execute().data or [])
    if not rows:
        return None
    c = rows[0]
    if c.get("status") not in _PUBLIC_STATUSES or c.get("hidden"):
        return None
    imgs = (sb.table("official_card_images").select("public_url, fetched_at")
            .eq("unreleased_card_id", card_id).eq("hidden", False).is_("deleted_at", "null")
            .order("id", desc=True).limit(1).execute().data or [])
    url = ((imgs[0].get("public_url") if imgs else "") or "").strip()
    if not url:
        return None
    return {
        "id": c["id"], "name": c.get("name") or "", "product_name": c.get("product_name") or "",
        "release_date": c.get("release_date"), "card_type": c.get("card_type") or "",
        "image_url": url, "image_key": f"{imgs[0].get('fetched_at') or ''}|{url}",
        "watermark": c.get("watermark_no_repost"),
    }


def version_key(card: dict, blur_enabled: bool) -> str:
    """見た目を決める入力から作る短いキー（キャッシュのファイル名と、リンク画像URLの ?v= に使う）。"""
    src = "\x1f".join([DESIGN_VERSION, str(card["id"]), card.get("name") or "", card.get("product_name") or "",
                       str(card.get("release_date") or ""), card.get("card_type") or "",
                       card.get("image_key") or "", "blur" if blur_enabled else "noblur",
                       no_repost_logo.DETECT_VERSION, str(no_repost_logo.MATCH_THRESHOLD)])
    return hashlib.sha1(src.encode("utf-8")).hexdigest()[:12]


def og_image_path(card: dict, blur_enabled: bool) -> str:
    return f"/solitaire/og/card/{card['id']}.png?v={version_key(card, blur_enabled)}"


def _format_release(release_date) -> str | None:
    if not release_date:
        return None
    try:
        d = date.fromisoformat(str(release_date)[:10])
    except ValueError:
        return None
    return f"{d.month}月{d.day}日"


def sub_line(card: dict) -> str:
    """「{商品名}｜{M月D日}発売」。どちらかが無ければ残る方だけ、両方無ければ空文字。"""
    parts = []
    if (card.get("product_name") or "").strip():
        parts.append(card["product_name"].strip())
    rd = _format_release(card.get("release_date"))
    if rd:
        parts.append(f"{rd}発売")
    return "｜".join(parts)


# ──────────────────────────────────────────────
# 文字の折り返し
# ──────────────────────────────────────────────

def _fit_prefix(draw, text: str, font, max_w: float) -> int:
    """text の先頭から max_w に収まる最大の文字数（最低1）。"""
    n = 1
    while n < len(text) and draw.textlength(text[:n + 1], font=font) <= max_w:
        n += 1
    return n


def wrap_name(draw, text: str, font, max_w: float) -> list[str]:
    """カード名を max_w に収まる行に分ける。

    折る位置は「・」の直後を優先し、なければ収まる最大位置。小書き文字・長音・「・」で行を始めない。
    英数字の途中では折らない（やむを得ない場合を除く）。
    """
    lines, rest = [], text.strip()
    while rest:
        if draw.textlength(rest, font=font) <= max_w:
            lines.append(rest)
            break
        n = _fit_prefix(draw, rest, font, max_w)   # rest[:n] は収まり、rest[:n+1] は収まらない
        k = rest.rfind("・", 0, n)
        if k != -1 and k + 1 >= n * _MIN_DOT_BREAK_RATIO and rest[k + 1] not in _NO_LINE_START:
            b = k + 1
        else:
            b = n
            while b > 1 and rest[b] in _NO_LINE_START:
                b -= 1
            c = b
            while c > 1 and rest[c - 1].isascii() and rest[c].isascii() and rest[c - 1].isalnum() and rest[c].isalnum():
                c -= 1
            if c > 1:
                b = c
        lines.append(rest[:b].rstrip())
        rest = rest[b:].lstrip()
    return lines


def _ellipsize(draw, text: str, font, max_w: float) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def layout_name(draw, name: str) -> tuple[int, list[str]]:
    """(フォントサイズ, 行のリスト)。56px で収まればそれ、だめなら小さくし、最小でも3行までで省略する。

    最後の行が1〜2文字だけ（「ム」だけが残るなど）になる折り返しは避け、先に文字を小さくして収める。
    """
    for size in _NAME_SIZES:
        lines = wrap_name(draw, name, _get_font(size, True), _TEXT_MAX_W)
        if len(lines) == 1 or (len(lines) == 2 and len(lines[-1]) >= _MIN_LAST_LINE_CHARS):
            return size, lines
    for size in _NAME_SIZES:
        lines = wrap_name(draw, name, _get_font(size, True), _TEXT_MAX_W)
        if len(lines) <= 2:
            return size, lines
    size = _NAME_SIZES[-1]
    font = _get_font(size, True)
    lines = wrap_name(draw, name, font, _TEXT_MAX_W)
    if len(lines) > 3:
        tail = "".join(lines[2:])
        lines = lines[:2] + [_ellipsize(draw, tail, font, _TEXT_MAX_W)]
    return size, lines


# ──────────────────────────────────────────────
# 描画
# ──────────────────────────────────────────────

def _background() -> Image.Image:
    """下地: 暗い紺＋右側のやわらかい明るみ＋下端の金色帯。"""
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    cx, cy = 915.0, 310.0
    dist2 = ((xx - cx) / 430.0) ** 2 + ((yy - cy) / 380.0) ** 2
    a = (0.55 * np.exp(-dist2 * 1.6))[..., None]
    base = np.array(_BG, np.float32)
    glow = np.array(_GLOW, np.float32)
    arr = base * (1 - a) + glow * a
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").convert("RGBA")
    ImageDraw.Draw(img).rectangle([0, H - 8, W, H], fill=_GOLD + (255,))
    return img


def _paste_card(canvas: Image.Image, card_img: Image.Image) -> None:
    """右側にカード画像（高さ 520px）を影つきで置く。"""
    w = round(card_img.width * _CARD_H / card_img.height)
    ci = card_img.convert("RGBA").resize((w, _CARD_H), Image.LANCZOS)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([_CARD_X, _CARD_Y + 12, _CARD_X + w, _CARD_Y + 12 + _CARD_H], fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(18))
    canvas.alpha_composite(shadow)
    canvas.alpha_composite(ci, (_CARD_X, _CARD_Y))


def render_png(card: dict, card_img: Image.Image) -> bytes:
    """OG 画像（PNG バイト列）を描く。card_img はぼかし済みのものを渡す。"""
    canvas = _background()
    d = ImageDraw.Draw(canvas)
    _paste_card(canvas, card_img)

    # ロゴ
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
        lh = 56
        logo = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
        canvas.alpha_composite(logo, (_LEFT, 80))
    except Exception as e:
        logger.warning(f"[card_og] ロゴ読み込み失敗: {e}")
        d.text((_LEFT, 108), "強欲なラボ", fill=_TEXT, font=_get_font(40, True), anchor="lm")

    d.text((_LEFT, 170), HEADLINE, fill=_GOLD, font=_get_font(30, True), anchor="lm")

    size, lines = layout_name(d, card["name"] or "？")
    nf = _get_font(size, True)
    pitch = round(size * 1.3)
    for i, ln in enumerate(lines):
        d.text((_LEFT, _NAME_TOP + pitch * i + pitch / 2), ln, fill=_TEXT, font=nf, anchor="lm")
    y = _NAME_TOP + pitch * len(lines)

    sub = sub_line(card)
    if sub:
        sf = _get_font(26)
        d.text((_LEFT, y + 27), _ellipsize(d, sub, sf, _TEXT_MAX_W), fill=_SUB, font=sf, anchor="lm")
        btn_top = y + 27 + 52
    else:
        btn_top = y + 34

    d.rounded_rectangle([_LEFT, btn_top, _LEFT + _BTN_W, btn_top + _BTN_H], radius=_BTN_H // 2, fill=_GOLD)
    d.text((_LEFT + _BTN_W / 2, btn_top + _BTN_H / 2), "▶ タップしてシミュレーターで試す",
           fill=_BTN_FG, font=_get_font(30, True), anchor="mm")
    d.text((_LEFT, btn_top + _BTN_H + 39), "登録なし・無料ですぐ一人回しできます", fill=_SUB, font=_get_font(22), anchor="lm")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


# ──────────────────────────────────────────────
# 画像取得・ロゴ判定・キャッシュ
# ──────────────────────────────────────────────

def _download(url: str) -> Image.Image:
    """公開画像を取得する（URL は DB の公開 URL）。失敗は例外。"""
    if not url.startswith("https://"):
        raise ValueError(f"https 以外の画像URL: {url[:40]!r}")
    resp = requests.get(url, timeout=_IMAGE_TIMEOUT_SEC, headers={"User-Agent": "TCGYM-OG/1.0"})
    resp.raise_for_status()
    if len(resp.content) > _IMAGE_MAX_BYTES:
        raise ValueError(f"画像サイズ超過: {len(resp.content):,}バイト")
    im = Image.open(io.BytesIO(resp.content))
    if im.width * im.height > _IMAGE_MAX_PIXELS:
        raise ValueError("画像の画素数が上限を超えています")
    return im.convert("RGB")


def _watermark_for(sb, card: dict, img: Image.Image) -> dict:
    """保存済みのロゴ判定（同じ画像に対するもの）を返す。無い・画像が変わっていれば計算して保存する。"""
    wm = card.get("watermark")
    if (isinstance(wm, dict) and wm.get("image_key") == card.get("image_key")
            and wm.get("v") == no_repost_logo.DETECT_VERSION
            and isinstance(wm.get("score"), (int, float)) and not isinstance(wm.get("score"), bool)):
        # 保存済み（同じ画像・同じ検出の版）。detected は保存値を信じず、score を現行の閾値で判定し直す
        return {**wm, "detected": no_repost_logo.is_detected(wm["score"])}
    detected, score = no_repost_logo.detect(img)
    wm = {"detected": detected, "score": score, "at": datetime.now(timezone.utc).isoformat(),
          "image_key": card.get("image_key"), "v": no_repost_logo.DETECT_VERSION}
    try:
        from unreleased_image_store import _merge_extraction_raw
        _merge_extraction_raw(sb, card["id"], {"watermark_no_repost": wm})
    except Exception as e:
        logger.warning(f"[card_og] ロゴ判定の保存に失敗（無視） card_id={card['id']}: {e}")
    return wm


def prepare_watermark(sb, card_id: int) -> bool:
    """ロゴ判定を計算して保存しておく（X 投稿の直前に呼ぶ。公開の GET で DB 書き込みが起きる機会を減らす）。
    公開中のカードでなければ何もせず False。画像取得などの失敗は例外のまま（呼び出し側が無視する）。"""
    card = fetch_public_card(sb, card_id)
    if card is None:
        return False
    _watermark_for(sb, card, _download(card["image_url"]))
    return True


def render_card(sb, card: dict, blur_enabled: bool) -> bytes:
    img = _download(card["image_url"])
    wm = _watermark_for(sb, card, img)
    if blur_enabled and wm.get("detected"):
        img = no_repost_logo.blur_art(img, card.get("card_type"))
    return render_png(card, img)


def _cache_path(key: str) -> str:
    return os.path.join(_CACHE_DIR, f"{key}.png")


def read_cached(key: str):
    if not re.fullmatch(r"[0-9a-f]{12}", key):
        return None
    try:
        with open(_cache_path(key), "rb") as f:
            return f.read()
    except OSError:
        return None


def _prune_cache() -> None:
    try:
        files = [os.path.join(_CACHE_DIR, f) for f in os.listdir(_CACHE_DIR) if f.endswith(".png")]
        if len(files) <= _CACHE_MAX_FILES:
            return
        files.sort(key=lambda p: os.path.getmtime(p))
        for p in files[:len(files) - _CACHE_PRUNE_TO]:
            os.remove(p)
    except OSError as e:
        logger.warning(f"[card_og] キャッシュの整理に失敗（無視）: {e}")


def _recently_failed(key: str) -> bool:
    now = time.monotonic()
    with _failure_lock:
        return _failures.get(key, 0.0) > now


def _remember_failure(key: str) -> None:
    now = time.monotonic()
    with _failure_lock:
        for k in [k for k, t in _failures.items() if t <= now]:
            del _failures[k]
        while len(_failures) >= _FAILURE_MEMO_MAX:
            del _failures[min(_failures, key=_failures.get)]
        _failures[key] = now + FAILURE_MEMO_SEC


def get_or_render(sb, card: dict, blur_enabled: bool) -> bytes:
    """キャッシュ優先で OG 画像（PNG）を返す。

    描画枠が取れない・描画の予算超過は replay_og_image.RenderBusy。画像取得・描画の失敗はそのまま例外。
    """
    key = version_key(card, blur_enabled)
    hit = read_cached(key)
    if hit is not None:
        return hit
    if _recently_failed(key):
        raise replay_og_image.RenderBusy(f"recent-failure:card:{card['id']}")
    if not _render_sem.acquire(timeout=RENDER_WAIT_SEC):
        raise replay_og_image.RenderBusy(f"card:{card['id']}")
    try:
        hit = read_cached(key)
        if hit is not None:
            return hit
        if not replay_og_image.consume_render_budget():
            raise replay_og_image.RenderBusy(f"budget:card:{card['id']}")
        try:
            png = render_card(sb, card, blur_enabled)
        except Exception:
            _remember_failure(key)
            raise
    finally:
        _render_sem.release()
    tmp = _cache_path(key) + f".{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(png)
        os.replace(tmp, _cache_path(key))
        _prune_cache()
    except OSError as e:
        logger.warning(f"[card_og] キャッシュ書き込み失敗: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
    return png
