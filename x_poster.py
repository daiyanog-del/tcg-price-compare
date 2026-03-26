"""X（Twitter）自動投稿モジュール — 値動きランキングを毎日投稿"""

import os
from datetime import date, datetime, timedelta
from collections import defaultdict


SITE_URL = "https://tcg-price-compare.onrender.com"


def get_price_movers(sb, direction="up", limit=5):
    """Supabaseから値上がり/値下がりランキングを取得"""
    cutoff = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")

    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        resp = (sb.table("price_history")
                .select("card_name, min_price, recorded_at")
                .gte("recorded_at", cutoff)
                .order("recorded_at", desc=False)
                .range(offset, offset + page_size - 1)
                .execute())
        batch = resp.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    if not all_rows:
        return [], None, None

    # 日付ごと・カードごとの最安値（10円以下は除外）
    card_dates = defaultdict(dict)
    for r in all_rows:
        name = r["card_name"]
        d = r["recorded_at"][:10]
        price = r["min_price"]
        if price <= 10:
            continue
        if d not in card_dates[name] or price < card_dates[name][d]:
            card_dates[name][d] = price

    # 全カード共通で比較に使う2日分を特定
    all_dates_set = set()
    for dates in card_dates.values():
        all_dates_set.update(dates.keys())
    all_dates_sorted = sorted(all_dates_set)
    if len(all_dates_sorted) < 2:
        return [], None, None
    date_new = all_dates_sorted[-1]
    date_old = all_dates_sorted[-2]

    movers = []
    for name, dates in card_dates.items():
        if date_new not in dates or date_old not in dates:
            continue
        today_price = dates[date_new]
        yesterday_price = dates[date_old]
        if yesterday_price == 0:
            continue
        diff = today_price - yesterday_price
        if abs(diff) < 100:
            continue
        pct = round((diff / yesterday_price) * 100, 1)
        movers.append({
            "name": name, "today": today_price,
            "yesterday": yesterday_price, "diff": diff, "pct": pct,
        })

    if direction == "up":
        result = sorted([m for m in movers if m["diff"] > 0], key=lambda x: -x["pct"])[:limit]
    else:
        result = sorted([m for m in movers if m["diff"] < 0], key=lambda x: x["pct"])[:limit]
    return result, date_old, date_new


def _truncate(name, max_len=18):
    """長いカード名を省略"""
    return name if len(name) <= max_len else name[:max_len] + "..."


def _format_date(date_str):
    """'2026-03-25' → '3/25' 形式に変換"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.month}/{d.day}"


def format_tweet(movers, direction, date_old, date_new):
    """投稿テキストを生成"""
    label = "値上がり" if direction == "up" else "値下がり"
    period = f"{_format_date(date_old)}→{_format_date(date_new)}"
    lines = [f"【{label}カード】{period}\n"]

    for i, m in enumerate(movers):
        sign = "+" if m["diff"] > 0 else ""
        name = _truncate(m["name"])
        lines.append(
            f"{i+1}. {name} {sign}{m['pct']}%"
            f"({m['yesterday']:,}→{m['today']:,}円)"
        )

    lines.append(f"\n{SITE_URL}")
    lines.append("#遊戯王 #遊戯王OCG #遊戯王相場 #遊戯王高騰 #CardPriceis")
    return "\n".join(lines)


def post_tweet(text):
    """X API v2でツイートを投稿"""
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        print("  X API認証情報が未設定のためスキップ")
        return False

    try:
        import tweepy
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        client.create_tweet(text=text)
        print(f"  投稿成功: {text[:50]}...")
        return True
    except Exception as e:
        print(f"  X投稿失敗: {e}")
        return False


def post_daily_movers(sb):
    """毎日の値動きランキングをXに投稿"""
    print("\n=== X自動投稿 ===")

    for direction in ("up", "down"):
        movers, date_old, date_new = get_price_movers(sb, direction, limit=5)
        if not movers:
            label = "値上がり" if direction == "up" else "値下がり"
            print(f"  {label}データなし — スキップ")
            continue

        text = format_tweet(movers, direction, date_old, date_new)
        print(f"\n--- {direction} ---")
        print(text)
        post_tweet(text)
