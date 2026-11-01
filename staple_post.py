"""汎用カードの値動き X 投稿（2026-10-21〜）

汎用カード（staple_cards.py のリスト）の最安値が大きく動いた日に、1カード1ポストで知らせる。
値動きの集計は大変動ポスト（x_poster.post_big_movers）と同じ RPC get_price_movers
（共通店舗のみ・複数店が同じ向き）を使い、対象と基準だけを変える。

値下がりだけは1店だけの値下げも採る（2026-10-24 ユーザー決定）。投稿が約束するのは
「今その値段で買える」ことで、ほかの店が同じ向きに動いたかは関係ないため。その代わり、
投稿の直前にその値段をつけていた店をもう一度調べ（recheck_price）、今もその値段（以下）で
在庫があるときだけ投稿する。価格の収集は朝（JST 5〜10時）で投稿は20時なので、半日前の値段を
「今」として出さないためでもある。また、判定日の前7日（前日を含む）のどの日の最安よりも安いときだけ値下がりとして採る
（普段の値段に戻っただけを「値下がり」と呼ばない。WEEK_LOW_DAYS）。値上がりは2店以上のまま（1店だけの値上がりは最安の在庫が
売れただけのことが多く、「上がった理由が知りたい」に合わない）。

基準（2026-10-21 ユーザー決定。安くなったら買いたい・上がったら理由が知りたい、で非対称）:
  値下がり: -100円以下 かつ -15%以下
  値上がり: +200円以上 かつ +20%以上
1日に値下がり・値上がり各1件まで。同じカード・レアリティ・向きは7日間投稿しない。

投稿文（ユーザー確認済みの形）:
  【値下がり】聖王の粉砕（シークレット）
  ¥280（9/25 ¥660から -380円）
  https://.../card/聖王の粉砕?utm_...&utm_content=down
投稿の型（2026-10-29〜。型ごとの効果を比べる試み）: 値下がりのうち過去90日・30日でいちばん安いものは
底値の型（staple_floor）にして、2行目を「¥280　過去90日でいちばん安い値段です」、3行目に前日比を出し、画像に札を付ける。
底値に当たっても奇数日は今までの形で出し staple_floorctl として比べる相手にする（is_floor_day）。
当たらない値下がりは今までの形（staple_down）、値上がりは staple_up。型は tweet_log.content_type の頭と
リンクの utm_content に残し、24時間の閲覧（collect_x_metrics）と GA4 の流入・店へのクリックで比べる。
画像は render_post_image（カード画像＋最安値の枠＋過去の値動きのグラフ。P2案・和文は M PLUS 2・数字は Outfit）。

python staple_post.py            … 投稿（X_POST_DRY_RUN=1 なら投稿せず画像を staple_post_preview/ に残す）
"""

import os
import shutil
import tempfile
import logging
import urllib.parse
from datetime import datetime, timedelta, date

from constants import JST, PRICE_FLOOR
from name_normalize import fuzzy_key
import staple_cards
import x_poster

logger = logging.getLogger(__name__)

# 基準（ユーザー決定値。過去60日の試算では、値下がり側は ±100円/15% で約17件/60日の一部、
# 値上がり側は ±200円/20% で数件。docs/decisions.md 2026-10-21）
DOWN_MAX_DIFF = -100
DOWN_MAX_PCT = -15
UP_MIN_DIFF = 200
UP_MIN_PCT = 20
COOLDOWN_DAYS = 7
# 値下がりを採る、同じ向きに動いた店の最少数（RPC get_price_movers の min_down_shops）。
# 1 にした代わりに投稿直前の取り直し（recheck_price）で確かめる。値上がりは RPC 内で2店固定
MIN_DOWN_SHOPS = 1
# 値下がりは、判定日の前のこの日数（前日を含む）のどの日の最安（全店）よりも安いときだけ採る。
# 普段の値段に戻っただけ（前日だけ安い在庫が切れていた等）を「値下がり」と呼ばないため（2026-10-24
# ユーザー決定。直近28日の1店だけの値下がり56件中20件がこれに当たった）
WEEK_LOW_DAYS = 7  # TODO: calibrate from data
# クールダウン・取り直し不合格時の次点繰り上げ用に向きごとに受け取る候補数。
# 取り直しは1候補につき店1〜2回の検索なので、5件でも店への負荷は小さい
CANDIDATES_PER_DIRECTION = 5  # TODO: calibrate from data（取り直しで落ちる割合を見て決める）
# グラフに描く期間（日）。price_history のレアリティ別記録は 2026-07-22 から
HISTORY_DAYS = 90
# 底値の型（staple_floor）にする期間（日）。長いほうから試し、その期間でいちばん安ければ「過去N日で最安」と書く
# （2026-10-29 ユーザー決定。投稿の型を比べる試み。どれにも当たらない値下がりは今までの形 staple_down で出し、比べる相手にする）
FLOOR_DAYS = (90, 30)
# 期間の頭にこの日数以内の記録があれば「その期間の記録がある」とみなす（記録の始まりが期間の途中なら名乗らない）
FLOOR_COVER_SLACK_DAYS = 3

