"""
tests/test_aggregations.py — aggregations.daily_min_by_lowest_rarity の代表レアリティ選定テスト
（フェーズ3 P3、docs/design-phase3-generation-side.md 「P3: rarity 空文字バケット」）
＋ common_shop_price_change（監査F5: /api/wish-prices の共通店舗比較）のテスト

背景:
  rarity抽出失敗行は price_persist.build_min_price_rows で "(不明)" ラベルへ隔離される
  （P3-A）。読み出し側（代表レアリティ選定）でもこのラベルを候補から除外し、正体不明の
  疑似系列がグラフ・通知の基準系列として選ばれないようにする。他に候補が無い場合のみ
  フォールバックとして採用する。

common_shop_price_change:
  /api/wish-prices の±%表示は従来「8日窓の最古日vs最新日・全店舗min」で計算していたため、
  最古日にしかない店舗の安値が混入し約16%が共通店舗で再計算すると消える偽シグナルだった
  （監査F5）。notify.compute_drop と同じ「共通店舗が2店以上ある最古日」探索方式で
  diff/pct を計算し直す。ロジックはネットワーク不要の純関数。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aggregations import daily_min_by_lowest_rarity, common_shop_price_change
from rarity import UNKNOWN_RARITY_LABEL

OLD, NEW = "2026-07-01", "2026-07-08"


def row(name, rarity, date, price):
    return {"card_name": name, "rarity": rarity, "recorded_at": date, "min_price": price}


def shop_row(name, rarity, shop, date, price):
    return {"card_name": name, "rarity": rarity, "shop": shop,
            "recorded_at": date, "min_price": price}


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


# ──────────────────────────────────────────────
# common_shop_price_change（監査F5）
# ──────────────────────────────────────────────

class TestCommonShopPriceChangeBasic:
    def test_two_common_shops_diff_and_pct_computed(self):
        # 店舗A・Bとも両日に記録がある → 共通店舗のminどうしで比較
        rows = [
            shop_row("カード", "ノーマル", "店舗A", OLD, 1000),
            shop_row("カード", "ノーマル", "店舗A", NEW, 900),
            shop_row("カード", "ノーマル", "店舗B", OLD, 1100),
            shop_row("カード", "ノーマル", "店舗B", NEW, 950),
        ]
        result = common_shop_price_change(rows)["カード"]
        assert result["base_7d"] == 1000  # 共通店舗内minは店舗Aの1000
        assert result["diff"] == -100     # 900 - 1000
        assert result["pct"] == -10.0

    def test_orphan_shop_at_old_date_excluded_from_base(self):
        # 監査F5の再現ケース: 最古日にしか記録が無い店舗Bの安値(500)は、
        # 従来ロジック(全店舗min)なら base_7d に混入していた。共通店舗比較では
        # 店舗Bが除外され、両日にある店舗Aだけで計算される
        rows = [
            shop_row("カード", "ノーマル", "店舗A", OLD, 1000),
            shop_row("カード", "ノーマル", "店舗A", NEW, 1000),  # 横ばい
            shop_row("カード", "ノーマル", "店舗B", OLD, 500),   # 最古日のみ記録 → 共通店舗ではない
        ]
        result = common_shop_price_change(rows)["カード"]
        # 店舗Bが1店のみ（共通店舗2店未満）なので date_old が見つからず None
        assert result == {"base_7d": None, "diff": None, "pct": None}


class TestCommonShopPriceChangeSearchesOlderDate:
    def test_finds_older_date_with_two_common_shops(self):
        # 最古日(D1)は店舗Aのみ・2日目(D2)は店舗A/Bともにある → D2をdate_oldとして採用
        d1, d2, d3 = "2026-07-01", "2026-07-03", "2026-07-08"
        rows = [
            shop_row("カード", "ノーマル", "店舗A", d1, 1200),  # 店舗Bなし→共通店舗1店のみ
            shop_row("カード", "ノーマル", "店舗A", d2, 1000),
            shop_row("カード", "ノーマル", "店舗B", d2, 1050),
            shop_row("カード", "ノーマル", "店舗A", d3, 900),
            shop_row("カード", "ノーマル", "店舗B", d3, 920),
        ]
        result = common_shop_price_change(rows)["カード"]
        assert result["base_7d"] == 1000  # d2の共通店舗min（d1のorphanな1200は使わない）
        assert result["diff"] == -100     # 900 - 1000
        assert result["pct"] == -10.0


class TestCommonShopPriceChangeNoData:
    def test_single_date_only_returns_none(self):
        rows = [shop_row("カード", "ノーマル", "店舗A", NEW, 1000)]
        result = common_shop_price_change(rows)["カード"]
        assert result == {"base_7d": None, "diff": None, "pct": None}

    def test_empty_rows_returns_empty_dict(self):
        assert common_shop_price_change([]) == {}

    def test_multiple_cards_grouped_independently(self):
        rows = [
            shop_row("カードX", "ノーマル", "店舗A", OLD, 1000),
            shop_row("カードX", "ノーマル", "店舗A", NEW, 900),
            shop_row("カードX", "ノーマル", "店舗B", OLD, 1000),
            shop_row("カードX", "ノーマル", "店舗B", NEW, 900),
            shop_row("カードY", "ノーマル", "店舗A", NEW, 500),  # 1日分のみ
        ]
        result = common_shop_price_change(rows)
        assert result["カードX"]["diff"] == -100
        assert result["カードY"] == {"base_7d": None, "diff": None, "pct": None}
