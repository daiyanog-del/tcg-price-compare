"""X（Twitter）自動投稿モジュール — 値動きランキングを毎日投稿"""

import os
import tempfile
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from collections import defaultdict

import requests as _requests

SITE_URL = "https://tcg-price-compare.onrender.com"
JST = timezone(timedelta(hours=9))


def get_price_movers(sb, direction="up", limit=5):
    """Supabaseから値上がり/値下がりランキングを取得"""
    cutoff = (datetime.now(JST) - timedelta(days=3)).strftime("%Y-%m-%d")

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
        if diff == 0:
            continue
        pct = round((diff / yesterday_price) * 100, 1)
        movers.append({
            "name": name, "today": today_price,
            "yesterday": yesterday_price, "diff": diff, "pct": pct,
        })

    # 50円以上の変動を優先、なければ全件からフォールバック（app.pyと同じ方針）
    up_all = sorted([m for m in movers if m["diff"] > 0], key=lambda x: -x["pct"])
    down_all = sorted([m for m in movers if m["diff"] < 0], key=lambda x: x["pct"])
    if direction == "up":
        result = ([m for m in up_all if abs(m["diff"]) >= 50] or up_all)[:limit]
    else:
        result = ([m for m in down_all if abs(m["diff"]) >= 50] or down_all)[:limit]
    return result, date_old, date_new


def _truncate(name, max_len=18):
    """長いカード名を省略"""
    return name if len(name) <= max_len else name[:max_len] + "..."


def _format_date(date_str):
    """'2026-03-25' → '3/25' 形式に変換"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.month}/{d.day}"


def _download_image(image_url, label):
    """画像URLをダウンロードして一時ファイルパスを返す。失敗時はNone"""
    try:
        img_resp = _requests.get(image_url, timeout=15, headers={"User-Agent": "CardPriceBot/1.0"})
        if img_resp.status_code != 200:
            return None
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(img_resp.content)
        tmp.close()
        print(f"  カード画像取得成功 [{label}]")
        return tmp.name
    except Exception as e:
        print(f"  カード画像ダウンロード失敗 [{label}]: {e}")
        return None


def get_card_image_path(card_name):
    """カード画像を取得し一時ファイルパスを返す。失敗時はNone。
    取得元の優先順位: カーナベル → YGOPRODECK
    """
    # 1. カーナベルを試みる
    try:
        from scraper import kanabell_card_image_url
        image_url = kanabell_card_image_url(card_name)
        if image_url:
            path = _download_image(image_url, card_name)
            if path:
                return path
        print(f"  カーナベル画像なし [{card_name}] — YGOPRODECKで再試行")
    except Exception as e:
        print(f"  カーナベル画像取得失敗 [{card_name}]: {e} — YGOPRODECKで再試行")

    # 2. YGOPRODECKにフォールバック
    try:
        api_url = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
        resp = _requests.get(
            api_url,
            params={"name": card_name, "language": "ja"},
            timeout=10,
            headers={"User-Agent": "CardPriceBot/1.0"},
        )
        if resp.status_code == 200:
            cards = resp.json().get("data", [])
            if cards:
                image_url = cards[0]["card_images"][0]["image_url"]
                return _download_image(image_url, card_name)
        print(f"  YGOPRODECK失敗 [{card_name}]: HTTP {resp.status_code}")
    except Exception as e:
        print(f"  YGOPRODECK失敗 [{card_name}]: {e}")

    return None


def format_tweet(movers, direction, date_old, date_new):
    """投稿テキストを生成"""
    label = "値上がり" if direction == "up" else "値下がり"
    period = f"{_format_date(date_old)}→{_format_date(date_new)}"
    tags = "#遊戯王 #遊戯王高騰" if direction == "up" else "#遊戯王 #遊戯王相場"

    # No.1カードの個別ページへ直リンク（末尾固定）
    encoded_name = urllib.parse.quote(movers[0]["name"])
    cta = f"\n▶ 最安値を比較する\n{SITE_URL}/card/{encoded_name}"

    lines = [f"【{label}カード】{period}\n"]
    for i, m in enumerate(movers):
        sign = "+" if m["diff"] > 0 else ""
        name = _truncate(m["name"])
        lines.append(
            f"{i+1}. {name} {sign}{m['pct']}%"
            f"({m['yesterday']:,}→{m['today']:,}円)"
        )
    lines.append(cta)
    lines.append(tags)
    text = "\n".join(lines)

    # 280文字を超える場合、末尾のカードから削って収める
    while len(text) > 280 and len(movers) > 1:
        movers.pop()
        lines = [f"【{label}カード】{period}\n"]
        for i, m in enumerate(movers):
            sign = "+" if m["diff"] > 0 else ""
            name = _truncate(m["name"])
            lines.append(
                f"{i+1}. {name} {sign}{m['pct']}%"
                f"({m['yesterday']:,}→{m['today']:,}円)"
            )
        lines.append(cta)
        lines.append(tags)
        text = "\n".join(lines)

    return text


def post_tweet(text, image_path=None, reply_to_id=None):
    """X API v2でツイートを投稿。成功時はtweet_idを返す、失敗時はNone"""
    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    access_token = os.environ.get("X_ACCESS_TOKEN")
    access_token_secret = os.environ.get("X_ACCESS_TOKEN_SECRET")

    if not all([api_key, api_secret, access_token, access_token_secret]):
        print("  X API認証情報が未設定のためスキップ")
        return None

    try:
        import tweepy

        # メディアアップロード（v1.1 API経由）
        media_id_str = None
        if image_path:
            try:
                auth = tweepy.OAuth1UserHandler(api_key, api_secret, access_token, access_token_secret)
                api_v1 = tweepy.API(auth)
                media = api_v1.media_upload(image_path)
                media_id_str = str(media.media_id)  # v2 APIは文字列IDを要求
                print(f"  メディアアップロード成功 (media_id={media_id_str})")
            except Exception as e:
                print(f"  メディアアップロード失敗: {e}")
                # 画像なしで投稿続行

        # ツイート投稿（v2 API）
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_token_secret,
        )
        kwargs = {"text": text}
        if media_id_str:
            kwargs["media_ids"] = [media_id_str]
        if reply_to_id:
            kwargs["in_reply_to_tweet_id"] = str(reply_to_id)

        response = client.create_tweet(**kwargs)
        tweet_id = response.data["id"] if response.data else None
        print(f"  投稿成功 (id={tweet_id}): {text[:50]}...")
        return tweet_id
    except Exception as e:
        print(f"  ツイート投稿失敗: {e}")
        return None


def post_daily_movers(sb):
    """毎日の値動きランキングをXに投稿（値上がり→値下がりのスレッド形式）"""
    print("\n=== X自動投稿 ===")

    up_tweet_id = None

    for direction in ("up", "down"):
        movers, date_old, date_new = get_price_movers(sb, direction, limit=5)
        if not movers:
            label = "値上がり" if direction == "up" else "値下がり"
            print(f"  {label}データなし — スキップ")
            continue

        text = format_tweet(movers, direction, date_old, date_new)
        print(f"\n--- {direction} ---")
        print(text)

        # NO.1カードの画像を取得
        image_path = get_card_image_path(movers[0]["name"])

        # 値下がりは値上がりへのリプライとして投稿
        reply_to = up_tweet_id if direction == "down" else None
        tweet_id = post_tweet(text, image_path=image_path, reply_to_id=reply_to)

        # 一時ファイルを削除
        if image_path:
            try:
                os.unlink(image_path)
            except Exception:
                pass

        if direction == "up":
            up_tweet_id = tweet_id