PREVIEW_DIR = "staple_post_preview"


def select_staple_movers(rows, staple_names, top_n=CANDIDATES_PER_DIRECTION):
    """RPC get_price_movers の行から投稿候補を選ぶ純関数（ネットワーク不要）。

    staple_names: 汎用カード名の集合。表記ゆれ（全角半角など）は fuzzy_key で照合する。
    戻り値: [{"name","rarity","today","yesterday","diff","pct","direction","date_old","date_new"}, ...]
        向きごとに変動率の絶対値が大きい順で最大 top_n 件。
    """
    keys = {fuzzy_key(n) for n in staple_names}
    best = {}
    for r in rows:
        name = r.get("card_name") or ""
        if fuzzy_key(name) not in keys:
            continue
        today = int(r.get("today_price") or 0)
        prev = int(r.get("prev_price") or 0)
        if today <= 0 or prev <= 0:
            continue
        diff = today - prev
        pct = round(diff / prev * 100, 1)
        if diff >= UP_MIN_DIFF and pct >= UP_MIN_PCT:
            direction = "up"
        elif diff <= DOWN_MAX_DIFF and pct <= DOWN_MAX_PCT:
            direction = "down"
        else:
            continue
        rarity = r.get("rarity") or ""
        key = (fuzzy_key(name), rarity)
        cand = {"name": name, "rarity": rarity, "today": today, "yesterday": prev,
                "diff": diff, "pct": pct, "direction": direction,
                "date_old": r.get("date_old"), "date_new": r.get("date_new")}
        if key not in best or abs(pct) > abs(best[key]["pct"]):
            best[key] = cand
    result = []
    for direction in ("down", "up"):
        c = sorted((m for m in best.values() if m["direction"] == direction),
                   key=lambda m: (-abs(m["pct"]), m["name"]))
        result.extend(c[:top_n])
    return result


# 向きごとの投稿の型（tweet_log.content_type の頭・リンクの utm_content）。値下がりは底値（floor）と今までの形（down）の2つ
# floorctl は「底値に当たるが今までの形で出した」比べる相手。値下がりは前7日の最安を下回るものだけ採るので
# ほとんどが底値に当たり、当たらないもの（down）だけでは比べる相手がほぼ出ない（10-25〜29 の実投稿4件はすべて当たった）
KINDS_OF = {"down": ("floor", "floorctl", "down"), "up": ("up",)}


def is_floor_day(date_str):
    """底値に当たる値下がりを底値の形で出す日か。偶数日（日付の通し番号）は底値の形、奇数日は今までの形で出して比べる。"""
    return date.fromisoformat(date_str).toordinal() % 2 == 0


def post_kind(m):
    """投稿の型。値下がりで floor_days（過去N日で最安）があれば、floor_show（既定 True）で floor／floorctl、
    なければ down。値上がりは up。"""
    if m["direction"] == "down" and m.get("floor_days"):
        return "floor" if m.get("floor_show", True) else "floorctl"
    return m["direction"]


def _card_key(m):
    return f"{fuzzy_key(m['name'])}|{m['rarity']}"


def content_type(m):
    """tweet_log.content_type（クールダウン照会のキー・型ごとの集計のキー）。大変動ポストの bigmove_ と分ける。"""
    return f"staple_{post_kind(m)}|{_card_key(m)}"


