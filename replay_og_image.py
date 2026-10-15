"""
replay_og_image.py — 共有リプレイのリンク画像（OGP, 1200x630）を Pillow で描く

共有リプレイ（/solitaire?replay=<id>）を X などに投稿したとき、リプレイの最後の盤面が
カード画像つきで出るようにする。盤面データ（board）の形は replay_board.py を参照。

レイアウト（承認済みモックA）:
  上部   ロゴ・リプレイ名・手数の黄色バッジ
  左     フィールド（5列×3行。フィールド魔法・EXモンスターゾーンの並びは mobile.css の5列配置に合わせる）
  右     墓地・除外（枚数と一番上のカード）
  下部   手札（枚数と小さいカード）＋ デッキ/EX の残り枚数

再利用: 画像ダウンロード・ディスクキャッシュ・フォント取得は deck_image のものを import する（複製しない）。
カード画像の URL は利用者が送った値を使わず、カード名から card_display の画像解決で求める
（未発売カードも同じ。トークンは名前入りの枠で描く）。
"""

import io
import os
import re
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from PIL import Image, ImageDraw

from deck_image import _download_card_image, _get_font

logger = logging.getLogger(__name__)

# ── 配色（承認モックと同じ値） ──
_BG = (0x0b, 0x12, 0x21)
_ZONE = (0x1b, 0x25, 0x39)
_TEXT = (0xef, 0xf3, 0xfb)
_SUB = (0xa8, 0xb4, 0xc9)
_YELLOW = (0xf3, 0xce, 0x46)
_BADGE_FG = (0x24, 0x20, 0x13)

W, H = 1200, 630

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_HERE, "static", "og-logo.png")
GENERIC_IMAGE_PATH = os.path.join(_HERE, "static", "og-solitaire.png")

# 5列配置（mobile.css の slot→grid 対応と同じ）。slot9 はPCのEXデッキ位置の空きマスで、
# 5列には居場所が無いため左上の余りマスに置く
_SLOT_CELL = {
    1: (0, 1), 3: (0, 2), 2: (0, 3), 9: (0, 0),
    4: (1, 0), 5: (1, 1), 6: (1, 2), 7: (1, 3), 8: (1, 4),
    10: (2, 0), 11: (2, 1), 12: (2, 2), 13: (2, 3), 14: (2, 4),
}

# カード画像の取得待ち上限（秒）。Xのクローラーは待ち時間が短いので、超えた分は枠で代用して描画を続ける。
# TODO: calibrate from data（クローラーの実際の待ち時間は未計測）
FETCH_DEADLINE_SEC = 7.0
FETCH_MAX_WORKERS = 12

# カード画像は board の img を使わず、カード名からサーバー側で解決する（利用者が送った URL は一切取りに行かない。
# 任意URLの取得＝SSRF と、名前で引く共通キャッシュの汚染を避けるため）。
HAND_DRAW_MAX = 10   # 手札は先頭のこの枚数だけ描く（残りは「+N」）。1枚の画像で取りに行く枚数の上限

# 取得用スレッドはプロセス全体で共有する（リクエストごとに作ると連打でスレッドが際限なく増えるため）
_fetch_pool = ThreadPoolExecutor(max_workers=FETCH_MAX_WORKERS, thread_name_prefix="replay-og-fetch")


def _resolve_urls(names: list) -> tuple:
    """カード名のリストから取得先 URL をまとめて解決する（リクエスト側のスレッドで1回だけ呼ぶ）。

    戻り値: ({名前: URL|None}, unavailable)
      - URL が None のカードは取りに行かず、名前入りの枠で描く（画像の無い未発売カード・見つからない名前。
        店舗サイトの名前検索へのフォールバックはしない）。
      - unavailable=True は「名前が見つからない」ではなく「名前の索引・未発売カード辞書が使えない」ことを示す
        （この場合の枠だらけの描画は不完全として扱い、ディスクに固定しない）。
    まとめて解決することで、未発売カード辞書の期限切れ時に全件読み込みが同時に何本も走るのを避ける。
    """
    if not names:
        return {}, False
    import card_display
    try:
        disp = card_display.resolve_card_displays(list(names))
    except Exception as e:
        logger.warning(f"[replay_og] カード画像の解決に失敗: {e}")
        return {n: None for n in names}, True
    urls = {n: (disp.get(n, {}).get("url") if disp.get(n, {}).get("kind") == "image" else None) for n in names}
    unresolved = any(disp.get(n, {}).get("kind", "none") == "none" for n in names)
    unavailable = unresolved and not (card_display.released_index_available()
                                      and card_display.unreleased_data_available())
    return urls, unavailable


