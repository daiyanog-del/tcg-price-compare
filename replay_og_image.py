"""
replay_og_image.py — 共有リプレイのリンク画像（OGP, 1200x630）を Pillow で描く

共有リプレイ（/solitaire?replay=<id>）を X などに投稿したとき、リプレイの最後の盤面が
カード画像つきで出るようにする。盤面データ（board）の形は replay_board.py を参照。

レイアウト（承認済みモック v2。本番のシミュレーター画面の盤面（PC版 1440x900 で実測）を縮尺して再現する）:
  上部   ロゴ・リプレイ名・手数の黄色バッジ（盤面の左右端にそろえる）
  中央   盤面。EXモンスターゾーン2マス・中段6マス・下段6マス・墓地・除外を本番の座標のまま拡大して描く
         （空きマスのゾーンアイコンは本番CSSの線画SVGを透過PNG化した static/og/zone-*.png）
         墓地・除外は枠の中にカードを縦にずらして重ねる（最後の数枚）
  下部   手札（枚数と、少し重ねて並べた小さいカード）＋ デッキ/EX の残り枚数

再利用: 画像ダウンロード・ディスクキャッシュ・フォント取得は deck_image のものを import する（複製しない）。
カード画像の URL は利用者が送った値を使わず、カード名から card_display の画像解決で求める
（未発売カードも同じ。トークンは名前入りの枠で描く）。
"""

import io
import math
import os
import re
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw

from deck_image import _download_card_image, _get_font

logger = logging.getLogger(__name__)

# ── 配色（承認モックと同じ値） ──
_BG = (0x0b, 0x12, 0x21)
# 本番（relief-theme.css）のマス・墓地・除外の面と、手札・デッキのトレイの色
_FACE = (0x25, 0x31, 0x43)   # --relief-face
_TRAY = (0x20, 0x2c, 0x3d)   # --relief-tray
# 本番は面に細かい粒子（feTurbulence・opacity .07）を重ねている。粒子の細かい凹凸は縮小すると消える（本番画面の実測で
# 標準偏差 約1.5/255）ので省き、粒子による平均の明るさの上がり分だけを色に足す（実測: 面 +5・トレイ +5.5 前後）。
_GRAIN_SHIFT = (5, 5, 4)
_TEXT = (0xef, 0xf3, 0xfb)
_SUB = (0xa8, 0xb4, 0xc9)
_YELLOW = (0xf3, 0xce, 0x46)
_BADGE_FG = (0x24, 0x20, 0x13)

W, H = 1200, 630

_HERE = os.path.dirname(os.path.abspath(__file__))
_LOGO_PATH = os.path.join(_HERE, "static", "og-logo.png")
GENERIC_IMAGE_PATH = os.path.join(_HERE, "static", "og-solitaire.png")

_ICON_DIR = os.path.join(_HERE, "static", "og")
_COUNT = (0x64, 0x74, 0x8b)   # 墓地・除外の枚数（本番の .zone-count の色）