def card_url(m):
    """投稿に載せるカードのページ。どの型の投稿から来たかを GA4 で見分けられるよう utm を付ける。"""
    return (f"{x_poster.SITE_URL}/card/{urllib.parse.quote(m['name'])}"
            f"?utm_source=x&utm_medium=social&utm_campaign=staple&utm_content={post_kind(m)}")


def format_post_text(m):
    label = "値上がり" if m["direction"] == "up" else "値下がり"
    rarity = f"（{m['rarity']}）" if m["rarity"] else ""
    sign = "+" if m["diff"] > 0 else "-"
    change = f"{x_poster._format_date(m['date_old'])} ¥{m['yesterday']:,}から {sign}{abs(m['diff']):,}円"
    if post_kind(m) == "floor":
        lines = [f"¥{m['today']:,}　過去{m['floor_days']}日でいちばん安い値段です", f"（{change}）"]
    else:
        lines = [f"¥{m['today']:,}（{change}）"]
    return "\n".join([f"【{label}】{m['name']}{rarity}", *lines, card_url(m)])


def floor_days(history, m):
    """判定日（date_new）の値段 today が、その前の何日でいちばん安いか（FLOOR_DAYS の長いほうから）。当たらなければ None。

    history は fetch_history の日ごとの全店最安（align_history の前）。判定日とそれ以降の日は比べる相手に含めない。
    同じ値段が前にあっても「いちばん安い」と言えるので含む（<=）。期間の頭に記録が無いとき（記録の始まりが
    期間の途中）は、その期間を名乗らない。"""
    end = date.fromisoformat(m["date_new"])
    prior = [(date.fromisoformat(d), p) for d, p in history if date.fromisoformat(d) < end]
    if not prior:
        return None
    first = prior[0][0]
    for days in FLOOR_DAYS:
        start = end - timedelta(days=days)
        if first > start + timedelta(days=FLOOR_COVER_SLACK_DAYS):
            continue
        window = [p for d, p in prior if d >= start]
        if window and m["today"] <= min(window):
            return days
    return None


def _fetch_daily_history(sb, name, rarity, start, date_to):
    """price_daily（畳んだ日ごとの全店最安）の行。ビュー未作成・失敗なら空（店舗別の行だけで描く）。"""
    rows, offset = [], 0
    try:
        while True:
            resp = (sb.table("price_daily").select("min_price_over10, recorded_at")
                    .eq("card_name", name).eq("rarity", rarity)
                    .gte("recorded_at", start).lte("recorded_at", date_to)
                    .order("recorded_at").order("shop").range(offset, offset + 999).execute())
            batch = resp.data or []
            rows.extend(batch)
            if len(batch) < 1000:
                break
            offset += 1000
            if offset >= 10000:  # 暴走ガード（/api/price-history と同じ。1枚1レアリティなら100行程度）
                logger.error(f"日ごとの最安取得: 10ページ超過で打ち切り [{name}]")
                break
    except Exception as e:
        logger.warning(f"日ごとの最安取得失敗（店舗別の行だけで描く） [{name}]: {e}")
        return []
    return rows


def fetch_history(sb, name, rarity, date_to):
    """そのレアリティの日ごとの最安値（全店）。[(date, price), ...] 昇順。"""
    end = date.fromisoformat(date_to)
    start = (end - timedelta(days=HISTORY_DAYS - 1)).isoformat()
    rows, offset = [], 0
    while True:
        resp = (sb.table("price_history").select("min_price, recorded_at")
                .eq("card_name", name).eq("rarity", rarity)
                .gte("recorded_at", start).lte("recorded_at", date_to + "T23:59:59")
                .order("recorded_at").range(offset, offset + 999).execute())
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        offset += 1000
    daily = {}
    for r in rows:
        p = r.get("min_price") or 0
        if p < PRICE_FLOOR:
            continue
        d = r["recorded_at"][:10]
        daily[d] = min(daily.get(d, p), p)
    # 15日目以前は店舗別の行が消え（constants.PRICE_RETENTION_DAYS）、日ごとの全店最安（price_daily）に畳まれている。
    # PRICE_FLOOR 未満を除いた最安（min_price_over10。名前は「10円超」だった頃のまま）を使い、上と同じ値になるようにする
    full_dates = {r["recorded_at"][:10] for r in rows}
    for r in _fetch_daily_history(sb, name, rarity, start, date_to):
        d = r["recorded_at"][:10]
        p = r.get("min_price_over10") or 0
        if d in full_dates or p < PRICE_FLOOR:
            continue
        daily[d] = min(daily.get(d, p), p)
    return sorted(daily.items())


