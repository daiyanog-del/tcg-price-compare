"""
Web Push 値下がり通知バッチ
collect_prices.py の実行後に GitHub Actions から呼び出す。

処理内容:
1. push_subscriptions テーブルから購読情報を取得
2. 各購読のカードについて7日前比を計算
3. -5% 以上かつ 50円以上下落しているカードがあれば通知送信
4. last_notified_at を更新して24時間以内の重複通知を防止
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from supabase import create_client, Client
from aggregations import daily_min_by_lowest_rarity

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))

# ── 環境変数 ──
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_CLAIMS      = {"sub": "mailto:tcg.price.compare@gmail.com"}

# 値下がり判定の閾値
DROP_PCT = -5.0   # -5% 以上の下落
DROP_ABS = -50    # かつ 50円以上安い

# 同一購読への通知間隔（これ未満なら送らない）
NOTIFY_INTERVAL_HOURS = 20


def aggregate_daily_min(rows: list) -> dict:
    """price_history 行リストから、最安レアリティ系列を代表として
    {card_name: {date_str: min_price}} を返す。（aggregations.py に一元化）
    """
    return daily_min_by_lowest_rarity(rows)


def get_price_drops(sb: Client, card_names: list) -> dict:
    """カード名リストの値下がり情報を返す。
    戻り値: {card_name: {"latest": int, "base_7d": int, "pct": float, "diff": int}}
    閾値を超えていないカードは含まれない。
    """
    if not card_names:
        return {}

    cutoff = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d")
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        resp = (sb.table("price_history")
                .select("card_name, rarity, min_price, recorded_at")
                .in_("card_name", card_names)
                .gte("recorded_at", cutoff)
                .order("recorded_at", desc=False)
                .range(offset, offset + page_size - 1)
                .execute())
        batch = resp.data or []
        all_rows.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size

    card_dates = aggregate_daily_min(all_rows)
    drops = {}
    for name, dates in card_dates.items():
        sorted_dates = sorted(dates.keys())
        if len(sorted_dates) < 2:
            continue
        latest_price = dates[sorted_dates[-1]]
        base_price   = dates[sorted_dates[0]]
        if base_price <= 0:
            continue
        diff = latest_price - base_price
        pct  = round((diff / base_price) * 100, 1)
        if pct <= DROP_PCT and diff <= DROP_ABS:
            drops[name] = {"latest": latest_price, "base_7d": base_price, "pct": pct, "diff": diff}

    return drops


def send_push(subscription: dict, payload: dict) -> bool:
    """1件の購読に Web Push を送信する。失敗時は False を返す。"""
    try:
        from pywebpush import webpush, WebPushException
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {
                    "p256dh": subscription["p256dh"],
                    "auth":   subscription["auth"],
                },
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=VAPID_CLAIMS,
        )
        return True
    except Exception as e:
        logger.warning(f"  Push 送信失敗 ({subscription['endpoint'][:40]}...): {e}")
        return False


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        logger.error("SUPABASE_URL / SUPABASE_KEY が未設定です")
        return
    if not VAPID_PRIVATE_KEY:
        logger.error("VAPID_PRIVATE_KEY が未設定です")
        return

    sb: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # 購読一覧を取得
    resp = sb.table("push_subscriptions") \
        .select("endpoint, p256dh, auth, card_names, last_notified_at") \
        .execute()
    subscriptions = resp.data or []
    logger.info(f"購読数: {len(subscriptions)}")
    if not subscriptions:
        return

    now = datetime.now(JST)
    cutoff_dt = now - timedelta(hours=NOTIFY_INTERVAL_HOURS)

    # 全購読が持つカード名を重複なく収集してまとめてクエリ
    all_card_names = list({
        name
        for sub in subscriptions
        for name in (sub.get("card_names") or [])
    })
    logger.info(f"チェック対象カード: {len(all_card_names)}件")
    all_drops = get_price_drops(sb, all_card_names)
    logger.info(f"値下がりカード: {len(all_drops)}件")

    sent = 0
    for sub in subscriptions:
        # 最近通知済みはスキップ
        last = sub.get("last_notified_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.astimezone(JST) > cutoff_dt:
                    continue
            except Exception:
                pass

        cards = sub.get("card_names") or []
        drop_cards = {n: all_drops[n] for n in cards if n in all_drops}
        if not drop_cards:
            continue

        # 通知メッセージ作成（最大3件表示）
        lines = []
        for name, info in list(drop_cards.items())[:3]:
            lines.append(f"・{name} ¥{info['latest']:,}（{info['pct']:+.1f}%）")
        if len(drop_cards) > 3:
            lines.append(f"…他{len(drop_cards)-3}件")

        payload = {
            "title": f"値下がり {len(drop_cards)}件 | TCGYM",
            "body":  "\n".join(lines),
            "url":   "/?tab=wishlist",
        }

        if send_push(sub, payload):
            sent += 1
            sb.table("push_subscriptions") \
                .update({"last_notified_at": now.isoformat()}) \
                .eq("endpoint", sub["endpoint"]) \
                .execute()
            logger.info(f"  送信済み: {len(drop_cards)}件の値下がり")

    logger.info(f"通知送信完了: {sent}/{len(subscriptions)} 件")


if __name__ == "__main__":
    main()
