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
  - _fetch_price_rows（fakeクライアント、ページング・例外境界・カード番号＋日付の等号で引くこと）
  - get_pack_matrix（カード名→カード番号→記録、1時間のキャッシュ）
  - _chunk_card_names_by_bytes（バイト予算でのチャンク分割）

ネットワーク・実Supabaseは一切使わない。
"""

import logging
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import featured_matrix
from featured_matrix import build_matrix, _chunk_card_names_by_bytes, _fetch_price_rows, get_pack_matrix
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
    def eq(self, *a, **k): return self
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
        result = _fetch_price_rows(sb, [1], ["2026-08-08"])
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
            def eq(self, *a, **k): return self
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
                _fetch_price_rows(_BoomSupabase(), [1], ["2026-08-01"])
        assert any("取得失敗" in rec.message for rec in caplog.records)


# ──────────────────────────────────────────────
# get_pack_matrix（どのパックでも作る表。2026-10-22）
# ──────────────────────────────────────────────

class _RecSupabase:
    """card_dim と price_history の呼び出しを記録する作り物。price_history は渡した日付・カード番号で絞って返す"""

    def __init__(self, names, rows):
        self.names = names  # {card_name: card_id}
        self.rows = rows
        self.calls = []

    def table(self, name):
        sb = self

        class Q:
            def __init__(self):
                self.f = {}

            def select(self, *a, **k): return self

            @property
            def not_(self):
                self.f["_neg"] = True
                return self

            def in_(self, col, vals):
                if self.f.pop("_neg", False):
                    self.f["not_" + col] = list(vals)
                else:
                    self.f[col] = list(vals)
                return self

            def eq(self, col, val):
                self.f[col] = val
                return self

            def order(self, *a, **k): return self

            def range(self, start, end):
                self.f["range"] = (start, end)
                return self

            def execute(self):
                sb.calls.append((name, dict(self.f)))
                if name == "card_dim":
                    return _FakeResp([{"card_id": sb.names[n], "card_name": n} for n in self.f["card_name"] if n in sb.names])
                assert name == "price_history"
                assert "recorded_at" in self.f and isinstance(self.f["recorded_at"], str), "日付は等号で引く"
                assert "card_id" in self.f, "カード番号で引く（カード名で探さない）"
                skip = self.f.get("not_shop_id", [])
                return _FakeResp([r for r in sb.rows if r["card_id"] in self.f["card_id"] and r["recorded_at"] == self.f["recorded_at"]
                                  and r.get("shop_id") not in skip])
        return Q()


class TestGetPackMatrix:
    def setup_method(self):
        featured_matrix._cache.clear()

    def test_builds_from_card_ids_and_caches(self, monkeypatch):
        class _D(featured_matrix.datetime):
            @classmethod
            def now(cls, tz=None):
                return featured_matrix.datetime(2026, 10, 22, 9, 0, tzinfo=tz)
        monkeypatch.setattr(featured_matrix, "datetime", _D)
        rows = [
            dict(_row("カードA", "遊々亭", "ウルトラ", 300, 300, "2026-10-21", code="DBGV-JP001"), card_id=1),
            dict(_row("カードA", "遊々亭", "ウルトラ", 280, 280, "2026-10-22", code="DBGV-JP001"), card_id=1),
            dict(_row("カードA", "カードラボ", "ウルトラ", 350, 350, "2026-10-21", code="DBGV-JP001"), card_id=1),
            dict(_row("カードB", "遊々亭", "ノーマル", 20, 20, "2026-10-15"), card_id=2),  # 2日より前は読まない
        ]
        sb = _RecSupabase({"カードA": 1, "カードB": 2}, rows)
        out = get_pack_matrix(sb, ["カードA", "カードB", "名簿に無いカード"])
        days = sorted({c[1]["recorded_at"] for c in sb.calls if c[0] == "price_history"})
        assert days == ["2026-10-21", "2026-10-22"]
        a = [r for r in out["rows"] if r["name"] == "カードA"]
        assert a[0]["cells"]["遊々亭"]["price"] == 280, "新しい日の値を使う"
        assert a[0]["cells"]["カードラボ"]["price"] == 350, "今日の記録が無い店は昨日の値で埋める"
        assert [r["name"] for r in out["rows"]][-2:] == ["カードB", "名簿に無いカード"]
        assert out["updated_at"] == "2026-10-22"
        n = len(sb.calls)
        out2 = get_pack_matrix(sb, ["カードA", "カードB", "名簿に無いカード"])
        assert out2 == out and len(sb.calls) == n, "1時間以内の同じパックはデータベースを読まない"



class TestPrepareRecordsAndCache:
    """reviewer 指摘への対応（2026-10-22）"""

    def setup_method(self):
        featured_matrix._cache.clear()

    def test_shop_with_today_rows_drops_yesterday(self):
        """同じカード×店に今日の記録があれば昨日の記録を使わない（今日売り切れた品に昨日の値が残らない）。まだ取っていないカード×店は昨日で埋める"""
        recs = [
            _row("A", "店1", "ウルトラ", 100, 100, "2026-10-22"),
            _row("A", "店1", "シークレット", 500, 500, "2026-10-21"),  # A×店1は今日取った → 昨日のシークレットは捨てる
            _row("A", "店2", "ウルトラ", 120, 120, "2026-10-21"),      # A×店2はまだ → 残す
        ]
        out = featured_matrix._prepare_records(recs)
        keys = {(r["shop"], r["rarity"]) for r in out}
        assert keys == {("店1", "ウルトラ"), ("店2", "ウルトラ")}

    def test_one_card_searched_today_keeps_other_cards_yesterday(self):
        """深夜に1枚だけ検索されて今日の記録ができても、ほかのカードのその店の昨日の記録は残す（2026-11-01 IMMORTAL PHOENIX）"""
        recs = [
            _row("A", "店1", "ウルトラ", 100, 100, "2026-11-01"),
            _row("A", "店1", "ウルトラ", 110, 110, "2026-10-31"),
            _row("B", "店1", "シークレット", 900, 900, "2026-10-31"),
            _row("C", "店2", "ノーマル", 30, 30, "2026-10-31"),
        ]
        out = featured_matrix._prepare_records(recs)
        assert sorted((r["card_name"], r["shop"], r["recorded_at"]) for r in out) == [
            ("A", "店1", "2026-11-01"), ("B", "店1", "2026-10-31"), ("C", "店2", "2026-10-31")]

    def test_code_cleared_when_shown_price_is_another_listing(self):
        """表に出す値段（min_price_any）と型番の出品（min_price）が違う行は、型番を空（版不明）にする"""
        recs = [
            _row("A", "店1", "ノーマル", 30, 15, "2026-10-22", code="RC04-JP001"),
            _row("A", "店2", "ノーマル", 20, 20, "2026-10-22", code="DBGV-JP012"),
        ]
        out = {r["shop"]: r for r in featured_matrix._prepare_records(recs)}
        assert out["店1"]["code"] == ""
        assert out["店2"]["code"] == "DBGV-JP012"

    def test_cache_key_ignores_order_and_failure_is_remembered(self, monkeypatch):
        class _D(featured_matrix.datetime):
            @classmethod
            def now(cls, tz=None):
                return featured_matrix.datetime(2026, 10, 22, 9, 0, tzinfo=tz)
        monkeypatch.setattr(featured_matrix, "datetime", _D)
        rows = [dict(_row("カードA", "遊々亭", "ウルトラ", 280, 280, "2026-10-22"), card_id=1),
                dict(_row("カードB", "遊々亭", "ノーマル", 20, 20, "2026-10-22"), card_id=2)]
        sb = _RecSupabase({"カードA": 1, "カードB": 2}, rows)
        out1 = get_pack_matrix(sb, ["カードA", "カードB"])
        n = len(sb.calls)
        out2 = get_pack_matrix(sb, ["カードB", "カードA"])
        assert len(sb.calls) == n, "並びが違っても同じ集まりならデータベースを読まない"
        assert [r["name"] for r in out2["rows"]] == ["カードB", "カードA"], "並び順は呼び出しごとに当てる"
        assert featured_matrix.is_cached(["カードA", "カードB"])

        class _Boom:
            def table(self, name):
                raise RuntimeError("db down")
        with pytest.raises(RuntimeError):
            get_pack_matrix(_Boom(), ["カードC"])
        assert featured_matrix.is_cached(["カードC"]), "失敗も短い間は覚える"
        with pytest.raises(RuntimeError):
            get_pack_matrix(sb, ["カードC"])  # 覚えている間は読みに行かずに失敗を返す
        assert not any(c[1].get("card_name") == ["カードC"] for c in sb.calls)


class TestFetchFewerRoundTrips:
    """最初の読み込みが遅い件（2026-10-22）: 昨日は足りない店のぶんだけ・束は同時に・接続切れは1回だけ送り直す"""

    def setup_method(self):
        featured_matrix._cache.clear()

    @staticmethod
    def _r(card_id, shop, day):
        shop_id = featured_matrix.DEFAULT_SHOPS.index(shop) + 1
        return dict(_row(f"カード{card_id}", shop, "ウルトラ", 100, 100, day), card_id=card_id, shop_id=shop_id)

    def test_yesterday_not_read_when_all_shops_have_today(self):
        rows = [self._r(1, s, "2026-10-22") for s in featured_matrix.DEFAULT_SHOPS]
        rows += [self._r(1, "遊々亭", "2026-10-21")]
        sb = _RecSupabase({}, rows)
        out = _fetch_price_rows(sb, [1], ["2026-10-22", "2026-10-21"])
        assert {c[1]["recorded_at"] for c in sb.calls} == {"2026-10-22"}, "全部の店が今日揃ったら昨日は読まない"
        assert len(out) == len(featured_matrix.DEFAULT_SHOPS)

    def test_yesterday_reads_only_cards_missing_shops_today(self):
        """昨日は、今日の記録で全部の店がそろっていないカードだけ読む。店はパック全体で除かない（2026-11-01）"""
        full = [self._r(1, s, "2026-10-22") for s in featured_matrix.DEFAULT_SHOPS]
        rows = full + [self._r(1, "遊々亭", "2026-10-21"),
                       self._r(2, "遊々亭", "2026-10-22"), self._r(2, "カードラボ", "2026-10-21"),
                       self._r(3, "遊々亭", "2026-10-21"), self._r(3, "カーナベル", "2026-10-21")]
        sb = _RecSupabase({}, rows)
        out = _fetch_price_rows(sb, [1, 2, 3], ["2026-10-22", "2026-10-21"])
        y = [c[1] for c in sb.calls if c[1]["recorded_at"] == "2026-10-21"]
        assert y and sorted(i for c in y for i in c["card_id"]) == [2, 3], "全店そろったカード1は昨日を読まない"
        assert all("not_shop_id" not in c for c in y), "店をパック全体で除かない"
        got = {(r["card_id"], r["shop"]) for r in featured_matrix._prepare_records(out)}
        assert (3, "遊々亭") in got and (3, "カーナベル") in got, "カード1・2で今日の記録がある店でも、カード3は昨日で埋める"
        assert (2, "カードラボ") in got

    def test_card_ids_are_split_into_chunks(self):
        ids = list(range(1, featured_matrix._ID_CHUNK * 2 + 2))
        sb = _RecSupabase({}, [])
        _fetch_price_rows(sb, ids, ["2026-10-22"])
        sizes = sorted(len(c[1]["card_id"]) for c in sb.calls)
        assert sizes == [1, featured_matrix._ID_CHUNK, featured_matrix._ID_CHUNK]

    def test_retries_once_on_connection_terminated(self):
        import httpx
        calls = {"n": 0}

        class Q:
            def execute(self):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise httpx.RemoteProtocolError("<ConnectionTerminated error_code:1>")
                return _FakeResp([1])
        assert featured_matrix._execute(lambda: Q()).data == [1]
        assert calls["n"] == 2

    def test_other_errors_are_not_retried(self):
        calls = {"n": 0}

        class Q:
            def execute(self):
                calls["n"] += 1
                raise RuntimeError("timeout")
        with pytest.raises(RuntimeError):
            featured_matrix._execute(lambda: Q())
        assert calls["n"] == 1, "遅い・失敗が続くときは送り直さない"