def align_history(history, m):
    """グラフの最後の2日を、投稿の判定に使った値（共通店舗のみの最安値）にそろえる。
    全店の最安値と共通店舗の最安値は食い違うことがあり、見出しの値段と線の端がずれないようにする。"""
    h = dict(history)
    if m.get("date_old"):
        h[m["date_old"]] = m["yesterday"]
    if m.get("date_new"):
        h[m["date_new"]] = m["today"]
    return sorted(h.items())


# ── 画像（P2案） ──

W, H = 1200, 675
BG = (25, 27, 33)          # --bg
PANEL = (32, 35, 43)       # --bg-card
TXT = (239, 243, 251)      # --text
SUB = (168, 180, 201)      # --text-m
GRID = (53, 56, 67)
LINE = (174, 197, 255)     # --accent-l
UP_COLOR = (239, 68, 68)   # 販売の値上がり（サイトのランキングと同じ）
DOWN_COLOR = (34, 197, 94) # 販売の値下がり

_ROOT = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(_ROOT, "static", "brand", "logo-lockup.png")


def _font_paths():
    """(和文, 数字, 和文の予備) のフォントファイル。STAPLE_FONT_DIR に MPLUS2.ttf / Outfit.ttf /
    NotoSansJP.ttf を置く（post-staple-movers.yml が google/fonts から取得する）。
    和文は M PLUS 2（2026-10-21 ユーザー選定。英語・カタカナの多い名前でも字間が詰まって見える）。"""
    d = os.environ.get("STAPLE_FONT_DIR", "")
    jp = os.path.join(d, "MPLUS2.ttf")
    num = os.path.join(d, "Outfit.ttf")
    fallback = os.path.join(d, "NotoSansJP.ttf")
    if not (os.path.isfile(jp) and os.path.isfile(num)):
        raise FileNotFoundError(f"フォントが見つからない: STAPLE_FONT_DIR={d!r}")
    return jp, num, (fallback if os.path.isfile(fallback) else None)


_CMAP_CACHE = {}


def _missing_chars(path, text):
    """フォントに字形が無い文字。PIL は無い字を黙って抜かすので先に調べる
    （Zen Old Mincho で「－」が抜けた。2026-10-21）。fontTools が無ければ調べない。"""
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        return set()
    if path not in _CMAP_CACHE:
        _CMAP_CACHE[path] = set(TTFont(path, lazy=True).getBestCmap().keys())
    return {c for c in text if not c.isspace() and ord(c) not in _CMAP_CACHE[path]}


def name_font_path(name, jp, fallback):
    """カード名を描くフォント。M PLUS 2 に無い字（難しい漢字・記号）があれば予備の Noto Sans JP。"""
    missing = _missing_chars(jp, name)
    if missing and fallback and not _missing_chars(fallback, name):
        logger.info(f"M PLUS 2 に無い字 {sorted(missing)} — 名前は Noto Sans JP で描く: {name}")
        return fallback
    if missing:
        logger.warning(f"カード名にフォントの無い字がある（抜けて描かれる）: {name} {sorted(missing)}")
    return jp


def _font(path, size, weight):
    from PIL import ImageFont
    f = ImageFont.truetype(path, size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass  # 可変でないフォントはそのまま
    return f


_RARITY_CSS = os.path.join(_ROOT, "templates", "index.html")
_DEFAULT_BADGE = ((174, 197, 255), (74, 124, 255, 0.1))  # .rb の既定（--accent-l）


def _parse_css_color(v):
    v = v.strip()
    if v.startswith("#") and len(v) == 7:
        return tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)), None
    if v.startswith("rgba("):
        r, g, b, a = [x.strip() for x in v[5:-1].split(",")]
        return (int(r), int(g), int(b)), float(a)
    if v == "var(--text-m)":
        return SUB, None
    return None, None