def _fetch_card_image(name: str, url) -> tuple:
    """1枚分の画像を取得する（url は _resolve_urls がサーバー側で求めたもの）。戻り値: (PIL Image|None, failed)。

    failed は「画像を取りに行ったが取れなかった（通信失敗・例外）」ことを示す。
    url が無い（枠で代用する）のは失敗ではない。
    """
    if not url:
        return None, False
    try:
        im = _download_card_image(name, url)
        return im, im is None
    except Exception as e:
        logger.warning(f"[replay_og] カード画像取得失敗 {name!r}: {e}")
        return None, True


def _fetch_all(names: list) -> tuple:
    """カード名のリストを並列取得。戻り値: ({名前: Image|None}, 失敗・時間切れ・解決不能があったか)

    時間切れ時、まだ始まっていない取得は取り消す（走り始めた分は _download_card_image の
    通信タイムアウト（8秒）で終わる。共有プールなので増え続けない）。
    """
    keys = list(dict.fromkeys(names))
    result = {k: None for k in keys}
    if not keys:
        return result, False
    urls, degraded = _resolve_urls(keys)
    futs = {_fetch_pool.submit(_fetch_card_image, name, urls.get(name)): name for name in keys if urls.get(name)}
    done, not_done = wait(futs, timeout=FETCH_DEADLINE_SEC)
    degraded = degraded or bool(not_done)
    for f in not_done:
        f.cancel()
    for f in done:
        try:
            im, failed = f.result()
            result[futs[f]] = im
            degraded = degraded or failed
        except Exception:
            degraded = True
    return result, degraded


# ── 描画ヘルパ ──

