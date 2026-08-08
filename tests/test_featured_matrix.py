"""
tests/test_featured_matrix.py — featured_matrix の単体テスト

対象:
  - build_matrix（純関数）
    - (card, shop, rarity) ごとに最新 recorded_at の行が採用されること
    - セルの price は min_price_any 優先、NULL/欠落時は min_price にフォールバックすること
      （両方 NULL の行はセルとして採用しないこと）
    - rarity が読み出し境界で normalize_rarity により正規化されること（別名表記の畳み込み）
    - 正規化後 "(不明)"/空文字のレアリティ行がマトリクスから除外されること
    - 同一カード内のレアリティが最安セル価格の降順に並ぶこと（同価格は辞書順タイブレーク）
    - DBに観測が1行も無いカードも rows に含まれ、cells が空であること
    - セルに code（採用した行の型番）が透過されること
  - _fetch_price_rows（fakeクライアント、ページング・例外境界）
  - _chunk_card_names_by_bytes（バイト予算でのチャンク分割）

ネットワーク・実Supabaseは一切使わない。
"""

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from featured_matrix import build_matrix, _chunk_card_names_by_bytes, _fetch_price_rows
from rarity import UNKNOWN_RARITY_LABEL

TODAY = "2026-08-08"


def _row(card, shop, rarity, min_price, min_price_any, recorded_at, url=None, code=""):
    return {
        "card_name": card, "shop": shop, "rarity": rarity,
        "min_price": min_price, "min_price_any": min_price_any,
        "code": code, "url": url, "recorded_at": recorded_at,
    }


def _row_for(rows, name, rarity):
    for r in rows:
        if r["name"] == name and r["rarity"] == rarity:
            return r
    raise AssertionError(f"row not found: {name}/{rarity}")