def rarity_badge_colors(rarity):
    """サイトのレアリティの札（.rb-<slug> の --rc-color / --rc-bg）と同じ色を
    templates/index.html から読む。(文字色, 背景色) を返す。背景は画像の地の色に重ねた結果。"""
    import re
    from rarity import slug_of
    fg, bg = _DEFAULT_BADGE
    try:
        css = open(_RARITY_CSS, encoding="utf-8").read()
        m = re.search(r"\.rb-" + re.escape(slug_of(rarity)) + r"\{--rc-color:([^;]+);--rc-bg:([^}]+)\}", css)
        if m:
            c, _ = _parse_css_color(m.group(1))
            b, a = _parse_css_color(m.group(2))
            if c:
                fg = c
            if b and a is not None:
                bg = (*b, a)
    except Exception as e:
        logger.warning(f"レアリティの色を読めない（既定色で描く）: {rarity}: {e}")
    r, g, b, a = bg
    blended = tuple(round(BG[i] * (1 - a) + (r, g, b)[i] * a) for i in range(3))
    return fg, blended


def _fit(draw, text, path, size, weight, max_w, min_size):
    while size > min_size and draw.textlength(text, font=_font(path, size, weight)) > max_w:
        size -= 2
    return _font(path, size, weight)


def render_post_image(card_image_path, m, history, out_path):
    from PIL import Image, ImageDraw
    jp, num, fallback = _font_paths()
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    col = UP_COLOR if m["direction"] == "up" else DOWN_COLOR

    card = Image.open(card_image_path).convert("RGB")
    ch = 575
    cw = int(card.width * ch / card.height)
    img.paste(card.resize((cw, ch), Image.LANCZOS), (60, 50))
    x, xr = 60 + cw + 50, W - 50

    name_font = name_font_path(m["name"], jp, fallback)
    d.text((x, 56), m["name"], font=_fit(d, m["name"], name_font, 34, 700, xr - x, 22), fill=TXT)
    if m["rarity"]:
        # サイトのランキングと同じレアリティの札（色・角丸・太字）
        fg, bg = rarity_badge_colors(m["rarity"])
        bf = _font(jp, 22, 700)
        tw = d.textlength(m["rarity"], font=bf)
        d.rounded_rectangle((x, 114, x + tw + 28, 148), 6, fill=bg)
        d.text((x + 14, 131), m["rarity"], font=bf, fill=fg, anchor="lm")

    d.rounded_rectangle((x, 172, xr, 342), 10, fill=PANEL)
    d.text((x + 24, 190), "最安値", font=_font(jp, 18, 400), fill=SUB)
    d.text((x + 22, 212), f"¥{m['today']:,}", font=_font(num, 68, 600), fill=TXT)
    sign = "+" if m["diff"] > 0 else "-"
    change = (f"{x_poster._format_date(m['date_old'])} ¥{m['yesterday']:,} → "
              f"{sign}¥{abs(m['diff']):,} ({sign}{abs(m['pct'])}%)")
    d.text((x + 24, 304), change, font=_font(num, 22, 500), fill=col)
    if post_kind(m) == "floor":
        # 底値の型だけ、最安値の枠の右上に「過去N日で最安」の札（値下がりの色の塗り・墨色の字）
        chip = f"過去{m['floor_days']}日で最安"
        cf = _font(jp, 22, 700)
        cw_ = d.textlength(chip, font=cf)
        d.rounded_rectangle((xr - 20 - cw_ - 28, 188, xr - 20, 224), 6, fill=col)
        d.text((xr - 20 - 14, 206), chip, font=cf, fill=BG, anchor="rm")

    if len(history) >= 2:
        values = [p for _, p in history]
        lo, hi = min(values), max(values)
        span_days = (date.fromisoformat(history[-1][0]) - date.fromisoformat(history[0][0])).days + 1
        now = m["today"]
        if post_kind(m) == "floor":
            verdict = ""  # 枠の札と同じことを二度書かない
        elif now == lo and lo != hi:
            verdict = f"　過去{span_days}日で最安"
        elif now == hi and lo != hi:
            verdict = f"　過去{span_days}日で最高"
        else:
            verdict = ""
        d.text((x, 370), "価格推移" + verdict, font=_font(jp, 20, 500), fill=SUB)

        top, bot = 412, 530
        x0, x1 = x, xr - 100
        d0 = date.fromisoformat(history[0][0])
        total = max(span_days - 1, 1)
        rng = max(hi - lo, 1)
        pts = []
        for ds, v in history:  # 階段線（サイトの価格推移と同じ）
            px = x0 + (x1 - x0) * (date.fromisoformat(ds) - d0).days / total
            py = bot - (v - lo) / rng * (bot - top)
            if pts:
                pts.append((px, pts[-1][1]))
            pts.append((px, py))
        d.line([(x0, top), (x1, top)], fill=GRID)
        d.line([(x0, bot), (x1, bot)], fill=GRID)
        d.line(pts, fill=LINE, width=2)
        ex, ey = pts[-1]
        d.ellipse((ex - 6, ey - 6, ex + 6, ey + 6), fill=col)
        small = _font(num, 20, 400)
        d.text((x1 + 12, top), f"¥{hi:,}", font=small, fill=SUB, anchor="lm")
        d.text((x1 + 12, bot), f"¥{lo:,}", font=small, fill=SUB, anchor="lm")
        dates = _font(num, 18, 400)
        d.text((x0, bot + 10), x_poster._format_date(history[0][0]), font=dates, fill=SUB)
        d.text((x1, bot + 10), x_poster._format_date(history[-1][0]), font=dates, fill=SUB, anchor="ra")

    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo.thumbnail((190, 60))
    img.paste(logo, (W - logo.width - 36, H - logo.height - 22), logo)
    img.save(out_path, "PNG")
    return out_path


