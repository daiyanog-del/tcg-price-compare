"""
買取価格履歴データ収集スクリプト
==================================
GitHub Actionsで毎日実行し、注目カードの買取価格をSupabaseに記録する。
collect_prices.py（販売価格）とは別スケジュール・別ワークフローで動作する。

収集方式:
  注目カード（新弾・現メタ主要・規制・検索ヒット）のみを毎日収集。
  周辺カードは対象外（買取で値動きが意味を持つのは人気カードのため）。
  店舗はカードラッシュ・カーナベルのみ（遊々亭はCIでIPブロックのためスキップ）。
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

from supabase import create_client, Client
from scraper import compare_buyback, BUYBACK_SHOPS
from collect_prices import (
    get_supabase,
    normalize_card_name,
    fetch_tracked_cards_with_meta,
    check_shop_availability,
    sync_latest_packs,
    sync_meta_decks,
    sync_regulation,
    sync_searched_cards,
)

# ── 定数 ──
JST = timezone(timedelta(hours=9))
WAIT_BETWEEN_CARDS = 1.0  # 収集間隔（秒）。collect_prices.py に合わせる
RETENTION_DAYS = 90       # 買取履歴の保持期間（販売と同じ）

# CIでブロックされる買取店舗を除外（遊々亭はスクレイピングのためIPブロックリスクあり）
SKIP_BUYBACK_SHOPS_IN_CI = {"遊々亭"}


def collect_and_save_buyback(
    sb: Client, card_name: str, today: str, shop_names: list[str]
) -> int:
    """1枚のカードの買取価格を取得してSupabaseに保存。保存した行数を返す。

    販売の collect_and_save とほぼ同じ構造だが、
    店舗×レアリティごとの「最大買取額(max_price)」を記録する点が異なる。
    """
    try:
        results = compare_buyback(card_name, shop_names=shop_names)
    except Exception as e:
        print(f"  スクレイピング失敗 [{card_name}]: {e}")
        return 0

    if not results:
        print(f"  結果なし [{card_name}]")
        return 0

    # 店舗×レアリティごとに最大買取額を集計（販売は最小だが買取は最大が意味を持つ）
    max_prices: dict[tuple, int] = {}
    for item in results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price <= 10:  # 10円以下は異常値として除外
            continue
        key = (item.get("shop", ""), item.get("rarity", ""))
        if key not in max_prices or price > max_prices[key]:
            max_prices[key] = price

    if not max_prices:
        print(f"  有効な買取価格なし [{card_name}]")
        return 0

    rows = [
        {"card_name": card_name, "shop": shop, "rarity": rarity,
         "max_price": price, "recorded_at": today}
        for (shop, rarity), price in max_prices.items()
    ]

    try:
        sb.table("buyback_history").insert(rows).execute()
        print(f"  保存完了 [{card_name}]: {len(rows)}件")
        return len(rows)
    except Exception as e:
        print(f"  DB保存失敗 [{card_name}]: {e}")
        return 0


def cleanup_old_buyback_data(sb: Client):
    """保持期間を超えた古い買取履歴データを削除"""
    cutoff = (datetime.now(JST).date() - timedelta(days=RETENTION_DAYS)).isoformat()
    try:
        resp = sb.table("buyback_history") \
            .delete() \
            .lt("recorded_at", cutoff) \
            .execute()
        deleted = len(resp.data) if resp.data else 0
        if deleted > 0:
            print(f"古い買取データを削除: {deleted}件（{cutoff}より前）")
    except Exception as e:
        print(f"古い買取データ削除失敗: {e}")


def main():
    started_at = datetime.now(JST)
    print(f"=== 買取価格履歴収集 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    sb = get_supabase()

    # ── 注目カードの同期（hot集合のみ。周辺カードは収集しない）──
    # hot同期は外部HTTPを伴うため、import時ではなくここで呼ぶ
    hot: set[str] = set()
    print("\n--- 注目カード同期 ---")
    pack_all, _ = sync_latest_packs(sb)
    hot |= pack_all
    hot |= sync_meta_decks(sb)
    hot |= sync_regulation(sb)
    hot |= sync_searched_cards(sb, recent_days=7, min_count=2)
    print(f"注目カード候補: {len(hot)}件")

    # ── 収集対象: tracked_cards の中から hot に含まれるカードのみ ──
    all_cards = fetch_tracked_cards_with_meta(sb)
    if not all_cards:
        print("監視対象カードが登録されていません")
        return

    hot_cards = [c for c in all_cards if c["card_name"] in hot]
    print(f"収集対象（注目カードのみ）: {len(hot_cards)}件 / 全体: {len(all_cards)}件")

    if not hot_cards:
        print("注目カードが0件のため終了")
        return

    # ── 店舗ヘルスチェック（CIスキップ対象を除いた買取店舗のみ）──
    buyback_shop_names = [
        name for name, _ in BUYBACK_SHOPS
        if name not in SKIP_BUYBACK_SHOPS_IN_CI
    ]
    print(f"\n買取対象店舗: {buyback_shop_names}")
    print("\n--- 店舗ヘルスチェック ---")
    available_shops = check_shop_availability(buyback_shop_names)
    if not available_shops:
        print("全買取店舗が応答しないため収集を中止します")
        return
    print(f"使用店舗: {available_shops}")

    today = datetime.now(JST).date().isoformat()
    total_saved = 0
    success_count = 0
    fail_count = 0

    for i, card_data in enumerate(hot_cards):
        card_name = card_data["card_name"]
        print(f"[{i+1}/{len(hot_cards)}] {card_name}")
        saved = collect_and_save_buyback(sb, card_name, today, available_shops)
        if saved > 0:
            total_saved += saved
            success_count += 1
        else:
            fail_count += 1

        if i < len(hot_cards) - 1:
            time.sleep(WAIT_BETWEEN_CARDS)

    cleanup_old_buyback_data(sb)

    elapsed_min = (datetime.now(JST) - started_at).total_seconds() / 60
    print(f"\n=== 完了（{elapsed_min:.0f}分）===")
    print(f"成功: {success_count}件 / 失敗: {fail_count}件 / 保存行数: {total_saved}")


if __name__ == "__main__":
    main()
