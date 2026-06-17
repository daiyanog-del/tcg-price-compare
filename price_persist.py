"""価格履歴 (price_history) への書き込みを集約するモジュール。

collect_prices.py（定期収集）と app.py（検索・見積もりの即時反映）の両方から
呼ばれる共通ヘルパ。書き込み経路を 1 か所に集約することで、スキーマ変更や
冪等性ロジックの修正を 1 か所で完結させる。

前提:
- price_history テーブルには UNIQUE(card_name, shop, rarity, recorded_at) 制約あり
- ON CONFLICT DO UPDATE による upsert で last-write-wins
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def build_min_price_rows(card_name: str, scrape_results: list, today: str) -> list[dict]:
    """スクレイピング結果から price_history への書き込み行を組み立てる。

    (shop, rarity) ごとに最安値 (min_price) を取り、price_history のスキーマ
    {card_name, shop, rarity, min_price, recorded_at} に変換する。
    sold_out や price <= 10 はノイズとして除外する。
    （ロジックは collect_prices.py:156-175 から切り出し。内容変更なし）
    """
    min_prices: dict[tuple[str, str], int] = {}
    for item in scrape_results:
        if item.get("sold_out"):
            continue
        price = item.get("price", 0)
        if price <= 10:
            continue
        shop = item.get("shop", "")
        if not shop:
            continue
        rarity = item.get("rarity", "") or ""
        key = (shop, rarity)
        if key not in min_prices or price < min_prices[key]:
            min_prices[key] = price

    return [
        {"card_name": card_name, "shop": shop, "rarity": rarity,
         "min_price": price, "recorded_at": today}
        for (shop, rarity), price in min_prices.items()
    ]


def upsert_price_rows(sb, rows: list[dict]) -> int:
    """price_history に upsert する。

    UNIQUE(card_name, shop, rarity, recorded_at) 制約に基づき、既存があれば
    全列上書き（直近の scraper の結果を最新として採用 = last-write-wins）。
    成功した行数を返す。エラー時は 0 を返し、警告ログのみ出す（呼び元の処理は止めない）。
    """
    if not rows:
        return 0
    try:
        sb.table("price_history").upsert(
            rows,
            on_conflict="card_name,shop,rarity,recorded_at"
        ).execute()
        return len(rows)
    except Exception as e:
        logger.warning(f"[price_persist] upsert 失敗 ({len(rows)}行): {e}")
        return 0