# ── 投稿 ──

def _passes_threshold(direction, diff, pct):
    if direction == "up":
        return diff >= UP_MIN_DIFF and pct >= UP_MIN_PCT
    return diff <= DOWN_MAX_DIFF and pct <= DOWN_MAX_PCT


def week_low(sb, m):
    """判定日（date_new）の前 WEEK_LOW_DAYS 日（前日を含む）の、そのレアリティの最安値（全店・PRICE_FLOOR 未満は除く）。
    記録が無ければ None。照会に失敗したら例外をそのまま上げる（呼び出し側で見送る）。"""
    end = date.fromisoformat(m["date_new"])
    start = (end - timedelta(days=WEEK_LOW_DAYS)).isoformat()
    last = (end - timedelta(days=1)).isoformat()
    low, offset = None, 0
    while True:
        resp = (sb.table("price_history").select("min_price, recorded_at, shop")
                .eq("card_name", m["name"]).eq("rarity", m["rarity"])
                .gte("recorded_at", start).lte("recorded_at", last)
                .order("recorded_at").order("shop").range(offset, offset + 999).execute())
        batch = resp.data or []
        for r in batch:
            p = r.get("min_price") or 0
            if p >= PRICE_FLOOR and (low is None or p < low):
                low = p
        if len(batch) < 1000:
            return low
        offset += 1000


def cheapest_shops(sb, m):
    """判定に使った日（date_new）に、その値段（today）をつけていた店の名前（並びは名前順）。
    照会に失敗したら空（取り直せないので投稿しない）。"""
    try:
        resp = (sb.table("price_history").select("shop, min_price")
                .eq("card_name", m["name"]).eq("rarity", m["rarity"])
                .eq("recorded_at", m["date_new"])
                # 1カード×1レアリティ×1日は店の数（最大7行）なので1ページで足りる
                .order("shop").range(0, 999).execute())
    except Exception as e:
        logger.warning(f"最安の店の照会失敗 [{m['name']}（{m['rarity']}）]: {e}")
        return []
    return sorted({r["shop"] for r in (resp.data or [])
                   if r.get("shop") and (r.get("min_price") or 0) == m["today"]})


