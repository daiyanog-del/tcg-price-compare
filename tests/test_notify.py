"""
tests/test_notify.py — notify.py の値下がり判定ガード（compute_drop）の単体テスト

背景:
  値動き検出の偽陽性（実測98%）対策として、ガード①（共通店舗）・ガード②（複数店確認）
  を get_price_drops（Web Push値下がり通知）にも適用した。ネットワーク・Supabaseクライアント
  不要な純関数 compute_drop / _representative_rarity を対象にテストする。

テスト方針:
  - rows: 1カード分の price_history 行（card_name, rarity, shop, min_price, recorded_at）を
    手組みして渡すだけ。
  - ガード①: 片方の日にしか記録がない店舗は比較から除外されることを確認
  - ガード②: 同方向に動いた共通店舗が2店未満なら不採用になることを確認
  - 閾値（-5%かつ-50円）の境界判定
  - 代表レアリティ選出（最新日に最安のレアリティ、フォールバック）
  - _representative_rarity と aggregations.daily_min_by_lowest_rarity の等価性ピン留め
    （代表レアリティ選定ロジックを notify.py 内に複製した際、元のロジックと乖離していないことの保証）
  - date_old 選定（最新日との共通店舗が2店以上になる最古の日を探す。窓の初日が疎な
    ケースで従来の「絶対最古日固定」より偽陰性が減ることの確認）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from notify import compute_drop, _representative_rarity, DROP_PCT, DROP_ABS
from aggregations import daily_min_by_lowest_rarity, common_shop_price_change
from rarity import UNKNOWN_RARITY_LABEL

OLD, NEW = "2026-07-01", "2026-07-08"


def row(rarity, shop, date, price):
    return {"card_name": "テストカード", "rarity": rarity, "shop": shop,
            "min_price": price, "recorded_at": date}


# ──────────────────────────────────────────────
# ガード①（共通店舗）
# ──────────────────────────────────────────────

class TestGuard1CommonShops:
    def test_shop_missing_on_one_day_excluded_from_comparison(self):
        # 店舗Aは両日、店舗Bは新しい日にしか記録がない（片方のみ→ガード①で除外）
        # 店舗Aのみで比較: 1000→940 (-6%, -60円) は閾値超えで採用されるはず
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 940),
            row("ノーマル", "店舗B", NEW, 500),  # 旧日データなし→ガード①で除外
        ]
        # ガード②を満たすには2店以上必要なので、これは down_shops=1 で不採用になる
        result = compute_drop(rows)
        assert result is None

    def test_common_shop_price_used_not_orphan_shop(self):
        # 店舗Bのみ旧日にしかない極端な安値(50円)が、共通店舗ベースの計算に
        # 混入しないことを確認（ガード①がなければ base_price=50 になってしまう）
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 940),
            row("ノーマル", "店舗C", OLD, 1000),
            row("ノーマル", "店舗C", NEW, 940),
            row("ノーマル", "店舗B", OLD, 50),  # 新日データなし→ガード①で除外
        ]
        result = compute_drop(rows)
        assert result is not None
        assert result["base_7d"] == 1000  # 50円が混入していないこと
        assert result["latest"] == 940


# ──────────────────────────────────────────────
# ガード②（複数店確認）
# ──────────────────────────────────────────────

class TestGuard2MultiShopConfirmation:
    def test_single_shop_drop_not_notified(self):
        # 共通店舗が1店のみ値下がり方向 → ガード②未達で不採用
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 900),  # -10%, -100円（閾値は満たす）
        ]
        assert compute_drop(rows) is None

    def test_two_shops_same_direction_notified(self):
        # 共通店舗2店が両方値下がり方向 → ガード②通過
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 900),
            row("ノーマル", "店舗B", OLD, 1100),
            row("ノーマル", "店舗B", NEW, 950),
        ]
        result = compute_drop(rows)
        assert result is not None
        assert result["base_7d"] == 1000
        assert result["latest"] == 900

    def test_one_up_one_down_not_notified(self):
        # 2店あるが逆方向に動いている場合、値下がり方向の店舗は1店のみ → 不採用
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 900),   # 値下がり
            row("ノーマル", "店舗B", OLD, 1000),
            row("ノーマル", "店舗B", NEW, 1200),  # 値上がり（最安値には影響しない）
        ]
        result = compute_drop(rows)
        assert result is None


# ──────────────────────────────────────────────
# 閾値境界（-5%かつ-50円）
# ──────────────────────────────────────────────

class TestThreshold:
    def test_below_threshold_not_notified(self):
        # -3%, -30円（閾値未達）
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 970),
            row("ノーマル", "店舗B", OLD, 1000),
            row("ノーマル", "店舗B", NEW, 970),
        ]
        assert compute_drop(rows) is None

    def test_pct_met_but_abs_not_met_excluded(self):
        # -6%だが-30円（DROP_ABS=-50円未達）→ 両方満たさないと不採用
        rows = [
            row("ノーマル", "店舗A", OLD, 500),
            row("ノーマル", "店舗A", NEW, 470),  # -6%, -30円
            row("ノーマル", "店舗B", OLD, 500),
            row("ノーマル", "店舗B", NEW, 470),
        ]
        assert compute_drop(rows) is None

    def test_both_met_notified(self):
        rows = [
            row("ノーマル", "店舗A", OLD, 2000),
            row("ノーマル", "店舗A", NEW, 1800),  # -10%, -200円
            row("ノーマル", "店舗B", OLD, 2000),
            row("ノーマル", "店舗B", NEW, 1800),
        ]
        result = compute_drop(rows)
        assert result is not None
        assert result["diff"] == -200
        assert result["pct"] == -10.0


# ──────────────────────────────────────────────
# 該当なしのケース
# ──────────────────────────────────────────────

class TestNoData:
    def test_empty_rows(self):
        assert compute_drop([]) is None

    def test_single_date_only(self):
        rows = [row("ノーマル", "店舗A", OLD, 1000)]
        assert compute_drop(rows) is None

    def test_representative_rarity_none_for_empty(self):
        assert _representative_rarity([]) is None


# ──────────────────────────────────────────────
# _representative_rarity と aggregations.daily_min_by_lowest_rarity の等価性ピン留め
# ──────────────────────────────────────────────

class TestRepresentativeRarityMatchesAggregations:
    """_representative_rarity は aggregations.daily_min_by_lowest_rarity の
    代表レアリティ選定ロジック（店舗粒度再計算のためnotify.py内に複製したもの）と
    同一結果を返すべき。同一rowsを両方に渡し、選ばれたレアリティの価格系列が一致するか確認する。
    """

    def _representative_series(self, rows, rarity):
        """rows のうち rarity のものだけで (date -> min_price) を素朴に集計する"""
        series = {}
        for r in rows:
            if (r.get("rarity") or "") != rarity:
                continue
            d = r.get("recorded_at", "")[:10]
            p = r.get("min_price", 0)
            if p <= 10:
                continue
            if d not in series or p < series[d]:
                series[d] = p
        return series

    def test_simple_case_matches(self):
        rows = [
            {"card_name": "テストカード", "rarity": "ノーマル", "recorded_at": OLD, "min_price": 1000},
            {"card_name": "テストカード", "rarity": "ノーマル", "recorded_at": NEW, "min_price": 900},
            {"card_name": "テストカード", "rarity": "シク",     "recorded_at": OLD, "min_price": 3000},
            {"card_name": "テストカード", "rarity": "シク",     "recorded_at": NEW, "min_price": 2500},
        ]
        rarity = _representative_rarity(rows)
        expected = daily_min_by_lowest_rarity(rows)["テストカード"]
        actual = self._representative_series(rows, rarity)
        assert actual == expected

    def test_fallback_case_matches(self):
        # 「シク」は最新日(NEW)にデータがなく、「ノーマル」だけが最新日に存在する
        # → 最新日に存在するレアリティ（ノーマル）が優先的に選ばれる
        rows = [
            {"card_name": "テストカード", "rarity": "ノーマル", "recorded_at": OLD, "min_price": 1000},
            {"card_name": "テストカード", "rarity": "ノーマル", "recorded_at": NEW, "min_price": 900},
            {"card_name": "テストカード", "rarity": "シク",     "recorded_at": OLD, "min_price": 500},
        ]
        rarity = _representative_rarity(rows)
        assert rarity == "ノーマル"
        expected = daily_min_by_lowest_rarity(rows)["テストカード"]
        actual = self._representative_series(rows, rarity)
        assert actual == expected

    def test_full_fallback_when_all_rarities_missing_latest_date(self):
        # 全レアリティが最新日(all_datesの最後)にデータを持たないケース
        # （両レアリティともOLDのみ）→ 期間最小値が最安のレアリティにフォールバック
        rows = [
            {"card_name": "テストカード", "rarity": "ノーマル", "recorded_at": OLD, "min_price": 1000},
            {"card_name": "テストカード", "rarity": "シク",     "recorded_at": OLD, "min_price": 500},
        ]
        rarity = _representative_rarity(rows)
        expected = daily_min_by_lowest_rarity(rows)["テストカード"]
        actual = self._representative_series(rows, rarity)
        assert actual == expected


# ──────────────────────────────────────────────
# "(不明)" レアリティの除外（フェーズ3 P3）
# ──────────────────────────────────────────────

class TestUnknownRarityExcludedFromRepresentative:
    def test_unknown_excluded_when_alternative_exists(self):
        rows = [
            row(UNKNOWN_RARITY_LABEL, "店舗A", NEW, 100),
            row("シク", "店舗A", NEW, 3000),
            row("シク", "店舗A", OLD, 3200),
        ]
        assert _representative_rarity(rows) == "シク"


# ──────────────────────────────────────────────
# aggregations.common_shop_price_change と compute_drop の等価性ピン留め
# （監査 2026-08-07, 中6/L11）
# ──────────────────────────────────────────────

class TestCommonShopPriceChangeMatchesComputeDrop:
    """aggregations.common_shop_price_change は notify.compute_drop の
    「共通店舗2店以上ある最古日を探索し、共通店舗内minどうしで比較する」という
    core計算部分の複製である（店舗粒度の再計算にレアリティ名自体が必要なため。
    docstring参照）。ただし通知専用のガード②（値下がり方向2店確認）・閾値判定は
    適用しない（裁定 中7, 2026-08-07: ガード②はWeb Push誤送信対策専用）。

    ガード②・閾値の両方を満たすrowsを渡した場合、両者の base_7d/diff/pct が
    一致することをピン留めする。
    """

    def test_base_diff_pct_match_when_compute_drop_passes(self):
        rows = [
            row("ノーマル", "店舗A", OLD, 2000),
            row("ノーマル", "店舗A", NEW, 1800),  # -10%, -200円
            row("ノーマル", "店舗B", OLD, 2000),
            row("ノーマル", "店舗B", NEW, 1800),
        ]
        drop = compute_drop(rows)
        assert drop is not None  # ガード①②・閾値とも通過する組み合わせ
        common = common_shop_price_change(rows)["テストカード"]
        assert common["base_7d"] == drop["base_7d"]
        assert common["diff"] == drop["diff"]
        assert common["pct"] == drop["pct"]

    def test_returns_value_even_when_guard2_blocks_notify(self):
        # 2店とも共通店舗だが逆方向（店舗Aは値下がり、店舗Bは値上がり）→
        # compute_drop はガード②（値下がり方向2店未満）で不採用=None。
        # common_shop_price_change はガード②を適用しないため、同じ共通店舗min計算で
        # base_7d/diff/pct を返す（中7で意図した仕様どおりの差）
        rows = [
            row("ノーマル", "店舗A", OLD, 1000),
            row("ノーマル", "店舗A", NEW, 900),   # 値下がり
            row("ノーマル", "店舗B", OLD, 1000),
            row("ノーマル", "店舗B", NEW, 1200),  # 値上がり（最安値には影響しない）
        ]
        assert compute_drop(rows) is None
        common = common_shop_price_change(rows)["テストカード"]
        assert common["base_7d"] == 1000  # min(店舗A:1000, 店舗B:1000)
        assert common["diff"] == -100     # min(900,1200) - 1000
        assert common["pct"] == -10.0

    def test_unknown_used_as_last_resort(self):
        rows = [
            row(UNKNOWN_RARITY_LABEL, "店舗A", OLD, 500),
            row(UNKNOWN_RARITY_LABEL, "店舗A", NEW, 480),
        ]
        assert _representative_rarity(rows) == UNKNOWN_RARITY_LABEL


# ──────────────────────────────────────────────
# date_old 選定（最新日との共通店舗が2店以上になる最古の日）
# ──────────────────────────────────────────────

class TestDateOldSelection:
    def test_sparse_earliest_day_is_skipped(self):
        # D1(疎: 店舗Aのみ) → D2(店舗A,B) → D3=最新日(店舗A,B)
        # 絶対最古日D1を使うと共通店舗が店舗Aのみ(1店)でガード②未達になってしまうが、
        # D2を使えば店舗A,Bの2店が確保できるため date_old には D2 が選ばれるはず
        D1, D2, D3 = "2026-07-01", "2026-07-05", "2026-07-08"
        rows = [
            row("ノーマル", "店舗A", D1, 1200),
            row("ノーマル", "店舗A", D2, 1000),
            row("ノーマル", "店舗B", D2, 1100),
            row("ノーマル", "店舗A", D3, 900),
            row("ノーマル", "店舗B", D3, 950),
        ]
        result = compute_drop(rows)
        assert result is not None
        assert result["base_7d"] == 1000  # D2の共通店舗min(A=1000, B=1100)
        assert result["latest"] == 900    # D3の共通店舗min(A=900, B=950)
        assert result["diff"] == -100
        assert result["pct"] == -10.0

    def test_no_day_with_two_common_shops_returns_none(self):
        # どの日を選んでも最新日との共通店舗が1店以下 → date_old が見つからずNone
        D1, D2 = "2026-07-01", "2026-07-08"
        rows = [
            row("ノーマル", "店舗A", D1, 1000),
            row("ノーマル", "店舗B", D2, 900),
        ]
        assert compute_drop(rows) is None