# ── 本番の盤面の実測値（PC版 1440x900 の /solitaire。単位は画面の px）──
# 盤面の領域（EXモンスターゾーン〜墓地・除外）をこの範囲で切り出し、縦横比を保って 1200x630 の中へ拡大する
_SRC_X0, _SRC_X1, _SRC_Y0, _SRC_Y1 = 320, 1254, 30, 480
_SCALE = 492 / (_SRC_Y1 - _SRC_Y0)           # 盤面の高さを 492px にする
_BX = (W - (_SRC_X1 - _SRC_X0) * _SCALE) / 2  # 左右中央
_BY = 70
_SLOT_W, _SLOT_H = 98, 141.19                  # PR#56 後の実測（--slot-width 96px）
_COLS = [330.81 + 106 * i for i in range(6)]   # 中段・下段の6列
_ROWS = (33, 184.19, 335.38)
_SLOTS = {                                      # slot番号 -> (x, y)。1・2 は EXモンスターゾーン（中段の3列目・5列目の真上）
    1: (_COLS[2], _ROWS[0]), 2: (_COLS[4], _ROWS[0]),
    **{3 + i: (_COLS[i], _ROWS[1]) for i in range(6)},
    **{9 + i: (_COLS[i], _ROWS[2]) for i in range(6)},
}
# 空きマスの刻印アイコン: (slot, 名前, 本番の background-size に当たる幅px)。slot3=フィールド・10/14=ペンデュラムとも 76%（相対はマスの内側 96px）
_SLOT_ICONS = [(3, "field", 96 * 0.76), (10, "pendulum", 96 * 0.76), (14, "pendulum", 96 * 0.76)]
_ICON_SIDE_RATIO = 0.72                        # 墓地・除外の刻印は side-slot 幅の 72%
_CARD_W, _CARD_H = 96, 139.19                  # .tier-item の実寸
_GRAVE_RECT = (966.81, 33, 133.19, 443.56)     # 墓地 .sol-grave
_BANISH_RECT = (1110, 33, 133.19, 443.56)      # 除外 .sol-banish
_SIDE_TOP, _SIDE_W, _SIDE_H = 27, 115.19, 409.56  # 枠の中の .side-slot（カードを積む領域）
_PILE_STEP = 23.92                             # 墓地・除外でカードを縦にずらす幅（本番で5枚置いて実測）
PILE_DRAW_MAX = 4                              # 墓地・除外で描く枚数（最後の数枚）
_BAND_Y, _BAND_H = 570, 56                     # 下の帯（手札・デッキ/EX）
_HAND_GAP = 5                                  # 手札カードの間隔（収まるときは重ねずにこの間を空ける。収まらないときだけ重ねる）

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