def recheck_price(m, shops, fetch=None):
    """投稿直前に、その値段をつけていた店をもう一度調べ、今の通常品の最安値を返す。

    取り方は夜間の収集と同じ（scraper.compare_prices → price_persist.build_min_price_rows。
    売り切れ・PRICE_FLOOR 未満を除く）。ただし通常品（状態難でない出品）だけで数える。収集は通常品が
    無いと状態難の値段で代用するが、「今この値段で買える」の中身が状態難に変わるのを防ぐため。
    戻り値: (値段, 店, 理由) — 店が複数なら安いほう。どの店でも取れなければ値段と店は None で、
    理由に店ごとの結果（取得失敗／在庫なし など）を入れる（見送りの原因をログで切り分けるため）。
    fetch はテスト用の差し替え口（既定は scraper.compare_prices）。
    """
    from price_persist import build_min_price_rows, _is_normal_condition
    if fetch is None:
        from scraper import compare_prices as fetch
    today = datetime.now(JST).strftime("%Y-%m-%d")
    best_price, best_shop, notes = None, None, []
    for shop in shops:
        status = {}
        # カードラボは OF をレアリティ欄に書かず、印の無い行は遊々亭の行と突き合わせて OF と判定する
        # （overframe.py）。カードラボだけで取り直すと OF の行が見つからず見送りになるので遊々亭も取る
        fetch_shops = [shop, "遊々亭"] if shop == "カードラボ" and m["rarity"].startswith("OF") else [shop]
        try:
            results = fetch(m["name"], shop_names=fetch_shops, status_out=status)
        except Exception as e:
            notes.append(f"{shop}:取得失敗({e})")
            continue
        st = status.get(shop)
        if st is None:
            # compare_prices は休止中・一覧に無い店を黙って飛ばす
            notes.append(f"{shop}:取得対象外")
            continue
        if st.get("exception") or (st.get("fetch_errors", 0) > 0 and st.get("count", 0) == 0):
            notes.append(f"{shop}:取得失敗（店側エラー — 在庫なしとは区別）")
            continue
        normal = [it for it in (results or [])
                  if _is_normal_condition(it.get("shop", ""), it.get("condition", ""))]
        price = None
        for r in build_min_price_rows(m["name"], normal, today):
            if r["shop"] == shop and r["rarity"] == m["rarity"]:
                price = r["min_price"]
        if price is None:
            notes.append(f"{shop}:そのレアリティの通常品の在庫なし")
            continue
        notes.append(f"{shop}:¥{price:,}")
        if best_price is None or price < best_price:
            best_price, best_shop = price, shop
    return best_price, best_shop, "・".join(notes) or "店なし"


def apply_recheck(m, price):
    """取り直した値段で候補を作り直す。次のどれかなら None:
    取り直せない／判定時の値段より高い（ほかの店のほうが安いかもしれず「最安値」と言えない）／
    基準を満たさない。比べる相手（前日の値段 yesterday）はそのまま。"""
    if price is None or price > m["today"]:
        return None
    diff = price - m["yesterday"]
    pct = round(diff / m["yesterday"] * 100, 1)
    if not _passes_threshold(m["direction"], diff, pct):
        return None
    return {**m, "today": price, "diff": diff, "pct": pct}


def _recently_posted(sb, m):
    """同じカード・レアリティ・向きを COOLDOWN_DAYS 日以内に出したか。型が違っても同じ向きなら数える（底値と今までの形の値下がりなど）。"""
    try:
        cutoff = (datetime.now(JST) - timedelta(days=COOLDOWN_DAYS)).isoformat()
        types = [f"staple_{k}|{_card_key(m)}" for k in KINDS_OF[m["direction"]]]
        resp = (sb.table("tweet_log").select("tweet_id").in_("content_type", types)
                .gte("posted_at", cutoff).limit(1).execute())
        return bool(resp.data)
    except Exception as e:
        # 照会できないときは投稿しない（同じカードを連投するよりは1日見送るほうがよい）
        logger.warning(f"tweet_log クールダウン照会失敗 — 見送り: {m['name']}（{m['rarity']}）: {e}")
        return True


