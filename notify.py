"""
Web Push 値下がり通知バッチ
collect_prices.py の実行後に GitHub Actions から呼び出す。

処理内容:
1. push_subscriptions テーブルから購読情報を取得
2. 各購読のカードについて監視窓内の最古日と最新日の比較（最大8日）を計算
3. -5% 以上かつ 50円以上下落しているカードがあれば通知送信
4. last_notified_at を更新して24時間以内の重複通知を防止

偽陽性対策のガード（2026-07-10導入。supabase_rpc_movers.sql の値動きランキングと同じ方針）:
  ガード①（共通店舗）: 前日比の計算を、比較する2日の両方に記録がある店舗
    （共通店舗）だけで行う。片方の日にしか行が無い店舗は集計から除外する
  ガード②（複数店確認）: 変動方向と同方向に動いた共通店舗が2店以上ある
    系列のみ通知対象とする
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from supabase import create_client, Client

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

from constants import JST, VAPID_CLAIMS
from rarity import UNKNOWN_RARITY_LABEL

# ── 環境変数 ──
SUPABASE_URL      = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY      = os.environ.get("SUPABASE_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")

# 値下がり判定の閾値
DROP_PCT = -5.0   # -5% 以上の下落
DROP_ABS = -50    # かつ 50円以上安い

# 同一購読への通知間隔（これ未満なら送らない）
NOTIFY_INTERVAL_HOURS = 20


def _representative_rarity(rows: list) -> str | None:
    """1カード分の price_history 行（card_name統一）から代表レアリティを選ぶ。

    ロジックは aggregations.daily_min_by_lowest_rarity と同一（店舗横断で
    (rarity, date) -> 最安値 を求め、最新日に存在するレアリティのうち最安を選択。
    全レアリティで最新日欠損なら期間最小値が最安のレアリティにフォールバック）。
    "(不明)"（rarity抽出失敗の隔離ラベル、フェーズ3 P3）は他に候補がある限り
    代表選定から除外する（aggregations.daily_min_by_lowest_rarity と同じ方針）。
    店舗粒度でのガード①②再計算にレアリティ名自体が必要なため、ここに複製する。
    データなしは None を返す。
    """
    rarity_dates: dict = defaultdict(dict)  # rarity -> {date: min_price}
    for r in rows:
        rarity = r.get("rarity", "") or ""
        date   = r.get("recorded_at", "")[:10]
        price  = r.get("min_price", 0)
        if not date or price <= 10:
            continue
        if date not in rarity_dates[rarity] or price < rarity_dates[rarity][date]:
            rarity_dates[rarity][date] = price

    if not rarity_dates:
        return None
    # "(不明)" は他に候補がある限り除外する（フェーズ3 P3）。
    # 移行前のレガシー空文字 rarity も同様に除外（aggregations と同一方針）
    candidates = {r: d for r, d in rarity_dates.items() if r not in ("", UNKNOWN_RARITY_LABEL)} or rarity_dates

    all_dates = sorted({d for dates in candidates.values() for d in dates})
    if not all_dates:
        return None
    latest_date = all_dates[-1]

    best_rarity, best_price = None, None
    for rarity, dates in candidates.items():
        if latest_date not in dates:
            continue
        p = dates[latest_date]
        if best_price is None or p < best_price:
            best_price, best_rarity = p, rarity

    # フォールバック: 全レアリティで最新日欠損 → 期間最小値が最安のレアリティ
    if best_rarity is None:
        for rarity, dates in candidates.items():
            min_p = min(dates.values())
            if best_price is None or min_p < best_price:
                best_price, best_rarity = min_p, rarity

    return best_rarity


def compute_drop(rows: list) -> dict | None:
    """1カード分の price_history 行（shop列必須、card_name統一）から値下がり判定を行う。

    date_new は監視窓内の最新日で固定。date_old は「date_new との共通店舗
    （両日に記録がある店舗）が2店以上になる最古の日」を窓内で古い順に探索して選ぶ
    （2026-07-10改訂）。窓の絶対最古日を固定で使うと、その日の店舗記録が疎な場合に
    共通店舗が0〜1店になり系列全体が沈黙する偽陰性が生じるため。該当日が窓内に
    見つからない場合は従来どおり通知なし（None）。

    ガード①（共通店舗）: 上記で選んだ2日の両方に記録がある店舗（共通店舗）
      だけで最安値を計算する
    ガード②（複数店確認）: 値下がり方向に動いた共通店舗が2店以上ある場合のみ採用する

    戻り値: {"latest": int, "base_7d": int, "pct": float, "diff": int} または
    ガード未通過・閾値未達なら None。
    """
    rarity = _representative_rarity(rows)
    if rarity is None:
        return None
    rarity_rows = [r for r in rows if (r.get("rarity", "") or "") == rarity]

    shop_dates: dict = defaultdict(dict)  # shop -> {date: min_price}
    for r in rarity_rows:
        shop  = r.get("shop", "")
        date  = r.get("recorded_at", "")[:10]
        price = r.get("min_price", 0)
        if not shop or not date or price <= 10:
            continue
        if date not in shop_dates[shop] or price < shop_dates[shop][date]:
            shop_dates[shop][date] = price

    all_dates = sorted({d for dates in shop_dates.values() for d in dates})
    if len(all_dates) < 2:
        return None
    date_new = all_dates[-1]

    # date_new との共通店舗が2店以上になる最古の日を、窓内を古い順に探索して選ぶ
    date_old = None
    common_shops: list = []
    for candidate in all_dates[:-1]:
        shops = [s for s, dates in shop_dates.items() if candidate in dates and date_new in dates]
        if len(shops) >= 2:
            date_old = candidate
            common_shops = shops
            break
    if date_old is None:
        return None

    base_price   = min(shop_dates[s][date_old] for s in common_shops)
    latest_price = min(shop_dates[s][date_new] for s in common_shops)
    if base_price <= 0:
        return None

    diff = latest_price - base_price
    pct  = round((diff / base_price) * 100, 1)
    if not (pct <= DROP_PCT and diff <= DROP_ABS):
        return None

    # ガード②: 値下がり方向に動いた共通店舗が2店以上
    down_shops = sum(
        1 for s in common_shops if shop_dates[s][date_new] < shop_dates[s][date_old]
    )
    if down_shops < 2:
        return None

    return {"latest": latest_price, "base_7d": base_price, "pct": pct, "diff": diff}


def get_price_drops(sb: Client, card_names: list) -> dict:
    """カード名リストの値下がり情報を返す。
    戻り値: {card_name: {"latest": int, "base_7d": int, "pct": float, "diff": int}}
    ガード①②を通過し、かつ閾値を超えたカードのみ含む。
    """
    if not card_names:
        return {}

    # recorded_at はJST日付のため比較もJST基準（UTCだと0-9時に1日ずれる）
    cutoff = (datetime.now(JST) - timedelta(days=8)).strftime("%Y-%m-%d")
    all_rows = []
    page_size = 1000
    offset = 0
    while True:
        resp = (sb.table("price_history")
                .select("card_name, rarity, shop, min_price, recorded_at")
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

    by_card: dict = defaultdict(list)
    for r in all_rows:
        by_card[r["card_name"]].append(r)

    drops = {}
    for name, rows in by_card.items():
        result = compute_drop(rows)
        if result is not None:
            drops[name] = result

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
