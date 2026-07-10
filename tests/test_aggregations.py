"""
tests/test_aggregations.py — aggregations.daily_min_by_lowest_rarity の代表レアリティ選定テスト
（フェーズ3 P3、docs/design-phase3-generation-side.md 「P3: rarity 空文字バケット」）

背景:
  rarity抽出失敗行は price_persist.build_min_price_rows で "(不明)" ラベルへ隔離される
  （P3-A）。読み出し側（代表レアリティ選定）でもこのラベルを候補から除外し、正体不明の
  疑似系列がグラフ・通知の基準系列として選ばれないようにする。他に候補が無い場合のみ
  フォールバックとして採用する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aggregations import daily_min_by_lowest_rarity
from rarity import UNKNOWN_RARITY_LABEL

OLD, NEW = "2026-07-01", "2026-07-08"


def row(name, rarity, date, price):
    return {"card_name": name, "rarity": rarity, "recorded_at": date, "min_price": price}


class TestUnknownRarityExcludedWhenAlternativeExists:
    def test_unknown_cheaper_but_normal_rarity_present_at_latest_date(self):
        # "(不明)" のほうが最新日で安いが、除外され「シク」が代表に選ばれる
        rows = [
            row("カード", UNKNOWN_RARITY_LABEL, NEW, 100),
            row("カード", "シク", NEW, 3000),
            row("カード", "シク", OLD, 3200),
        ]
        result = daily_min_by_lowest_rarity(rows)
        assert result["カード"] == {OLD: 3200, NEW: 3000}

    def test_unknown_excluded_from_period_min_fallback_too(self):
        # 全レアリティが最新日欠損 → 期間最小値フォールバックでも "(不明)" は除外される
        rows = [
            row("カード", UNKNOWN_RARITY_LABEL, OLD, 100),
            row("カード", "シク", OLD, 3000),
        ]
        result = daily_min_by_lowest_rarity(rows)
        assert result["カード"] == {OLD: 3000}


class TestUnknownRarityUsedAsLastResort:
    def test_only_unknown_rarity_available(self):
        # 他に候補が無い場合のみ "(不明)" をフォールバック採用する
        rows = [
            row("カード", UNKNOWN_RARITY_LABEL, OLD, 500),
            row("カード", UNKNOWN_RARITY_LABEL, NEW, 480),
        ]
        result = daily_min_by_lowest_rarity(rows)
        assert result["カード"] == {OLD: 500, NEW: 480}


class TestUnaffectedWhenNoUnknownRarity:
    def test_lowest_rarity_still_picked(self):
        rows = [
            row("カード", "ノーマル", OLD, 1000),
            row("カード", "ノーマル", NEW, 900),
            row("カード", "シク", OLD, 3000),
            row("カード", "シク", NEW, 2500),
        ]
        result = daily_min_by_lowest_rarity(rows)
        assert result["カード"] == {OLD: 1000, NEW: 900}