def post_staple_movers(sb):
    print("\n=== X自動投稿（汎用カードの値動き） ===")
    dry_run = os.environ.get("X_POST_DRY_RUN") == "1"
    names = staple_cards.load_staple_names(sb)
    if not names:
        print("  汎用カードのリストが空 — スキップ")
        return
    print(f"  汎用カード {len(names)}枚")

    cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")
    try:
        rows = sb.rpc("get_price_movers", {"cutoff_date": cutoff, "min_diff": 50,
                                           "top_n": 1000, "per_card": 999,
                                           "min_down_shops": MIN_DOWN_SHOPS}).execute().data or []
    except Exception as e:
        logger.warning(f"RPC get_price_movers 呼び出し失敗 — スキップ: {e}")
        # 無投稿の日と区別できるよう知らせる（関数の作り直しを本番に入れる前にコードが出た場合など）
        from discord_notify import send_discord_message
        send_discord_message(f"[汎用カード投稿] RPC get_price_movers 呼び出し失敗: {e}")
        return
    candidates = select_staple_movers(rows, names)
    if not candidates:
        print("  基準を満たす値動きなし — 本日は無投稿")
        return

    for direction in ("down", "up"):
        if not dry_run and any(x_poster._already_posted_today(sb, f"staple_{k}|") for k in KINDS_OF[direction]):
            print(f"  [{direction}] 本日分は投稿済み — スキップ")
            continue
        for m in (c for c in candidates if c["direction"] == direction):
            if _recently_posted(sb, m):
                print(f"  [{direction}] {COOLDOWN_DAYS}日以内に投稿済み/照会失敗: {m['name']}（{m['rarity']}）— 次点へ")
                continue
            if direction == "down":
                # 直近の最安を下回っていなければ、普段の値段に戻っただけとみて採らない
                try:
                    low = week_low(sb, m)
                except Exception as e:
                    print(f"  [down] 直近{WEEK_LOW_DAYS}日の最安の照会失敗 — 見送り: {m['name']}（{m['rarity']}）: {e}")
                    continue
                if low is not None and m["today"] >= low:
                    print(f"  [down] 直近{WEEK_LOW_DAYS}日に ¥{low:,} があった（今回 ¥{m['today']:,}）— 値下がりとみなさず次点へ:"
                          f" {m['name']}（{m['rarity']}）")
                    continue
                # 投稿直前の取り直し。不合格は投稿していないので次点へ進んでよい
                shops = cheapest_shops(sb, m)
                price, shop, why = recheck_price(m, shops) if shops else (None, None, "最安の店が見つからない")
                checked = apply_recheck(m, price)
                if checked is None:
                    print(f"  [down] 取り直しで確かめられず: {m['name']}（{m['rarity']}）"
                          f" 判定時¥{m['today']:,}・今 {why} — 次点へ")
                    continue
                print(f"  [down] 取り直し: {shop} ¥{price:,}（判定時 ¥{m['today']:,}）")
                m = checked
            try:
                raw_history = fetch_history(sb, m["name"], m["rarity"], m["date_new"])
            except Exception as e:
                logger.warning(f"価格推移の取得に失敗 — この候補は見送り: {e}")
                continue
            if direction == "down":
                m = {**m, "floor_days": floor_days(raw_history, m), "floor_show": is_floor_day(m["date_new"])}
                if m["floor_days"]:
                    shown = "底値の形" if m["floor_show"] else "今までの形（比べる相手の日）"
                    print(f"  [down] 過去{m['floor_days']}日で最安 — {shown}で出す")
            text = format_post_text(m)
            print(f"\n--- {direction}: {m['name']}（{m['rarity']}） ---\n{text}")
            card_path = x_poster.get_card_image_path(m["name"])
            if not card_path:
                print("  カード画像が取れない — この候補は見送り")
                continue
            fd, out = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                history = align_history(raw_history, m)
                render_post_image(card_path, m, history, out)
            except Exception as e:
                logger.warning(f"投稿画像の作成に失敗 — この候補は見送り: {e}")
                for p in (card_path, out):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                continue
            if dry_run:
                os.makedirs(PREVIEW_DIR, exist_ok=True)
                keep = os.path.join(PREVIEW_DIR, f"{post_kind(m)}_{fuzzy_key(m['name'])}_{m['rarity']}.png")
                shutil.copy(out, keep)
                print(f"  画像（試し実行）: {keep}")
            tweet_id = x_poster.post_tweet(text, image_paths=[out], require_media=True)
            for p in (card_path, out):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            if tweet_id:
                try:
                    sb.table("tweet_log").insert({
                        "tweet_id": str(tweet_id),
                        "posted_at": datetime.now(JST).isoformat(),
                        "content_type": content_type(m),
                        "parent_tweet_id": None,
                    }).execute()
                except Exception as e:
                    print(f"  tweet_log 書き込み失敗（投稿自体は成功）: {e}")
            else:
                print("  投稿されず（試し実行・API未設定・失敗のいずれか）")
            # 1件試したらこの向きは終わり（失敗時に次点で連打しない。大変動ポストと同じ）
            break


if __name__ == "__main__":
    from supabase import create_client
    _url, _key = os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_KEY", "")
    if not _url or not _key:
        raise SystemExit("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
    post_staple_movers(create_client(_url, _key))
