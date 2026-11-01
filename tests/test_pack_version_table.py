"""パックの版ごとの価格（新弾用の小さな表 price_history_pack。2026-11-01）。"""
import featured_matrix
from price_persist import build_pack_price_rows, valid_pack_code
from tests.test_featured_matrix import _RecSupabase, _row


def _item(code, price, rarity="ウルトラ", shop="遊々亭"):
    return {"shop": shop, "name": "ラーの翼神竜", "rarity": rarity, "code": code, "condition": "-",
            "price": price, "stock": 1, "sold_out": False, "url": f"u{price}", "image": ""}


def test_valid_pack_code():
    assert valid_pack_code(" imph ") == "IMPH"
    assert valid_pack_code("IMPH-JP") == "" and valid_pack_code("") == "" and valid_pack_code(None) == ""


def test_build_pack_rows_only_this_pack_version():
    """同じレアリティでほかの版が安くても、このパックの版の最安を残す（本表は安い版しか残らない）"""
    results = [_item("MB01-JPS03", 300), _item("IMPH-JPS01", 980), _item("IMPH-JPS01", 1100), _item("IMPHJPS01", 900),
               _item("", 100), _item("IMPH-JPS01", 50, rarity="")]
    rows = build_pack_price_rows("ラーの翼神竜", results, "2026-11-01", "imph")
    assert [(r["pack_code"], r["rarity"], r["min_price"], r["code"]) for r in rows] == [("IMPH", "ウルトラ", 980, "IMPH-JPS01")],         "型番はハイフンまでで読む（画面と同じ）・型番の無い出品とレアリティ不明の行は入れない"
    assert build_pack_price_rows("ラーの翼神竜", results, "2026-11-01", "") == []


class _PackSb(_RecSupabase):
    """price_history_pack_v も返す作り物"""

    def __init__(self, names, rows, pack_rows):
        super().__init__(names, rows)
        self.pack_rows = pack_rows

    def table(self, name):
        q = super().table(name)
        if name != "price_history_pack_v":
            return q
        sb, orig = self, q.execute

        def execute():
            f = q.f
            assert "pack_code" not in f, "版ごとの表は pack_code で絞らず、日付×カードで引く"
            days = f["recorded_at"] if isinstance(f["recorded_at"], list) else [f["recorded_at"]]
            return featured_matrix_resp([r for r in sb.pack_rows if r["card_id"] in f["card_id"] and r["recorded_at"] in days])
        q.execute = execute
        return q


def featured_matrix_resp(data):
    from tests.test_featured_matrix import _FakeResp
    return _FakeResp(data)


def test_pack_matrix_returns_pack_rows(monkeypatch):
    featured_matrix._cache.clear()
    featured_matrix._pack_table_missing = False

    class _D(featured_matrix.datetime):
        @classmethod
        def now(cls, tz=None):
            return featured_matrix.datetime(2026, 11, 1, 12, 0, tzinfo=tz)
    monkeypatch.setattr(featured_matrix, "datetime", _D)
    rows = [dict(_row("ラーの翼神竜", "遊々亭", "ウルトラ", 300, 300, "2026-11-01", code="MB01-JPS03"), card_id=5)]
    pack = [dict(_row("ラーの翼神竜", "遊々亭", "ウルトラ", 900, 900, "2026-10-31", code="IMPH-JPS01"), card_id=5, pack_code="IMPH"),
            dict(_row("ラーの翼神竜", "遊々亭", "ウルトラ", 980, 980, "2026-11-01", code="IMPH-JPS01"), card_id=5, pack_code="IMPH")]
    out = featured_matrix.get_pack_matrix(_PackSb({"ラーの翼神竜": 5}, rows, pack), ["ラーの翼神竜"])
    assert out["rows"][0]["cells"]["遊々亭"]["price"] == 300, "本表の行はそのまま"
    assert out["pack_rows"] == [{"name": "ラーの翼神竜", "rarity": "ウルトラ", "shop": "遊々亭", "price": 980, "url": None,
                                 "code": "IMPH-JPS01", "recorded_at": "2026-11-01", "pack_code": "IMPH"}], "新しい日の1行"
    assert featured_matrix.is_cached(["ラーの翼神竜"])
    featured_matrix._cache.clear()


def test_pack_cells_drops_older_than_main_same_version():
    """本表に同じ版のより新しい行があれば、版ごとの古い行は使わない（reviewer 高2）。ほかの版の本表の行では落とさない"""
    pack = [{"card_name": "R", "shop": "店", "rarity": "ウルトラ", "min_price": 980, "min_price_any": 980,
             "code": "IMPH-JPS01", "url": None, "recorded_at": "2026-10-31", "pack_code": "IMPH"}]
    same = [{"card_name": "R", "shop": "店", "rarity": "ウルトラ", "code": "IMPH-JPS01", "recorded_at": "2026-11-01"}]
    other = [{"card_name": "R", "shop": "店", "rarity": "ウルトラ", "code": "MB01-JPS03", "recorded_at": "2026-11-01"}]
    assert featured_matrix.pack_cells(pack, same) == []
    assert [c["price"] for c in featured_matrix.pack_cells(pack, other)] == [980]


def test_missing_pack_view_does_not_break_matrix(monkeypatch):
    featured_matrix._cache.clear()
    featured_matrix._pack_table_missing = False

    class _Broken(_RecSupabase):
        def table(self, name):
            if name == "price_history_pack_v":
                raise RuntimeError("{'code': 'PGRST205', 'message': 'Could not find the table'}")
            return super().table(name)
    rows = [dict(_row("A", "遊々亭", "ウルトラ", 300, 300, "2026-11-01"), card_id=1)]
    out = featured_matrix.get_pack_matrix(_Broken({"A": 1}, rows), ["A"])
    assert out["pack_rows"] == [] and featured_matrix._pack_table_missing
    featured_matrix._pack_table_missing = False
    featured_matrix._cache.clear()
