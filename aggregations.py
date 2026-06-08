"""
aggregations.py — 価格履歴集計の共通関数

app.py / notify.py の両方から利用する。
重複実装を1箇所に集約する。
"""

from __future__ import annotations
from collections import defaultdict


def daily_min_by_lowest_rarity(rows: list) -> dict:
    """price_history 行リストから、最安レアリティ系列を代表として
    {card_name: {date_str: min_price}} を返す。

    異なるレアリティ間の価格を比較しないよう、以下のアルゴリズムで代表レアリティを選ぶ:
      1. (card_name, rarity, date) → min_price を集計
      2. 各 card_name の「最新日の価格が最安のレアリティ」を代表に選択
         ※ 最新日に欠損のあるレアリティは候補除外
         ※ 全レアリティで最新日欠損の場合は期間最小値が最安のレアリティにフォールバック
      3. 代表レアリティの系列のみ返す

    10円以下の異常値は除外する。
    """
    # ステップ1: (name, rarity, date) → min_price
    rarity_dates: dict = defaultdict(lambda: defaultdict(dict))
    for r in rows:
        name   = r.get("card_name", "")
        rarity = r.get("rarity", "") or ""
        date   = r.get("recorded_at", "")[:10]
        price  = r.get("min_price", 0)
        if not name or not date or price <= 10:
            continue
        if date not in rarity_dates[name][rarity] or price < rarity_dates[name][rarity][date]:
            rarity_dates[name][rarity][date] = price

    # ステップ2 & 3: 代表レアリティを選んで card_name → {date: price} を返す
    result: dict = {}
    for name, by_rarity in rarity_dates.items():
        if not by_rarity:
            continue
        all_dates = sorted({d for dates in by_rarity.values() for d in dates})
        if not all_dates:
            continue
        latest_date = all_dates[-1]

        best_rarity = None
        best_price  = None
        # 最新日に存在するレアリティから最安を選ぶ
        for rarity, dates in by_rarity.items():
            if latest_date not in dates:
                continue
            p = dates[latest_date]
            if best_price is None or p < best_price:
                best_price  = p
                best_rarity = rarity

        # フォールバック: 全レアリティで最新日欠損 → 期間最小値が最安のレアリティ
        if best_rarity is None:
            for rarity, dates in by_rarity.items():
                min_p = min(dates.values())
                if best_price is None or min_p < best_price:
                    best_price  = min_p
                    best_rarity = rarity

        if best_rarity is not None:
            result[name] = dict(by_rarity[best_rarity])

    return result
