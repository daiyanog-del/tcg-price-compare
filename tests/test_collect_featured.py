"""
tests/test_collect_featured.py — collect_featured.py の純関数テスト

対象:
  - _is_shop_failed: (card, shop) 1回の取得試行の失敗判定（4分岐）
  - classify_blocklike_shops: 店舗ごとの失敗率によるブロック様判定
  - _group_pairs_by_card: (card, shop) ペアのカード単位グルーピング

ネットワーク・実Supabaseは一切使わない。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from collect_featured import _is_shop_failed, classify_blocklike_shops, _group_pairs_by_card


# ──────────────────────────────────────────────
# _is_shop_failed（4分岐）
# ──────────────────────────────────────────────

class TestIsShopFailed:
    def test_status_none_is_failed(self):
        # compare_prices が結果を書き込まなかった（想定外）
        assert _is_shop_failed(None) is True

    def test_exception_present_is_failed(self):
        assert _is_shop_failed({"count": 0, "fetch_errors": 0, "exception": "boom"}) is True

    def test_fetch_errors_with_zero_count_is_failed(self):
        assert _is_shop_failed({"count": 0, "fetch_errors": 2}) is True

    def test_count_positive_with_fetch_errors_is_not_failed(self):
        # 一部エラーがあっても結果が得られていれば「取得できた」扱い
        assert _is_shop_failed({"count": 3, "fetch_errors": 1}) is False

    def test_zero_errors_zero_count_is_not_failed(self):
        # 0件かつエラーなし＝在庫なし。取得失敗ではない
        assert _is_shop_failed({"count": 0, "fetch_errors": 0}) is False


# ──────────────────────────────────────────────
# classify_blocklike_shops
# ──────────────────────────────────────────────

class TestClassifyBlocklikeShops:
    def test_shop_at_threshold_is_blocklike(self):
        failed = [("カードA", "カードラボ"), ("カードB", "カードラボ"), ("カードC", "カードラボ")]
        attempted = {"カードラボ": 10}  # 3/10=30% ちょうど閾値
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == {"カードラボ"}

    def test_shop_under_threshold_is_not_blocklike(self):
        failed = [("カードA", "遊々亭")]
        attempted = {"遊々亭": 10}  # 1/10=10%
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == set()

    def test_mixed_shops_classified_independently(self):
        failed = [
            ("カードA", "カードラボ"), ("カードB", "カードラボ"), ("カードC", "カードラボ"),
            ("カードA", "遊々亭"),
        ]
        attempted = {"カードラボ": 10, "遊々亭": 10}
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == {"カードラボ"}

    def test_zero_attempted_shop_is_not_blocklike(self):
        # attempted=0（試行数不明）はゼロ除算を避け、ブロック様と判定しない
        failed = [("カードA", "遊々亭")]
        result = classify_blocklike_shops(failed, {}, threshold=0.30)
        assert result == set()


# ──────────────────────────────────────────────
# _group_pairs_by_card
# ──────────────────────────────────────────────

class TestGroupPairsByCard:
    def test_groups_shops_under_card_preserving_order(self):
        pairs = [("カードA", "遊々亭"), ("カードA", "カードラボ"), ("カードB", "遊々亭")]
        result = _group_pairs_by_card(pairs)
        assert result == {"カードA": ["遊々亭", "カードラボ"], "カードB": ["遊々亭"]}