class TestLatestDateWins:
    def test_latest_recorded_at_row_is_adopted(self):
        records = [
            _row("カードA", "カードラッシュ", "シク", 1000, 900, "2026-08-06", url="https://old"),
            _row("カードA", "カードラッシュ", "シク", 1200, 1100, "2026-08-08", url="https://new"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        # "シク" は normalize_rarity で正準名 "シークレット" に畳まれる
        row = _row_for(result["rows"], "カードA", "シークレット")
        cell = row["cells"]["カードラッシュ"]
        assert cell["price"] == 1100
        assert cell["url"] == "https://new"
        assert cell["recorded_at"] == "2026-08-08"


class TestMinPriceAnyFallback:
    def test_uses_min_price_any_when_present(self):
        records = [_row("カードA", "遊々亭", "レア", 1000, 800, "2026-08-08")]
        result = build_matrix(records, ["カードA"], TODAY)
        cell = _row_for(result["rows"], "カードA", "レア")["cells"]["遊々亭"]
        assert cell["price"] == 800

    def test_falls_back_to_min_price_when_any_is_none(self):
        records = [_row("カードA", "遊々亭", "レア", 1000, None, "2026-08-08")]
        result = build_matrix(records, ["カードA"], TODAY)
        cell = _row_for(result["rows"], "カードA", "レア")["cells"]["遊々亭"]
        assert cell["price"] == 1000

    def test_both_prices_none_row_is_skipped_as_cell(self):
        # min_price / min_price_any が両方NULLの行はセルとして採用しない
        # （card_order には残るので観測なしカードと同じ扱いになる）
        records = [_row("カードA", "遊々亭", "レア", None, None, "2026-08-08")]
        result = build_matrix(records, ["カードA"], TODAY)
        row = _row_for(result["rows"], "カードA", "")
        assert row["cells"] == {}


class TestRarityNormalization:
    def test_aliases_fold_into_same_canonical_row(self):
        # "シク" と "シークレット" は別名表記だが同一カノニカル名に畳まれ、
        # 同じ (card, shop, rarity) キーとして扱われる（最新日優先で1行に集約）
        records = [
            _row("カードA", "遊々亭", "シク", 1000, 1000, "2026-08-06"),
            _row("カードA", "遊々亭", "シークレット", 1200, 1200, "2026-08-08"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        card_rows = [r for r in result["rows"] if r["name"] == "カードA"]
        assert len(card_rows) == 1
        assert card_rows[0]["rarity"] == "シークレット"
        assert card_rows[0]["cells"]["遊々亭"]["price"] == 1200  # 最新日(08-08)側が採用される


class TestUnknownRarityExcluded:
    def test_unknown_label_rows_are_dropped(self):
        records = [_row("カードA", "遊々亭", UNKNOWN_RARITY_LABEL, 500, 500, "2026-08-08")]
        result = build_matrix(records, ["カードA"], TODAY)
        row = _row_for(result["rows"], "カードA", "")
        assert row["cells"] == {}

    def test_empty_rarity_rows_are_dropped(self):
        records = [_row("カードA", "遊々亭", "", 500, 500, "2026-08-08")]
        result = build_matrix(records, ["カードA"], TODAY)
        row = _row_for(result["rows"], "カードA", "")
        assert row["cells"] == {}

    def test_unknown_excluded_but_known_rarity_kept(self):
        records = [
            _row("カードA", "遊々亭", UNKNOWN_RARITY_LABEL, 999, 999, "2026-08-08"),
            _row("カードA", "遊々亭", "レア", 500, 500, "2026-08-08"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        card_rows = [r for r in result["rows"] if r["name"] == "カードA"]
        assert len(card_rows) == 1
        assert card_rows[0]["rarity"] == "レア"


class TestRarityOrdering:
    def test_rarities_sorted_by_cheapest_cell_descending(self):
        records = [
            _row("カードA", "遊々亭", "シークレット", 3000, 3000, "2026-08-08"),
            _row("カードA", "遊々亭", "ノーマル", 100, 100, "2026-08-08"),
            _row("カードA", "遊々亭", "スーパー", 1500, 1500, "2026-08-08"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        card_rows = [r for r in result["rows"] if r["name"] == "カードA"]
        rarities = [r["rarity"] for r in card_rows]
        assert rarities == ["シークレット", "スーパー", "ノーマル"]

    def test_same_price_rarities_tiebreak_by_dict_order(self):
        # 最安セル価格が同額のレアリティは、_min_cell_price降順ソートの安定性により
        # 元の sorted()（辞書順）の順序を保つ
        records = [
            _row("カードA", "遊々亭", "ウルトラ", 1000, 1000, "2026-08-08"),
            _row("カードA", "遊々亭", "スーパー", 1000, 1000, "2026-08-08"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        card_rows = [r for r in result["rows"] if r["name"] == "カードA"]
        rarities = [r["rarity"] for r in card_rows]
        assert rarities == sorted(["ウルトラ", "スーパー"])


class TestCardWithoutObservation:
    def test_card_with_no_records_gets_empty_cells_row(self):
        records = [_row("カードA", "遊々亭", "レア", 500, 500, "2026-08-08")]
        result = build_matrix(records, ["カードA", "カードB"], TODAY)
        row = _row_for(result["rows"], "カードB", "")
        assert row["cells"] == {}

    def test_row_order_follows_card_order(self):
        records = [
            _row("カードB", "遊々亭", "レア", 500, 500, "2026-08-08"),
            _row("カードA", "遊々亭", "レア", 300, 300, "2026-08-08"),
        ]
        result = build_matrix(records, ["カードA", "カードB"], TODAY)
        names_in_order = [r["name"] for r in result["rows"]]
        assert names_in_order == ["カードA", "カードB"]


class TestShopsOrdering:
    def test_shops_follow_default_shops_order_and_only_observed(self):
        from scraper import DEFAULT_SHOPS
        records = [
            _row("カードA", "まんぞく屋", "レア", 500, 500, "2026-08-08"),
            _row("カードA", "遊々亭", "レア", 400, 400, "2026-08-08"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        expected = [s for s in DEFAULT_SHOPS if s in ("まんぞく屋", "遊々亭")]
        assert result["shops"] == expected


class TestUpdatedAt:
    def test_updated_at_is_max_recorded_at(self):
        records = [
            _row("カードA", "遊々亭", "レア", 500, 500, "2026-08-05"),
            _row("カードA", "カードラッシュ", "レア", 400, 400, "2026-08-07"),
        ]
        result = build_matrix(records, ["カードA"], TODAY)
        assert result["updated_at"] == "2026-08-07"

    def test_updated_at_falls_back_to_today_when_no_records(self):
        result = build_matrix([], ["カードA"], TODAY)
        assert result["updated_at"] == TODAY


class TestCodePassthrough:
    def test_code_is_included_in_cell(self):
        records = [_row("カードA", "遊々亭", "レア", 500, 500, "2026-08-08", code="12345")]
        result = build_matrix(records, ["カードA"], TODAY)
        cell = _row_for(result["rows"], "カードA", "レア")["cells"]["遊々亭"]
        assert cell["code"] == "12345"


# ──────────────────────────────────────────────
# _chunk_card_names_by_bytes
# ──────────────────────────────────────────────

class TestChunkCardNamesByBytes:
    def test_short_list_stays_in_one_chunk(self):
        names = ["カードA", "カードB", "カードC"]
        chunks = _chunk_card_names_by_bytes(names, byte_budget=6000)
        assert chunks == [names]

    def test_splits_when_exceeding_byte_budget(self):
        names = [f"テストカード{'あ'*40}{i}" for i in range(50)]
        chunks = _chunk_card_names_by_bytes(names, byte_budget=6000)
        assert len(chunks) > 1
        # 元のカード名がすべて過不足なく含まれる（順序維持）
        flattened = [n for c in chunks for n in c]
        assert flattened == names

    def test_single_oversized_name_gets_its_own_chunk(self):
        huge_name = "カード" * 3000  # 単体で予算超過
        chunks = _chunk_card_names_by_bytes([huge_name, "カードB"], byte_budget=6000)
        assert chunks[0] == [huge_name]
        assert chunks[-1][-1] == "カードB"


# ──────────────────────────────────────────────
# _fetch_price_rows（fakeクライアント）
# ──────────────────────────────────────────────

class _FakeResp:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, all_rows, call_log):
        self._all_rows = all_rows
        self._call_log = call_log
        self._range = (0, len(all_rows))

    def select(self, *a, **k): return self
    def in_(self, *a, **k): return self
    def gte(self, *a, **k): return self
    def order(self, *a, **k): return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        start, end = self._range
        self._call_log.append((start, end))
        return _FakeResp(self._all_rows[start:end + 1])


class _FakeTable:
    def __init__(self, all_rows, call_log):
        self._all_rows = all_rows
        self._call_log = call_log

    def select(self, *a, **k):
        return _FakeQuery(self._all_rows, self._call_log)


class _FakeSupabase:
    def __init__(self, all_rows):
        self._all_rows = all_rows
        self.call_log = []

    def table(self, name):
        assert name == "price_history"
        return _FakeTable(self._all_rows, self.call_log)


class TestFetchPriceRowsPagination:
    def test_fetches_second_page_when_first_page_is_exactly_full(self):
        rows = [
            {"card_name": "カードA", "shop": "遊々亭", "rarity": "レア",
             "min_price": 100, "min_price_any": 100, "code": "", "url": None,
             "recorded_at": "2026-08-08"}
            for _ in range(1000)
        ]
        sb = _FakeSupabase(rows)
        result = _fetch_price_rows(sb, ["カードA"], "2026-08-01")
        assert len(result) == 1000
        # 1000行ちょうど→2ページ目（0件）も取得しに行っている
        assert len(sb.call_log) == 2
        assert sb.call_log[0] == (0, 999)
        assert sb.call_log[1] == (1000, 1999)


class TestFetchPriceRowsExceptionBoundary:
    def test_raises_and_logs_warning(self, caplog):
        class _BoomQuery:
            def select(self, *a, **k): return self
            def in_(self, *a, **k): return self
            def gte(self, *a, **k): return self
            def order(self, *a, **k): return self
            def range(self, *a, **k): return self
            def execute(self):
                raise RuntimeError("boom")

        class _BoomTable:
            def select(self, *a, **k): return _BoomQuery()

        class _BoomSupabase:
            def table(self, name): return _BoomTable()

        with caplog.at_level(logging.WARNING, logger="featured_matrix"):
            with pytest.raises(RuntimeError):
                _fetch_price_rows(_BoomSupabase(), ["カードA"], "2026-08-01")
        assert any("取得失敗" in rec.message for rec in caplog.records)
