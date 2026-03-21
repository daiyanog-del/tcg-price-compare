"""
価格履歴データ収集スクリプト
============================
GitHub Actionsで毎日実行し、監視対象カードの価格をSupabaseに記録する。
"""

import os
import sys
import time
from datetime import datetime, date

from supabase import create_client, Client
from scraper import compare_prices, SHOPS

# ── Supabase接続 ──
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# カード間の待機秒数（店舗への負荷軽減）
WAIT_BETWEEN_CARDS = 2.0

# 古いデータの保持日数
RETENTION_DAYS = 90

# GitHub Actionsからアクセスできない店舗をスキップ
SKIP_SHOPS_IN_CI = {"遊々亭"}


def get_supabase() -> Client:
    """Supabaseクライアントを取得"""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("エラー: SUPABASE_URL と SUPABASE_KEY を環境変数に設定してください")
        sys.exit(1)
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def fetch_tracked_cards(sb: Client) -> list[str]:
    """監視対象カードの一覧を取得"""
    resp = sb.table("tracked_cards") \
        .select("card_name") \
        .eq("active", True) \
        .execute()
    cards = [row["card_name"] for row in resp.data]
    print(f"監視対象カード: {len(cards)}件")
    return cards


def collect_and_save(sb: Client, card_name: str, today: str) -> int:
    """1枚のカードの価格を取得してSupabaseに保存。保存した行数を返す"""
    # GitHub Actions環境ではブロックされる店舗をスキップ
    available_shops = [name for name, _ in SHOPS if name not in SKIP_SHOPS_IN_CI]

    try:
        results = compare_prices(card_name, shop_names=available_shops)
    except Exception as e:
        print(f"  スクレイピング失敗 [{card_name}]: {e}")
        return 0

    if not results:
        print(f"  結果なし [{card_name}]")
        return 0

    # 店舗×レアリティごとの最安値を抽出
    min_prices = {}
    for item in results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price <= 0:
            continue

        key = (item.get("shop", ""), item.get("rarity", ""))
        if key not in min_prices or price < min_prices[key]:
            min_prices[key] = price

    if not min_prices:
        print(f"  有効な価格なし [{card_name}]")
        return 0

    # Supabaseに保存
    rows = []
    for (shop, rarity), price in min_prices.items():
        rows.append({
            "card_name": card_name,
            "shop": shop,
            "rarity": rarity,
            "min_price": price,
            "recorded_at": today,
        })

    try:
        sb.table("price_history").insert(rows).execute()
        print(f"  保存完了 [{card_name}]: {len(rows)}件")
        return len(rows)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]: {e}")
        return 0


def cleanup_old_data(sb: Client):
    """保持期間を超えた古いデータを削除"""
    from datetime import timedelta
    cutoff = (date.today() - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        resp = sb.table("price_history") \
            .delete() \
            .lt("recorded_at", cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古いデータを削除: {deleted}件（{cutoff}より前）")
    except Exception as e:
        print(f"古いデータ削除失敗: {e}")


def main():
    print(f"=== 価格履歴収集 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    sb = get_supabase()
    cards = fetch_tracked_cards(sb)

    if not cards:
        print("監視対象カードが登録されていません。Supabaseの tracked_cards テーブルにカードを追加してください。")
        return

    today = date.today().isoformat()
    total_saved = 0
    success_count = 0
    fail_count = 0

    for i, card_name in enumerate(cards):
        print(f"[{i+1}/{len(cards)}] {card_name}")
        saved = collect_and_save(sb, card_name, today)
        if saved > 0:
            total_saved += saved
            success_count += 1
        else:
            fail_count += 1

        # 最後のカード以外は待機
        if i < len(cards) - 1:
            time.sleep(WAIT_BETWEEN_CARDS)

    # 古いデータの削除
    cleanup_old_data(sb)

    print(f"\n=== 完了 ===")
    print(f"成功: {success_count}件 / 失敗: {fail_count}件 / 保存行数: {total_saved}")


if __name__ == "__main__":
    main()
