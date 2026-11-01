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
    results = [_item("MB01-JPS03", 300), _item("IMPH-JPS01", 980), _item("IMPHJPS01", 900), _item("", 100)]
    rows = build_pack_price_rows("ラーの翼神竜", results, "2026-11-01", "imph")
    assert [(r["pack_code"], r["rarity"], r["min_price"], r["code"]) for r in rows] == [("IMPH", "ウルトラ", 900, "IMPHJPS01")]
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
            assert f.get("pack_code") == "IMPH"
            return featured_matrix_resp([r for r in sb.pack_rows if r["card_id"] in f["card_id"] and r["recorded_at"] == f["recorded_at"]])
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
    pack = [dict(_row("ラーの翼神竜", "遊々亭", "ウルトラ", 900, 900, "2026-10-31", code="IMPH-JPS01"), card_id=5),
            dict(_row("ラーの翼神竜", "遊々亭", "ウルトラ", 980, 980, "2026-11-01", code="IMPH-JPS01"), card_id=5)]
    out = featured_matrix.get_pack_matrix(_PackSb({"ラーの翼神竜": 5}, rows, pack), ["ラーの翼神竜"], "IMPH")
    assert out["rows"][0]["cells"]["遊々亭"]["price"] == 300, "本表の行はそのまま"
    assert out["pack_rows"] == [{"name": "ラーの翼神竜", "rarity": "ウルトラ", "shop": "遊々亭", "price": 980,
                                 "url": None, "code": "IMPH-JPS01", "recorded_at": "2026-11-01"}], "新しい日の1行"
    # 型番の頭を渡さない呼び出しは別の鍵で覚え、版ごとの価格を読まない
    out2 = featured_matrix.get_pack_matrix(_PackSb({"ラーの翼神竜": 5}, rows, pack), ["ラーの翼神竜"])
    assert out2["pack_rows"] == []
    assert featured_matrix.is_cached(["ラーの翼神竜"], "IMPH") and featured_matrix.is_cached(["ラーの翼神竜"])
    featured_matrix._cache.clear()


def test_missing_pack_view_does_not_break_matrix(monkeypatch):
    featured_matrix._cache.clear()
    featured_matrix._pack_table_missing = False

    class _Broken(_RecSupabase):
        def table(self, name):
            if name == "price_history_pack_v":
                raise RuntimeError("{'code': 'PGRST205', 'message': 'Could not find the table'}")
            return super().table(name)
    rows = [dict(_row("A", "遊々亭", "ウルトラ", 300, 300, "2026-11-01"), card_id=1)]
    out = featured_matrix.get_pack_matrix(_Broken({"A": 1}, rows), ["A"], "IMPH")
    assert out["pack_rows"] == [] and featured_matrix._pack_table_missing
    featured_matrix._pack_table_missing = False
    featured_matrix._cache.clear()
