"""価格のデータベースを名前で引く読み手が、収集と同じそろえ方の名前でも引くこと（2026-11-01）。

パックの表で、Wiki の全角表記「Ｎｏ.９６ …」が、収集が半角にして保存した「No.96 …」と完全一致せず
「記録がありません」になった。読み手（featured_matrix・price_history_reads）の入口でそろえ、
行のカード名は呼び出し元の名前に戻す。
"""
import featured_matrix
import price_history_reads as phr
from name_normalize import db_lookup_names, pick_card_ids
from tests.test_featured_matrix import _RecSupabase, _row


FW = "Ｎｏ.９６ ブラック・マリオネッター"
HW = "No.96 ブラック・マリオネッター"


def test_lookup_adds_normalized_name():
    assert db_lookup_names([FW, "カードA"]) == {FW: FW, HW: FW, "カードA": "カードA"}


def test_lookup_prefers_callers_own_name():
    """そろえた名前が別の呼び出し元の名前と同じなら、その呼び出し元に割り振る"""
    assert db_lookup_names([FW, HW]) == {FW: FW, HW: HW}


def test_pick_prefers_exact_match():
    """完全一致したカードがあれば、そろえた名前でだけ一致したカードは混ぜない"""
    lookup = db_lookup_names([FW])
    assert pick_card_ids([{"card_id": 1, "card_name": FW}, {"card_id": 2, "card_name": HW}], lookup) == {1: FW}
    assert pick_card_ids([{"card_id": 2, "card_name": HW}], lookup) == {2: FW}


def test_pack_matrix_finds_full_width_name(monkeypatch):
    featured_matrix._cache.clear()

    class _D(featured_matrix.datetime):
        @classmethod
        def now(cls, tz=None):
            return featured_matrix.datetime(2026, 11, 1, 9, 0, tzinfo=tz)
    monkeypatch.setattr(featured_matrix, "datetime", _D)
    rows = [dict(_row(HW, "遊々亭", "ウルトラ", 300, 300, "2026-11-01"), card_id=7)]
    out = featured_matrix.get_pack_matrix(_RecSupabase({HW: 7}, rows), [FW])
    assert [r["name"] for r in out["rows"]] == [FW]
    assert out["rows"][0]["cells"]["遊々亭"]["price"] == 300
    featured_matrix._cache.clear()


def test_daily_rows_find_full_width_name():
    rows = [dict(_row(HW, "遊々亭", "ウルトラ", 300, 300, "2026-11-01"), card_id=7, shop_id=1)]
    got, failed = phr.fetch_daily_rows_by_card_names(
        _RecSupabase({HW: 7}, rows), [FW], "card_id, card_name, shop, min_price", ["2026-11-01"], "t")
    assert failed == set()
    assert [(r["card_name"], r["min_price"]) for r in got] == [(FW, 300)], "行の名前は呼び出し元の名前に戻す"


class _FailSb(_RecSupabase):
    """card_dim の問い合わせで、指定の名前を含む束だけ失敗させる"""

    def __init__(self, names, rows, fail_name):
        super().__init__(names, rows)
        self.fail_name = fail_name

    def table(self, name):
        q = super().table(name)
        if name != "card_dim":
            return q
        orig_execute, sb = q.execute, self

        def execute():
            if sb.fail_name in q.f.get("card_name", []):
                raise RuntimeError("読めない")
            return orig_execute()
        q.execute = execute
        return q


def _split_chunks(monkeypatch):
    monkeypatch.setattr(phr, "_chunk_card_names_by_bytes", lambda names: [[n] for n in names])


def test_exact_chunk_failed_does_not_use_loose_card(monkeypatch):
    """呼び出し元の名前そのものの束が読めなければ、そろえた名前の別カードの値段を混ぜず失敗にする（reviewer 中1）"""
    _split_chunks(monkeypatch)
    rows = [dict(_row(FW, "遊々亭", "ウルトラ", 100, 100, "2026-11-01"), card_id=1, shop_id=1),
            dict(_row(HW, "遊々亭", "ウルトラ", 50, 50, "2026-11-01"), card_id=2, shop_id=1)]
    got, failed = phr.fetch_daily_rows_by_card_names(
        _FailSb({FW: 1, HW: 2}, rows, FW), [FW], "card_id, card_name, shop, min_price", ["2026-11-01"], "t")
    assert got == [] and failed == {FW}


def test_loose_chunk_failed_keeps_exact_card(monkeypatch):
    """そろえた名前の束だけ読めなくても、完全一致で読めたカードは失敗にしない（reviewer 低1）"""
    _split_chunks(monkeypatch)
    rows = [dict(_row(FW, "遊々亭", "ウルトラ", 100, 100, "2026-11-01"), card_id=1, shop_id=1)]
    got, failed = phr.fetch_daily_rows_by_card_names(
        _FailSb({FW: 1}, rows, HW), [FW], "card_id, card_name, shop, min_price", ["2026-11-01"], "t")
    assert failed == set() and [r["min_price"] for r in got] == [100]
