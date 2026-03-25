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
        return []

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

    movers = []
    for name, dates in card_dates.items():
        sorted_dates = sorted(dates.keys())
        if len(sorted_dates) < 2:
            continue
        today_price = dates[sorted_dates[-1]]
        yesterday_price = dates[sorted_dates[-2]]
        if yesterday_price == 0:
            continue
        diff = today_price - yesterday_price
        pct = round((diff / yesterday_price) * 100, 1)
        if diff == 0:
            continue
        movers.append({
            "name": name, "today": today_price,
            "yesterday": yesterday_price, "diff": diff, "pct": pct,
        })

    if direction == "up":
        return sorted([m for m in movers if m["diff"] > 0], key=lambda x: -x["pct"])[:limit]
    else:
        return sorted([m for m in movers if m["diff"] < 0], key=lambda x: x["pct"])[:limit]


def _truncate(name, max_len=18):
    """長いカード名を省略"""
    return name if len(name) <= max_len else name[:max_len] + "..."


def format_tweet(movers, direction, today_str):
    """投稿テキストを生成"""
    label = "値上がり" if direction == "up" else "値下がり"
    lines = [f"【{label}カード】{today_str}\n"]

    for i, m in enumerate(movers):
        sign = "+" if m["diff"] > 0 else ""
        name = _truncate(m["name"])
        lines.append(
            f"{i+1}. {name} {sign}{m['pct']}%"
            f"({m['yesterday']:,}→{m['today']:,}円)"
        )

    lines.append(f"\n{SITE_URL}")
    lines.append("#遊戯王 #CardPriceis")
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
    today_str = date.today().strftime("%-m/%-d")

    for direction in ("up", "down"):
        movers = get_price_movers(sb, direction, limit=5)
        if not movers:
            label = "値上がり" if direction == "up" else "値下がり"
            print(f"  {label}データなし — スキップ")
            continue

        text = format_tweet(movers, direction, today_str)
        print(f"\n--- {direction} ---")
        print(text)
        post_tweet(text)
