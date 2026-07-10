"""
tests/test_price_persist.py — price_persist.build_min_price_rows / upsert_price_rows の単体テスト
（フェーズ3 P2/P3/P4/P6b、docs/design-phase3-generation-side.md 該当節）

対象:
  - P3: rarity 抽出失敗行（""）が "(不明)" ラベルへ隔離されること
  - P4: (shop, rarity) ごとに min_price（通常品）/ min_price_any（本当の最安）を
        並行集計すること。「通常品」の定義（カーナベルSA=通常・B/C/D=除外、
        遊々亭セール=通常、トレコロ中古キズあり=除外、カードラッシュ〔状態…〕=除外）
  - P2: min_price を決めた出品の code が行に添えられること
  - P6b: upsert_price_rows が ignore_duplicates を on_conflict と共に upsert() へ渡すこと

ネットワーク・実Supabaseは一切使わない。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from price_persist import build_min_price_rows, upsert_price_rows, _is_normal_condition
from rarity import UNKNOWN_RARITY_LABEL

TODAY = "2026-07-10"


def _item(shop, rarity, price, code="", condition="-", sold_out=False):
    return {
        "shop": shop, "name": "テストカード", "rarity": rarity, "code": code,
        "condition": condition, "price": price, "stock": 1,
        "sold_out": sold_out, "url": "", "image": "",
    }


def _row_for(rows, shop, rarity):
    for r in rows:
        if r["shop"] == shop and r["rarity"] == rarity:
            return r
    raise AssertionError(f"row not found: {shop}/{rarity}")


# ──────────────────────────────────────────────
# P3: rarity="" の隔離
# ──────────────────────────────────────────────

class TestRarityIsolation:
    def test_empty_rarity_becomes_unknown_label(self):
        rows = build_min_price_rows("テストカード", [_item("店舗A", "", 1000)], TODAY)
        assert rows[0]["rarity"] == UNKNOWN_RARITY_LABEL

    def test_known_rarity_untouched(self):
        rows = build_min_price_rows("テストカード", [_item("店舗A", "シク", 1000)], TODAY)
        assert rows[0]["rarity"] == "シク"

    def test_unknown_and_known_rarity_are_separate_keys(self):
        rows = build_min_price_rows(
            "テストカード",
            [_item("店舗A", "", 500), _item("店舗A", "シク", 3000)],
            TODAY,
        )
        assert len(rows) == 2
        rarities = {r["rarity"] for r in rows}
        assert rarities == {UNKNOWN_RARITY_LABEL, "シク"}


# ──────────────────────────────────────────────
# P4: 「通常品」判定テーブル
# ──────────────────────────────────────────────

class TestIsNormalCondition:
    def test_dash_is_always_normal(self):
        assert _is_normal_condition("カードラボ", "-") is True
        assert _is_normal_condition("まんぞく屋", "") is True

    def test_kanabell_sa_is_normal_bcd_are_not(self):
        assert _is_normal_condition("カーナベル", "状態:SA") is True
        assert _is_normal_condition("カーナベル", "状態:B") is False
        assert _is_normal_condition("カーナベル", "状態:C") is False
        assert _is_normal_condition("カーナベル", "状態:D") is False

    def test_yuyu_sale_is_normal(self):
        assert _is_normal_condition("遊々亭", "セール") is True

    def test_torecolo_kizu_is_not_normal(self):
        assert _is_normal_condition("トレコロCB", "中古キズあり") is False

    def test_cardrush_condition_marker_is_not_normal(self):
        # カードラッシュの実表記（2026-07-11 実HTML検証: 増殖するG 94件中
        # 無印"-"=36 / 状態A-=29 / 状態B=27 / 状態C=2。美品は無印で出る）
        assert _is_normal_condition("カードラッシュ", "状態A-") is False
        assert _is_normal_condition("カードラッシュ", "状態B") is False
        assert _is_normal_condition("カードラッシュ", "状態C") is False


# ──────────────────────────────────────────────
# P4: build_min_price_rows の2値計算
# ──────────────────────────────────────────────

class TestTwoValueAggregation:
    def test_kanabell_sa_and_c_split_normal_from_any(self):
        # SA=1000円(通常)・C=700円(通常外) → min_price=1000(通常のみ), min_price_any=700(全体)
        rows = build_min_price_rows(
            "テストカード",
            [
                _item("カーナベル", "ウルトラ", 1000, code="C1", condition="状態:SA"),
                _item("カーナベル", "ウルトラ", 700, code="C2", condition="状態:C"),
            ],
            TODAY,
        )
        row = _row_for(rows, "カーナベル", "ウルトラ")
        assert row["min_price"] == 1000
        assert row["min_price_any"] == 700
        assert row["code"] == "C1"  # min_price を決めた出品のcode

    def test_torecolo_kizu_only_falls_back_to_any(self):
        # 通常品が1件もない（キズありのみ）→ min_price は min_price_any にフォールバック
        rows = build_min_price_rows(
            "テストカード",
            [_item("トレコロCB", "レア", 500, code="K1", condition="中古キズあり")],
            TODAY,
        )
        row = _row_for(rows, "トレコロCB", "レア")
        assert row["min_price"] == 500
        assert row["min_price_any"] == 500
        assert row["code"] == "K1"

    def test_normal_and_any_equal_when_only_normal_items(self):
        rows = build_min_price_rows(
            "テストカード",
            [_item("カードラボ", "レア", 800, code="X1")],
            TODAY,
        )
        row = _row_for(rows, "カードラボ", "レア")
        assert row["min_price"] == 800
        assert row["min_price_any"] == 800
        assert row["code"] == "X1"

    def test_yuyu_sale_counts_as_normal_and_can_win(self):
        # セール980円(通常扱い) vs 通常1200円 → 通常品側の最安はセール価格
        rows = build_min_price_rows(
            "テストカード",
            [
                _item("遊々亭", "ノーマル", 1200, code="N1", condition="-"),
                _item("遊々亭", "ノーマル", 980, code="S1", condition="セール"),
            ],
            TODAY,
        )
        row = _row_for(rows, "遊々亭", "ノーマル")
        assert row["min_price"] == 980
        assert row["min_price_any"] == 980
        assert row["code"] == "S1"

    def test_sold_out_and_low_price_still_excluded(self):
        # 既存挙動（sold_out除外・10円以下除外）が2値集計後も保たれること
        rows = build_min_price_rows(
            "テストカード",
            [
                _item("店舗A", "レア", 10, condition="-"),
                _item("店舗A", "レア", 5000, condition="-", sold_out=True),
                _item("店舗A", "レア", 1500, condition="-"),
            ],
            TODAY,
        )
        row = _row_for(rows, "店舗A", "レア")
        assert row["min_price"] == 1500
        assert row["min_price_any"] == 1500


# ──────────────────────────────────────────────
# P6b: upsert_price_rows の ignore_duplicates 伝播
# ──────────────────────────────────────────────

class _FakeTable:
    def __init__(self):
        self.calls = []

    def upsert(self, rows, on_conflict=None, ignore_duplicates=None):
        self.calls.append({"rows": rows, "on_conflict": on_conflict, "ignore_duplicates": ignore_duplicates})
        return self

    def execute(self):
        return None


class _FakeSupabase:
    def __init__(self):
        self.table_obj = _FakeTable()

    def table(self, name):
        assert name == "price_history"
        return self.table_obj


class TestUpsertIgnoreDuplicates:
    def test_default_is_last_write_wins(self):
        sb = _FakeSupabase()
        rows = [{"card_name": "テストカード", "shop": "店舗A", "rarity": "レア",
                  "min_price": 1000, "min_price_any": 1000, "code": "", "recorded_at": TODAY}]
        saved = upsert_price_rows(sb, rows)
        assert saved == 1
        call = sb.table_obj.calls[0]
        assert call["ignore_duplicates"] is False
        assert call["on_conflict"] == "card_name,shop,rarity,recorded_at"

    def test_ignore_duplicates_true_is_forwarded(self):
        sb = _FakeSupabase()
        rows = [{"card_name": "テストカード", "shop": "店舗A", "rarity": "レア",
                  "min_price": 1000, "min_price_any": 1000, "code": "", "recorded_at": TODAY}]
        upsert_price_rows(sb, rows, ignore_duplicates=True)
        call = sb.table_obj.calls[0]
        assert call["ignore_duplicates"] is True

    def test_empty_rows_no_call(self):
        sb = _FakeSupabase()
        assert upsert_price_rows(sb, []) == 0
        assert sb.table_obj.calls == []
