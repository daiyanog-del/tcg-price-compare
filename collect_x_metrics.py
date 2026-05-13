"""X投稿のインプレッション数を取得して tweet_log に記録するスクリプト

15分おきに GitHub Actions から呼ばれる。
tweet_log テーブルから「投稿後30分以上経過 & 未計測」のレコードを抽出し、
X API で public_metrics を取得して書き込む。
新たに計測されたものがあれば Discord に通知する。
"""

import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def get_supabase():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL", "")
    key = os.environ.get("SUPABASE_KEY", "")
    if not url or not key:
        print("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
        raise SystemExit(1)
    return create_client(url, key)


def get_tweepy_client():
    import tweepy
    return tweepy.Client(
        consumer_key=os.environ["X_API_KEY"],
        consumer_secret=os.environ["X_API_SECRET"],
        access_token=os.environ["X_ACCESS_TOKEN"],
        access_token_secret=os.environ["X_ACCESS_TOKEN_SECRET"],
    )


def fetch_metrics(tweet_id: str, client) -> dict | None:
    """tweet_id の public_metrics を取得して返す。失敗時は None。"""
    try:
        resp = client.get_tweet(tweet_id, tweet_fields=["public_metrics"])
        if resp.data and resp.data.public_metrics:
            return dict(resp.data.public_metrics)
    except Exception as e:
        print(f"  metrics取得失敗 (id={tweet_id}): {e}")
    return None


def collect_30min_metrics(sb, client) -> list:
    """30分経過 & 未計測のツイートに metrics_30min を記録する。記録したレコードを返す。"""
    threshold = (datetime.now(JST) - timedelta(minutes=30)).isoformat()
    rows = (
        sb.table("tweet_log")
        .select("tweet_id, posted_at, content_type")
        .is_("metrics_30min", "null")
        .lt("posted_at", threshold)
        .execute()
    )
    if not rows.data:
        print("30分計測: 対象なし")
        return []

    now_str = datetime.now(JST).isoformat()
    recorded = []
    for row in rows.data:
        tid = row["tweet_id"]
        metrics = fetch_metrics(tid, client)
        if metrics is None:
            continue
        sb.table("tweet_log").update({
            "metrics_30min": metrics,
            "metrics_30min_at": now_str,
        }).eq("tweet_id", tid).execute()
        imp = metrics.get("impression_count", "?")
        print(f"  30min記録: id={tid} impression={imp}")
        recorded.append({**row, "metrics": metrics, "window": "30min"})
    return recorded


def collect_24h_metrics(sb, client) -> list:
    """24時間経過 & 未計測のツイートに metrics_24h を記録する。記録したレコードを返す。"""
    threshold = (datetime.now(JST) - timedelta(hours=24)).isoformat()
    rows = (
        sb.table("tweet_log")
        .select("tweet_id, posted_at, content_type")
        .is_("metrics_24h", "null")
        .lt("posted_at", threshold)
        .execute()
    )
    if not rows.data:
        print("24時間計測: 対象なし")
        return []

    now_str = datetime.now(JST).isoformat()
    recorded = []
    for row in rows.data:
        tid = row["tweet_id"]
        metrics = fetch_metrics(tid, client)
        if metrics is None:
            continue
        sb.table("tweet_log").update({
            "metrics_24h": metrics,
            "metrics_24h_at": now_str,
        }).eq("tweet_id", tid).execute()
        imp = metrics.get("impression_count", "?")
        print(f"  24h記録: id={tid} impression={imp}")
        recorded.append({**row, "metrics": metrics, "window": "24h"})
    return recorded


def notify_discord(results: list):
    """計測結果を Discord に通知する。DISCORD_WEBHOOK_URL 未設定時はスキップ。"""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not webhook_url or not results:
        return

    import requests as _req

    label_map = {"movers_up": "値上がりランキング", "movers_down": "値下がりランキング"}
    window_map = {"30min": "投稿後30分", "24h": "投稿後24時間"}

    lines = ["**X投稿 インプレッション計測結果**"]
    for r in results:
        m = r["metrics"]
        label = label_map.get(r["content_type"], r["content_type"])
        window = window_map.get(r["window"], r["window"])
        posted_jst = r["posted_at"][:16].replace("T", " ")
        lines.append(
            f"\n{label}（{posted_jst} 投稿）— {window}\n"
            f"  インプ: {m.get('impression_count', '?')} / "
            f"いいね: {m.get('like_count', '?')} / "
            f"RT: {m.get('retweet_count', '?')} / "
            f"返信: {m.get('reply_count', '?')}"
        )

    message = "\n".join(lines)
    try:
        _req.post(webhook_url, json={"content": message}, timeout=10)
        print(f"  Discord通知送信済み（{len(results)}件）")
    except Exception as e:
        print(f"  Discord通知失敗: {e}")


def main():
    print("=== X metrics 収集 ===")
    sb = get_supabase()
    client = get_tweepy_client()
    recorded_30min = collect_30min_metrics(sb, client)
    recorded_24h = collect_24h_metrics(sb, client)
    notify_discord(recorded_30min + recorded_24h)
    print("完了")


if __name__ == "__main__":
    main()