def _name_frame(name: str, w: int, h: int) -> Image.Image:
    """画像の無いカード（未発売プロキシ・取得失敗）の代用: カード名を書いた暗色の枠（RGB）。"""
    im = Image.new("RGB", (w, h), (0x26, 0x30, 0x4a))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=(0x5a, 0x68, 0x88), width=2)
    size = max(9, h // 9)
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
    lines = lines[: max(1, (h - 12) // lh)]
    y = (h - len(lines) * lh) // 2
    for ln in lines:
        d.text((w / 2, y + lh / 2), ln, fill=_TEXT, font=font, anchor="mm")
        y += lh
    return im


def _rounded_alpha(im: Image.Image, radius: int) -> Image.Image:
    """角丸のアルファを付ける（4倍で描いて縮小し、縁をなめらかにする）。"""
    w, h = im.size
    mask = Image.new("L", (w * 4, h * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w * 4 - 1, h * 4 - 1], radius=radius * 4, fill=255)
    im = im.convert("RGBA")
    im.putalpha(mask.resize((w, h), Image.BOX))   # LANCZOS だと縁に明るいリンギングが出る
    return im


def _card_face(src, name: str, w: int, h: int, radius: int) -> Image.Image:
    """表向きカード（RGBA・角丸）。本番の .tier-item（object-fit: cover）と同じく w×h いっぱいに描く。
    src が None なら名前入りの枠で代用。"""
    if src is None:
        base = _name_frame(name, w, h)
    else:
        # cover: 縦横比が違うときは中央を切り出す
        k = max(w / src.width, h / src.height)
        rw, rh = max(w, round(src.width * k)), max(h, round(src.height * k))
        base = src.convert("RGB").resize((rw, rh), Image.LANCZOS)
        left, top = (rw - w) // 2, (rh - h) // 2
        base = base.crop((left, top, left + w, top + h))
    return _rounded_alpha(base, radius)


@lru_cache(maxsize=64)
def _name_band(name: str, w: int, h: int, radius: int) -> Image.Image:
    """画像を取りに行かない重なりカード（墓地・除外の下側）: カード上部の名前欄に近い橙の枠に名前を書く。
    下のカードは上のカードの下に隠れ、上端の数十pxだけが見える。"""
    im = Image.new("RGB", (w, h), (0xb2, 0x6c, 0x3a))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, w - 1, h - 1], outline=(0x6e, 0x40, 0x20), width=max(1, round(w / 50)))
    size = max(8, round(h / 12))
    font = _get_font(size, True)
    d.text((max(3, w // 22), max(2, round(h / 30))), _fit_text(d, name or "？", font, w - 2 * max(3, w // 22)),
           fill=(0x1b, 0x12, 0x0a), font=font)
    return _rounded_alpha(im, radius)


@lru_cache(maxsize=8)
def _card_back(w: int, h: int, radius: int) -> Image.Image:
    """裏側（セット）のカード。components.css の .tier-item-wrapper.is-set::after と同じ配色
    （145度のグラデーション・金茶の枠・中央の楕円）で描く。"""
    dx, dy = math.sin(math.radians(145)), -math.cos(math.radians(145))
    length = abs(w * dx) + abs(h * dy)
    stops = [(0.0, (0x5a, 0x30, 0x18)), (0.4, (0x7d, 0x4d, 0x25)), (1.0, (0x3e, 0x20, 0x10))]
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            t = min(1.0, max(0.0, ((x + 0.5 - w / 2) * dx + (y + 0.5 - h / 2) * dy) / length + 0.5))
            for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
                if t <= t1:
                    f = (t - t0) / (t1 - t0)
                    px[x, y] = tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
                    break
    d = ImageDraw.Draw(im)
    ew, eh = w * 0.78 * 0.36, h * 0.78 * 0.393   # viewBox 100x145 の rx36 ry57 の楕円を、幅の78%に拡縮して描く
    d.ellipse([w / 2 - ew, h / 2 - eh, w / 2 + ew, h / 2 + eh], fill=(0x2a, 0x15, 0x08))
    bw = max(1, round(2 * w / 99))
    d.rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, outline=(0xb8, 0x7f, 0x28), width=bw)
    return _rounded_alpha(im, radius)


def _load_zone_icon(name: str, size: int):
    """ゾーンの線画アイコン（static/og/zone-*.png。本番CSSのSVGを透過PNG化したもの）。読めなければ None。
    失敗は覚えない（lru_cache を使わない）。下地側は、読めなかったときキャッシュせず次回また読みに行く。"""
    try:
        im = Image.open(os.path.join(_ICON_DIR, f"zone-{name}.png")).convert("RGBA")
    except Exception as e:
        logger.warning(f"[replay_og] ゾーンアイコン読み込み失敗 {name}: {e}")
        return None
    return im.resize((size, size), Image.LANCZOS)


def _X(x):
    return _BX + (x - _SRC_X0) * _SCALE


def _Y(y):
    return _BY + (y - _SRC_Y0) * _SCALE


def _paste_panel(img: Image.Image, x0: float, y0: float, w: float, h: float, radius: float, tray: bool = False):
    """本番の面（relief-theme.css）を、その面ぶんだけ4倍の角丸マスクで縁をなめらかにして貼る。
    色＝面（トレイ）の色＋粒子の平均分・145度の白→黒の薄いグラデーション・内側の縁の光と影（各約1px）。
    盤面全体を高解像度で作らないので、メモリはほとんど増えない。下地を作る1回だけ呼ばれる。"""
    ss = 4
    ix, iy = math.floor(x0), math.floor(y0)
    fx, fy = x0 - ix, y0 - iy
    tw, th = math.ceil(w + fx) + 1, math.ceil(h + fy) + 1
    box = [fx * ss, fy * ss, (fx + w) * ss - 1, (fy + h) * ss - 1]
    mask = Image.new("L", (tw * ss, th * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=radius * ss, fill=255)

    base = tuple(min(255, c + g) for c, g in zip(_TRAY if tray else _FACE, _GRAIN_SHIFT))
    tile = Image.new("RGBA", (tw, th), base + (255,))
    # linear-gradient(145deg, #ffffff05, #0000000c): 白 α.02 → 黒 α.047（箱 w×h に対する 145度）
    dx, dy = math.sin(math.radians(145)), -math.cos(math.radians(145))
    length = abs(w * dx) + abs(h * dy)
    ov = []
    for y in range(th):
        for x in range(tw):
            t = min(1.0, max(0.0, ((x + 0.5 - fx - w / 2) * dx + (y + 0.5 - fy - h / 2) * dy) / length + 0.5))
            a = 0.02 + (0.047 - 0.02) * t
            ov.append((round(255 * 0.02 * (1 - t) / a), round(255 * 0.02 * (1 - t) / a), round(255 * 0.02 * (1 - t) / a), round(a * 255)))
    grad = Image.new("RGBA", (tw, th))
    grad.putdata(ov)
    tile = Image.alpha_composite(tile, grad)

    # 内側の縁: 面=上に光(169,189,213 α.15)・下に影(7,15,28 α.4)／トレイ=上に影(7,14,24 α.35・約2px)・下に光(169,189,213 α.12)
    off = lambda dy_: ImageChops.offset(mask, 0, dy_)
    if tray:
        top = (7, 14, 24, 0.35, 2)
        bot = (169, 189, 213, 0.12, 1)
    else:
        top = (169, 189, 213, 0.15, 1)
        bot = (7, 15, 28, 0.4, 1)
    for (r, g, b_, al, px), sign in ((top, 1), (bot, -1)):
        edge = ImageChops.subtract(mask, off(sign * px * ss)).resize((tw, th), Image.BOX)
        edge = edge.point(lambda v, al=al: round(v * al))
        tile.paste((r, g, b_, 255), (0, 0), edge)
    img.paste(tile, (ix, iy), mask.resize((tw, th), Image.BOX))


_base_lock = threading.Lock()
_base_cache = None   # 盤面によらない下地（背景・マス・墓地・除外・帯・アイコン）。1200x630 の RGBA を1枚だけ持つ


def _build_base() -> tuple:
    """下地を作る。戻り値: (画像, アイコンを全部読めたか)。"""
    s = _SCALE
    img = Image.new("RGBA", (W, H), _BG + (255,))
    for sx, sy in _SLOTS.values():
        _paste_panel(img, _X(sx), _Y(sy), _SLOT_W * s, _SLOT_H * s, 8 * s)
    for rect in (_GRAVE_RECT, _BANISH_RECT):
        _paste_panel(img, _X(rect[0]), _Y(rect[1]), rect[2] * s, rect[3] * s, 8 * s)
    _paste_panel(img, _X(_SRC_X0), _BAND_Y, (_SRC_X1 - _SRC_X0) * s, _BAND_H, 6, tray=True)   # 手札の帯は本番の手札トレイの色

    ok = True
    icons = [(name, _SLOTS[slot][0] + _SLOT_W / 2, _SLOTS[slot][1] + _SLOT_H / 2, size) for slot, name, size in _SLOT_ICONS]
    icons += [(name, r[0] + r[2] / 2, r[1] + _SIDE_TOP + _SIDE_H / 2, _SIDE_W * _ICON_SIDE_RATIO)
              for name, r in (("grave", _GRAVE_RECT), ("banish", _BANISH_RECT))]
    for name, cx, cy, size in icons:   # 本番は背景画像なので、カードの下に描く
        im = _load_zone_icon(name, max(1, round(size * s)))
        if im is None:
            ok = False
            continue
        img.alpha_composite(im, (round(_X(cx) - im.width / 2), round(_Y(cy) - im.height / 2)))
    return img, ok


def _get_base() -> tuple:
    """下地のコピーと「アイコンを全部読めたか」を返す。読めたときだけプロセス内に1枚キャッシュする。"""
    global _base_cache
    with _base_lock:
        if _base_cache is None:
            img, ok = _build_base()
            if not ok:
                return img, False   # 読めなかった下地は覚えない（次の描画でまた読みに行く）
            _base_cache = img
        return _base_cache.copy(), True


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


def pile_cards(pile: dict) -> list:
    """墓地・除外に描くカード（奥→手前の順・最大 PILE_DRAW_MAX 枚）。旧形式（top のみ）の board も受ける。"""
    if "cards" in pile:
        cards = list(pile.get("cards") or [])
    else:
        cards = [pile["top"]] if pile.get("top") else []
    return cards[-PILE_DRAW_MAX:]


def render_replay_og(board: dict, title: str = "") -> tuple:
    """盤面画像（PNG バイト列）を描く。戻り値: (png_bytes, degraded)。

    degraded=True は、画像を取りに行ったが失敗・時間切れになったカードを含むことを示す
    （画像の無い未発売カードを枠で代用しただけの場合は degraded にしない）。
    呼び出し側は degraded の描画をディスクに固定せず、短期間だけメモリに置く。
    """
    title = (title or "").strip() or "共有リプレイ"

    # 取得対象: フィールド最上段(最大14)・手札の先頭 HAND_DRAW_MAX 枚・墓地/除外の一番上(2)＝最大26枚（裏向き・トークンは取りに行かない）
    hand = board["hand"][:HAND_DRAW_MAX]
    piles = [pile_cards(board["grave"]), pile_cards(board["banish"])]
    cands = [e for e in board["field"] if e["face"] == "up"] + hand
    for cards in piles:   # 墓地・除外で取りに行くのは一番上の1枚だけ（下に重なる分は名前の帯で描く）
        if cards and cards[-1].get("face") != "down":
            cands.append(cards[-1])
    imgs, degraded = _fetch_all([c["name"] for c in cands if _wants_image(c)])

    canvas, base_ok = _get_base()
    degraded = degraded or not base_ok   # アイコンを読めなかった描画はディスクに固定しない
    d = ImageDraw.Draw(canvas)
    s = _SCALE
    X, Y = _X, _Y
    band_x0, band_x1 = X(_SRC_X0), X(_SRC_X1)

    cw, ch, rad = round(_CARD_W * s), round(_CARD_H * s), max(2, round(8 * s))

    def put(card_dict, cx, cy, face_down=False, rotate=False, w=None, h=None, r=None, band=False):
        """カードを中心 (cx, cy)（キャンバス座標）に描く。"""
        w, h, r = w or cw, h or ch, r or rad
        if face_down:
            im = _card_back(w, h, r)
        elif band:
            im = _name_band(card_dict.get("name", ""), w, h, r)
        else:
            src = imgs.get(card_dict["name"]) if _wants_image(card_dict) else None
            im = _card_face(src, card_dict.get("name", ""), w, h, r)
        if rotate:
            im = im.rotate(90, expand=True)   # 守備表示は反時計回りに90度（CSS rotate(-90deg) と同じ向き）
        canvas.alpha_composite(im, (round(cx - im.width / 2), round(cy - im.height / 2)))

    # ── フィールドのカード（守備表示は隣のマスへはみ出すので、本番の z-index と同じく最後に描いて前面に出す）──
    for e in sorted(board["field"], key=lambda e: e["pos"] == "def"):
        no = int(e["slot"].rsplit("-", 1)[1])
        if no not in _SLOTS:
            continue
        sx, sy = _SLOTS[no]
        put(e, X(sx + _SLOT_W / 2), Y(sy + _SLOT_H / 2), face_down=(e["face"] == "down"), rotate=(e["pos"] == "def"))
        if e.get("n", 1) > 1:   # 重なり枚数（エクシーズ素材など）
            nx, ny = X(sx + _SLOT_W) - 18, Y(sy) + 16
            d.ellipse([nx - 13, ny - 13, nx + 13, ny + 13], fill=_YELLOW)
            d.text((nx, ny), str(e["n"]), fill=_BADGE_FG, font=_get_font(16, True), anchor="mm")

    # ── 墓地・除外: 見出しと、枠の中に縦にずらして重ねたカード ──
    lf, cf = _get_font(round(11 * s), True), _get_font(round(11 * s))
    for (label, pile, rect), cards in zip((("墓地", board["grave"], _GRAVE_RECT), ("除外", board["banish"], _BANISH_RECT)), piles):
        rx, ry = rect[0], rect[1]
        lx, ly = X(rx + 9), Y(ry + 15)   # 見出し（本番は枠の左上から (9, 7)・高さ16の行の中央）
        d.text((lx, ly), label, fill=_SUB, font=lf, anchor="lm")
        d.text((lx + d.textlength(label, font=lf) + 5 * s, ly), str(pile["count"]), fill=_COUNT, font=cf, anchor="lm")
        for i, c in enumerate(cards):
            put(c, X(rx + 9 + _SIDE_W / 2), Y(ry + _SIDE_TOP + i * _PILE_STEP + _CARD_H / 2),
                face_down=(c.get("face") == "down"), band=(i < len(cards) - 1))   # 一番上以外は名前の帯

    # ── 上部: ロゴ・タイトル・手数バッジ（盤面の左右端にそろえる）──
    left, right = round(band_x0), round(band_x1)
    cy0 = 34
    lh = 30
    try:
        logo = Image.open(_LOGO_PATH).convert("RGBA")
        logo = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
        canvas.alpha_composite(logo, (left, cy0 - lh // 2))
        x = left + logo.width + 18
    except Exception as e:
        logger.warning(f"[replay_og] ロゴ読み込み失敗: {e}")
        d.text((left, cy0), "強欲なラボ", fill=_TEXT, font=_get_font(26, True), anchor="lm")
        x = left + 100 + 18
    badge_w = 0
    moves = board.get("moves", 0)
    if moves > 0:
        bf = _get_font(24, True)
        txt = f"{moves}手"
        badge_w = round(d.textlength(txt, font=bf)) + 32
        d.rounded_rectangle([right - badge_w, cy0 - 20, right, cy0 + 20], radius=8, fill=_YELLOW)
        d.text((right - badge_w / 2, cy0), txt, fill=_BADGE_FG, font=bf, anchor="mm")
    tf = _get_font(30, True)
    d.text((x, cy0), _fit_text(d, title, tf, right - badge_w - 18 - x), fill=_TEXT, font=tf, anchor="lm")

    # ── 下部: 手札（本番の手札の帯と同じく、カードを少し重ねて並べる）──
    mid = _BAND_Y + _BAND_H / 2
    lab = f"手札 {len(board['hand'])}"
    labf = _get_font(18, True)
    d.text((band_x0 + 14, mid), lab, fill=_SUB, font=labf, anchor="lm")
    right_txt = f"デッキ {board['deckCount']} / EX {board['ex']['count']}"
    rf = _get_font(18)
    d.text((band_x1 - 14, mid), right_txt, fill=_SUB, font=rf, anchor="rm")
    hh = _BAND_H - 6   # 帯の高さの範囲でできるだけ大きく
    hw = round(hh * _CARD_W / _CARD_H)
    rest = len(board["hand"]) - len(hand)
    rest_f = _get_font(20, True)
    rest_w = d.textlength(f"+{rest}", font=rest_f) + 8 if rest > 0 else 0   # 「+N」の置き場
    hx0 = band_x0 + 14 + d.textlength(lab, font=labf) + 16
    avail = band_x1 - 14 - d.textlength(right_txt, font=rf) - 16 - hx0 - rest_w   # 手札カードを並べられる幅
    n = len(hand)
    step = hw + _HAND_GAP   # 収まるなら重ねずに少し間を空けて並べる
    if n > 1 and (n - 1) * step + hw > avail:
        step = max(1.0, (avail - hw) / (n - 1))   # 収まらないときだけ、収まる幅まで重ねる
    for i, c in enumerate(hand):
        put(c, hx0 + hw / 2 + i * step, mid, w=hw, h=hh, r=max(2, round(hh / 18)))
    if rest > 0:   # 描き切らなかった手札の枚数
        d.text((hx0 + (n - 1) * step + hw + 8, mid), f"+{rest}", fill=_TEXT, font=rest_f, anchor="lm")

    out = io.BytesIO()
    canvas.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue(), degraded


# ── キャッシュ・生成の入口 ──

_REPLAY_ID_RE = re.compile(r"[A-Za-z0-9_-]{6,16}")   # secrets.token_urlsafe(8) は11文字
_OG_CACHE_DIR = os.path.join(tempfile.gettempdir(), "cardprice_replay_og_v5")   # 見た目を変えたらバージョンを上げる（古い画像を使わない）
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


def purge_cache(replay_id: str) -> None:
    """その id のディスクキャッシュと、描画に失敗したとき用のメモリキャッシュを消す。
    管理画面で削除・非表示にしたあと、タイトル入りの画像が残って見え続けないようにする。失敗しても例外は投げない。"""
    if not is_valid_replay_id(replay_id):
        return
    try:
        os.remove(_cache_path(replay_id))
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(f"OG画像キャッシュの削除に失敗 {replay_id}: {e}")
    with _degraded_lock:
        _degraded_cache.pop(replay_id, None)


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