def _name_frame(name: str, height: int) -> Image.Image:
    """画像の無いカード（未発売プロキシ・取得失敗）の代用: カード名を書いた暗色の枠（RGB）。"""
    w = round(height * 0.686)
    im = Image.new("RGB", (w, height), (0x26, 0x30, 0x4a))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, height - 1], outline=(0x5a, 0x68, 0x88), width=2)
    size = max(9, height // 9)
    font = _get_font(size, True)
    lines, buf = [], ""
    for ch in (name or "？"):
        if buf and d.textlength(buf + ch, font=font) > w - 12:
            lines.append(buf)
            buf = ""
        buf += ch
    if buf:
        lines.append(buf)
    lh = round(size * 1.3)
    lines = lines[: max(1, (height - 12) // lh)]
    y = (height - len(lines) * lh) // 2
    for ln in lines:
        d.text((w / 2, y + lh / 2), ln, fill=_TEXT, font=font, anchor="mm")
        y += lh
    return im


def _card_face(src, name: str, height: int) -> Image.Image:
    """表向きカード（RGBA・角丸）。src が None なら名前入りの枠で代用。"""
    base = src if src is not None else _name_frame(name, height)
    w = max(1, round(base.width * height / base.height))
    im = base.resize((w, height), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1],
                                           radius=max(2, height // 28), fill=255)
    im.putalpha(mask)
    return im


def _card_back(height: int) -> Image.Image:
    """裏側（セット）のカード。カード裏の絵の代わりに暗色の枠＋楕円で描く。"""
    w = round(height * 0.686)
    im = Image.new("RGBA", (w, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, w - 1, height - 1], radius=max(2, height // 28),
                        fill=(0x3a, 0x2a, 0x22), outline=(0x8a, 0x6a, 0x3a), width=max(1, height // 60))
    m = w // 6
    d.ellipse([m, height // 2 - w // 3, w - m, height // 2 + w // 3], fill=(0x5a, 0x1f, 0x2a),
              outline=(0xb0, 0x8a, 0x4a), width=max(1, height // 60))
    return im


def _paste(canvas: Image.Image, im: Image.Image, cx: int, cy: int):
    canvas.paste(im, (round(cx - im.width / 2), round(cy - im.height / 2)), im)


def _shadow(canvas: Image.Image, im: Image.Image, cx: int, cy: int):
    """カードの影（簡易）。"""
    sh = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh.putalpha(im.getchannel("A").point(lambda a: int(a * 0.45)))
    canvas.paste(sh, (round(cx - im.width / 2) + 2, round(cy - im.height / 2) + 4), sh)


def _fit_text(draw, text: str, font, max_w: int) -> str:
    """max_w に収まるよう末尾を「…」で切る。"""
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _wants_image(c: dict) -> bool:
    """画像を取りに行く対象か（トークンは data: 画像を持つだけなので名前入りの枠で描く）。"""
    return bool(c.get("name")) and not c.get("token")


def render_replay_og(board: dict, title: str = "") -> tuple:
    """盤面画像（PNG バイト列）を描く。戻り値: (png_bytes, degraded)。

    degraded=True は、画像を取りに行ったが失敗・時間切れになったカードを含むことを示す
    （画像の無い未発売カードを枠で代用しただけの場合は degraded にしない）。
    呼び出し側は degraded の描画をディスクに固定せず、短期間だけメモリに置く。
    """
    title = (title or "").strip() or "共有リプレイ"

    # 取得対象: フィールド最上段・手札の先頭 HAND_DRAW_MAX 枚・墓地/除外の一番上（裏向き・トークンは取りに行かない）
    hand = board["hand"][:HAND_DRAW_MAX]
    cands = [e for e in board["field"] if e["face"] == "up"] + hand
    for pile in (board["grave"], board["banish"]):
        if pile["top"] and pile["top"].get("face") != "down":
            cands.append(pile["top"])
    imgs, degraded = _fetch_all([c["name"] for c in cands if _wants_image(c)])

    canvas = Image.new("RGBA", (W, H), _BG + (255,))
    d = ImageDraw.Draw(canvas)

    def put(card_dict, height, cx, cy, face_down=False, rotate=False):
        if face_down:
            im = _card_back(height)
        else:
            src = imgs.get(card_dict["name"]) if _wants_image(card_dict) else None
            im = _card_face(src, card_dict.get("name", ""), height)
        if rotate:
            im = im.rotate(-90, expand=True)   # 守備表示は時計回りに90度
        _shadow(canvas, im, cx, cy)
        _paste(canvas, im, cx, cy)

    # ── 上部: ロゴ・タイトル・手数バッジ ──
    x = 36
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
        lh = 40
        logo = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
        canvas.paste(logo, (36, 39 - lh // 2), logo)
        x = 36 + logo.width + 18
    except Exception as e:
        logger.warning(f"[replay_og] ロゴ読み込み失敗: {e}")
        d.text((36, 39), "TCGYM", fill=_TEXT, font=_get_font(30, True), anchor="lm")
        x = 36 + 120 + 18
    badge_w = 0
    moves = board.get("moves", 0)
    if moves > 0:
        bf = _get_font(24, True)
        txt = f"{moves}手"
        tw = d.textlength(txt, font=bf)
        badge_w = round(tw) + 32
        bx1 = W - 36
        d.rounded_rectangle([bx1 - badge_w, 39 - 22, bx1, 39 + 22], radius=8, fill=_YELLOW)
        d.text((bx1 - badge_w / 2, 39), txt, fill=_BADGE_FG, font=bf, anchor="mm")
    tf = _get_font(30, True)
    d.text((x, 39), _fit_text(d, title, tf, W - 36 - badge_w - 18 - x), fill=_TEXT, font=tf, anchor="lm")

    # ── フィールド（5列×3行） ──
    fx, fy, fw, fh, gap = 36, 96, 900, 430, 10
    cw = (fw - gap * 4) / 5
    ch = (fh - gap * 2) / 3
    for r in range(3):
        for c in range(5):
            x0 = fx + c * (cw + gap)
            y0 = fy + r * (ch + gap)
            d.rounded_rectangle([x0, y0, x0 + cw, y0 + ch], radius=6, fill=_ZONE)
    for e in board["field"]:
        cell = _SLOT_CELL.get(int(e["slot"].rsplit("-", 1)[1]))
        if not cell:
            continue
        r, c = cell
        cx = fx + c * (cw + gap) + cw / 2
        cy = fy + r * (ch + gap) + ch / 2
        put(e, 126, cx, cy, face_down=(e["face"] == "down"), rotate=(e["pos"] == "def"))
        if e.get("n", 1) > 1:   # 重なり枚数（エクシーズ素材など）
            nx, ny = fx + (c + 1) * cw + c * gap - 18, fy + r * (ch + gap) + 16
            d.ellipse([nx - 13, ny - 13, nx + 13, ny + 13], fill=_YELLOW)
            d.text((nx, ny), str(e["n"]), fill=_BADGE_FG, font=_get_font(16, True), anchor="mm")

    # ── 右: 墓地・除外 ──
    sx, sw = 956, 208
    sh = (fh - gap) / 2
    for i, (label, pile) in enumerate((("墓地", board["grave"]), ("除外", board["banish"]))):
        y0 = fy + i * (sh + gap)
        d.rounded_rectangle([sx, y0, sx + sw, y0 + sh], radius=6, fill=_ZONE)
        d.text((sx + 12, y0 + 18), label, fill=_SUB, font=_get_font(15, True), anchor="lm")
        d.text((sx + sw - 12, y0 + 18), str(pile["count"]), fill=_SUB, font=_get_font(15), anchor="rm")
        if pile["top"]:
            put(pile["top"], 110, sx + sw / 2, y0 + sh - 12 - 55, face_down=(pile["top"].get("face") == "down"))

    # ── 下部: 手札 ──
    hy0, hh = 540, 70
    d.rounded_rectangle([36, hy0, W - 36, hy0 + hh], radius=6, fill=_ZONE)
    lab = f"手札 {len(board['hand'])}"
    d.text((50, hy0 + hh / 2), lab, fill=_SUB, font=_get_font(15, True), anchor="lm")
    right = f"デッキ {board['deckCount']}  /  EX {board['ex']['count']}"
    rf = _get_font(16)
    rw = d.textlength(right, font=rf)
    d.text((W - 36 - 14, hy0 + hh / 2), right, fill=_SUB, font=rf, anchor="rm")
    hx0 = 50 + d.textlength(lab, font=_get_font(15, True)) + 18
    n = len(hand)
    cardw = round(56 * 0.686)
    step = cardw + 10
    for i, c in enumerate(hand):
        put(c, 56, hx0 + cardw / 2 + i * step, hy0 + hh / 2)
    rest = len(board["hand"]) - n
    if rest > 0:   # 描き切らなかった手札の枚数
        d.text((hx0 + n * step + 4, hy0 + hh / 2), f"+{rest}", fill=_TEXT, font=_get_font(20, True), anchor="lm")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue(), degraded


# ── キャッシュ・生成の入口 ──

_REPLAY_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,16}")   # secrets.token_urlsafe(8) は11文字
_OG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cardprice_replay_og_v1")
os.makedirs(_OG_CACHE_DIR, exist_ok=True)

# 同時描画数の上限（メモリ保護。本番は gunicorn 1ワーカー・8スレッドで約330MB常用）。
# 取れなければ待ち続けず呼び出し側が汎用画像を返す（連打でスレッドを塞がないため）。
_render_sem = threading.BoundedSemaphore(2)
RENDER_WAIT_SEC = 3.0   # TODO: calibrate from data

# 不完全な描画（カード画像の取得失敗・時間切れ）は短期間だけメモリに置く（同じ URL への連打で毎回描き直さないため）
DEGRADED_TTL_SEC = 180.0
DEGRADED_MAX_ENTRIES = 64
_degraded_lock = threading.Lock()
_degraded_cache: dict = {}   # replay_id -> (期限 time.monotonic, png)

# 実際に描くとき（board があり、キャッシュに無い）だけ消費する、プロセス全体での描画の予算。
# IP 単位にしないのは、X のクローラーが少数の IP から多数のリンクを取りに来るのと、IPv6 で IP を変えて
# すり抜けられるのを避けるため。超えたら呼び出し側が汎用画像を返す。
RENDER_BUDGET_WINDOW_SEC = 60
RENDER_BUDGET_MAX = 30   # TODO: calibrate from data（仮置き。描画は1回あたり数百ms〜数秒・同時2件まで）
_budget_lock = threading.Lock()
_budget_log: list = []   # 直近の描画時刻（time.monotonic）


class RenderBusy(Exception):
    """同時描画の枠が取れなかった、または描画の予算を使い切った（呼び出し側は汎用画像を返す）"""


def consume_render_budget() -> bool:
    """描画の予算を1つ使う。使い切っていれば False。"""
    now = time.monotonic()
    with _budget_lock:
        _budget_log[:] = [t for t in _budget_log if now - t < RENDER_BUDGET_WINDOW_SEC]
        if len(_budget_log) >= RENDER_BUDGET_MAX:
            return False
        _budget_log.append(now)
        return True


def is_valid_replay_id(replay_id) -> bool:
    return isinstance(replay_id, str) and _REPLAY_ID_RE.fullmatch(replay_id) is not None


def _cache_path(replay_id: str) -> str:
    return os.path.join(_OG_CACHE_DIR, f"{replay_id}.png")


def read_cached(replay_id: str):
    """ディスクキャッシュ済み（完全な描画）の PNG バイト列。無ければ None。"""
    if not is_valid_replay_id(replay_id):
        return None
    try:
        with open(_cache_path(replay_id), "rb") as f:
            return f.read()
    except OSError:
        return None


def _read_degraded(replay_id: str):
    with _degraded_lock:
        item = _degraded_cache.get(replay_id)
        if item and item[0] > time.monotonic():
            return item[1]
        _degraded_cache.pop(replay_id, None)
    return None


def _store_degraded(replay_id: str, png: bytes):
    now = time.monotonic()
    with _degraded_lock:
        for k in [k for k, v in _degraded_cache.items() if v[0] <= now]:
            del _degraded_cache[k]
        while len(_degraded_cache) >= DEGRADED_MAX_ENTRIES:
            del _degraded_cache[min(_degraded_cache, key=lambda k: _degraded_cache[k][0])]
        _degraded_cache[replay_id] = (now + DEGRADED_TTL_SEC, png)


def read_any_cached(replay_id: str):
    """キャッシュ済みの PNG。戻り値: (png, degraded) または None。degraded は短期メモリキャッシュ由来。"""
    png = read_cached(replay_id)
    if png is not None:
        return png, False
    png = _read_degraded(replay_id) if is_valid_replay_id(replay_id) else None
    return (png, True) if png is not None else None


def get_or_render(replay_id: str, board: dict, title: str = "") -> tuple:
    """キャッシュ優先で PNG を返す。戻り値: (png_bytes, degraded)。

    完全な描画はディスクに、degraded（画像取得に失敗した描画）は数分だけメモリに置く。
    描画枠が RENDER_WAIT_SEC 秒以内に取れない、または描画の予算を使い切っていれば RenderBusy を送出する。
    """
    hit = read_any_cached(replay_id)
    if hit is not None:
        return hit
    if not _render_sem.acquire(timeout=RENDER_WAIT_SEC):
        raise RenderBusy(replay_id)
    try:
        hit = read_any_cached(replay_id)   # 待っている間に他スレッドが生成済みの場合
        if hit is not None:
            return hit
        if not consume_render_budget():
            raise RenderBusy(f"budget:{replay_id}")
        png, degraded = render_replay_og(board, title)
    finally:
        _render_sem.release()
    if degraded:
        _store_degraded(replay_id, png)
    elif is_valid_replay_id(replay_id):
        tmp = _cache_path(replay_id) + f".{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(png)
            os.replace(tmp, _cache_path(replay_id))   # 書きかけを読ませない
        except OSError as e:
            logger.warning(f"[replay_og] キャッシュ書き込み失敗: {e}")
            try:
                os.remove(tmp)   # 書き込み失敗で残った一時ファイルを消す
            except OSError:
                pass
    return png, degraded


def warm_cache_async(replay_id: str, board: dict, title: str = ""):
    """保存直後にバックグラウンドで先に描いてキャッシュを温める（失敗しても保存には影響しない）。"""
    def _run():
        try:
            get_or_render(replay_id, board, title)
        except RenderBusy:
            pass   # 混んでいれば先行生成は諦める（実際に取得されたときに描く）
        except Exception as e:
            logger.warning(f"[replay_og] 先行生成失敗 {replay_id}: {e}")
    threading.Thread(target=_run, name=f"replay-og-{replay_id}", daemon=True).start()
