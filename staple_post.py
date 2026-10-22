"""汎用カードの値動き X 投稿（2026-10-21〜）

汎用カード（staple_cards.py のリスト）の最安値が大きく動いた日に、1カード1ポストで知らせる。
値動きの集計は大変動ポスト（x_poster.post_big_movers）と同じ RPC get_price_movers
（共通店舗のみ・複数店が同じ向き）を使い、対象と基準だけを変える。

基準（2026-10-21 ユーザー決定。安くなったら買いたい・上がったら理由が知りたい、で非対称）:
  値下がり: -100円以下 かつ -15%以下
  値上がり: +200円以上 かつ +20%以上
1日に値下がり・値上がり各1件まで。同じカード・レアリティ・向きは7日間投稿しない。

投稿文（ユーザー確認済みの形）:
  【値下がり】聖王の粉砕（シークレット）
  ¥280（9/25 ¥660から -380円）
  https://.../card/聖王の粉砕
画像は render_post_image（カード画像＋最安値の枠＋過去の値動きのグラフ。P2案・和文は M PLUS 2・数字は Outfit）。

python staple_post.py            … 投稿（X_POST_DRY_RUN=1 なら投稿せず画像を staple_post_preview/ に残す）
"""

import os
import shutil
import tempfile
import logging
import urllib.parse
from datetime import datetime, timedelta, date

from constants import JST
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
# クールダウン時の次点繰り上げ用に向きごとに受け取る候補数
CANDIDATES_PER_DIRECTION = 3
# グラフに描く期間（日）。price_history のレアリティ別記録は 2026-07-22 から
HISTORY_DAYS = 90

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


def content_type(m):
    """tweet_log.content_type（クールダウン照会のキー）。大変動ポストの bigmove_ と分ける。"""
    return f"staple_{m['direction']}|{fuzzy_key(m['name'])}|{m['rarity']}"


def format_post_text(m):
    label = "値上がり" if m["direction"] == "up" else "値下がり"
    rarity = f"（{m['rarity']}）" if m["rarity"] else ""
    sign = "+" if m["diff"] > 0 else "-"
    url = f"{x_poster.SITE_URL}/card/{urllib.parse.quote(m['name'])}"
    return "\n".join([
        f"【{label}】{m['name']}{rarity}",
        f"¥{m['today']:,}（{x_poster._format_date(m['date_old'])} ¥{m['yesterday']:,}から {sign}{abs(m['diff']):,}円）",
        url,
    ])


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
        if p <= 10:
            continue
        d = r["recorded_at"][:10]
        daily[d] = min(daily.get(d, p), p)
    # 31日目以前は店舗別の行が消え、日ごとの全店最安（price_daily）に畳まれている。
    # 10円以下を除いた最安（min_price_over10）を使い、上と同じ値になるようにする
    full_dates = {r["recorded_at"][:10] for r in rows}
    for r in _fetch_daily_history(sb, name, rarity, start, date_to):
        d = r["recorded_at"][:10]
        p = r.get("min_price_over10") or 0
        if d in full_dates or p <= 10:
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
BG = (16, 22, 38)          # --bg
PANEL = (23, 31, 49)       # --bg-card
TXT = (239, 243, 251)      # --text
SUB = (168, 180, 201)      # --text-m
GRID = (43, 53, 73)
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

    if len(history) >= 2:
        values = [p for _, p in history]
        lo, hi = min(values), max(values)
        span_days = (date.fromisoformat(history[-1][0]) - date.fromisoformat(history[0][0])).days + 1
        now = m["today"]
        if now == lo and lo != hi:
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

def _recently_posted(sb, m):
    try:
        cutoff = (datetime.now(JST) - timedelta(days=COOLDOWN_DAYS)).isoformat()
        resp = (sb.table("tweet_log").select("tweet_id").eq("content_type", content_type(m))
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
                                           "top_n": 1000, "per_card": 999}).execute().data or []
    except Exception as e:
        logger.warning(f"RPC get_price_movers 呼び出し失敗 — スキップ: {e}")
        return
    candidates = select_staple_movers(rows, names)
    if not candidates:
        print("  基準を満たす値動きなし — 本日は無投稿")
        return

    for direction in ("down", "up"):
        if not dry_run and x_poster._already_posted_today(sb, f"staple_{direction}|"):
            print(f"  [{direction}] 本日分は投稿済み — スキップ")
            continue
        for m in (c for c in candidates if c["direction"] == direction):
            if _recently_posted(sb, m):
                print(f"  [{direction}] {COOLDOWN_DAYS}日以内に投稿済み/照会失敗: {m['name']}（{m['rarity']}）— 次点へ")
                continue
            text = format_post_text(m)
            print(f"\n--- {direction}: {m['name']}（{m['rarity']}） ---\n{text}")
            card_path = x_poster.get_card_image_path(m["name"])
            if not card_path:
                print("  カード画像が取れない — この候補は見送り")
                continue
            fd, out = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            try:
                history = align_history(fetch_history(sb, m["name"], m["rarity"], m["date_new"]), m)
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
                keep = os.path.join(PREVIEW_DIR, f"{direction}_{fuzzy_key(m['name'])}_{m['rarity']}.png")
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
