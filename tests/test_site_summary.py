"""
tests/test_site_summary.py — 重い集計の作り置き（site_summary.fetch）

確かめること:
  - 作ったときの引数が今の引数と同じで新しければ作り置きを使い、RPC を呼ばない
  - 引数が違う・古い・まだ作られていない・表が読めないときは、その場で RPC を呼ぶ（例外も呼び出し側へ）
  - 依頼（kind・rpc・params）は引数が変わったときだけ書き直す。書けなくても止まらない
  - 引数の比べ方はキーの順番とタプル／リストの違いを気にしない
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import site_summary


class _Resp:
    def __init__(self, data):
        self.data = data


class _Table:
    def __init__(self, sb):
        self.sb = sb

    def upsert(self, row, on_conflict=None, returning=None):
        if self.sb.fail_write:
            raise RuntimeError("書けない")
        self.sb.upserts.append(row)
        return self

    def select(self, *_a):
        return self

    def eq(self, _k, v):
        self.kind = v
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self.sb.fail_read:
            raise RuntimeError("読めない")
        row = self.sb.rows.get(getattr(self, "kind", None))
        return _Resp([row] if row else [])


class _Rpc:
    def __init__(self, sb, name, params):
        self.sb, self.name, self.params = sb, name, params

    def execute(self):
        self.sb.rpc_calls.append((self.name, self.params))
        if self.sb.rpc_error:
            raise self.sb.rpc_error
        return _Resp([{"live": True}])


class _Sb:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.upserts, self.rpc_calls = [], []
        self.fail_read = self.fail_write = False
        self.rpc_error = None

    def table(self, name):
        assert name == "site_summaries"
        return _Table(self)

    def rpc(self, name, params):
        return _Rpc(self, name, params)


@pytest.fixture(autouse=True)
def _reset():
    site_summary._registered.clear()
    site_summary._warned_at.clear()


PARAMS = {"p_min_price": 1000, "p_limit": 80, "p_rarities": ("OFウルトラ", "OFスーパー")}


def _row(params_used=None, age_sec=60, payload=None):
    return {"params_used": params_used if params_used is not None
            else {"p_rarities": ["OFウルトラ", "OFスーパー"], "p_limit": 80, "p_min_price": 1000},
            "payload": payload if payload is not None else [{"cached": True}],
            "computed_epoch": time.time() - age_sec}


def test_uses_fresh_payload_with_same_params():
    sb = _Sb({"top_movers:of": _row()})
    assert site_summary.fetch(sb, "top_movers:of", "get_top_movers", PARAMS) == [{"cached": True}]
    assert sb.rpc_calls == []


def test_fetch_with_time_returns_when_it_was_built():
    sb = _Sb({"top_movers:of": _row(age_sec=600)})
    _data, built = site_summary.fetch_with_time(sb, "top_movers:of", "get_top_movers", PARAMS)
    assert abs((time.time() - built) - 600) < 5
    sb = _Sb()
    _data, built = site_summary.fetch_with_time(sb, "top_movers:of", "get_top_movers", PARAMS)
    assert abs(time.time() - built) < 5  # その場で計算したら今の時刻


def test_rewrites_request_overwritten_by_another_process():
    """別のプロセスが違う引数を書いたら、気づいて書き直す（reviewer 中2）"""
    sb = _Sb()
    site_summary.fetch(sb, "price_movers", "get_price_movers", {"top_n": 20})
    sb.rows["price_movers"] = {**_row(params_used={"top_n": 20}), "params": {"top_n": 5}}
    assert site_summary.fetch(sb, "price_movers", "get_price_movers", {"top_n": 20}) == [{"cached": True}]
    assert [u["params"] for u in sb.upserts] == [{"top_n": 20}, {"top_n": 20}]


@pytest.mark.parametrize("row", [
    None,                                            # まだ無い
    _row(params_used={"p_min_price": 500}),          # 条件が違う（定数を変えた・日付が変わった）
    _row(age_sec=site_summary.MAX_AGE_SEC + 60),     # 古い（定期実行が止まっている）
    {"params_used": None, "payload": None, "computed_epoch": None},  # 依頼だけあってまだ作られていない
    {**_row(), "computed_epoch": None},              # 時刻の無い行（作り直しの途中など）
])
def test_falls_back_to_live_rpc(row):
    sb = _Sb({"top_movers:of": row} if row else {})
    assert site_summary.fetch(sb, "top_movers:of", "get_top_movers", PARAMS) == [{"live": True}]
    assert sb.rpc_calls == [("get_top_movers", PARAMS)]


def test_read_failure_falls_back_and_rpc_errors_propagate():
    sb = _Sb({"best_prices": _row()})
    sb.fail_read = True
    sb.rpc_error = ValueError("57014")
    with pytest.raises(ValueError):
        site_summary.fetch(sb, "best_prices", "get_card_best_prices_json", {"cutoff_date": "2026-10-25"})


def test_registers_only_when_params_change():
    sb = _Sb()
    for _ in range(3):
        site_summary.fetch(sb, "price_movers", "get_price_movers", {"cutoff_date": "2026-10-29", "top_n": 20})
    site_summary.fetch(sb, "price_movers", "get_price_movers", {"cutoff_date": "2026-10-30", "top_n": 20})
    assert [u["params"]["cutoff_date"] for u in sb.upserts] == ["2026-10-29", "2026-10-30"]
    assert sb.upserts[0] == {"kind": "price_movers", "rpc": "get_price_movers",
                             "params": {"cutoff_date": "2026-10-29", "top_n": 20}}


def test_register_failure_does_not_stop_and_retries_next_time():
    sb = _Sb()
    sb.fail_write = True
    assert site_summary.fetch(sb, "staple_movers", "get_staple_movers", {}) == [{"live": True}]
    sb.fail_write = False
    site_summary.fetch(sb, "staple_movers", "get_staple_movers", {})
    assert sb.upserts == [{"kind": "staple_movers", "rpc": "get_staple_movers", "params": {}}]


# ── 相場キャッシュ（app._load_estimate_cache_inner）の経路の分かれ方 ──

@pytest.mark.parametrize("value, route_rows", [
    ([{"card_name": "灰流うらら", "best_price": 3000}], 1),  # 作り置き（list）→ json 経路のまま
])
def test_estimate_cache_uses_summary_list(monkeypatch, value, route_rows):
    import app as app_module
    monkeypatch.setattr(app_module, "_supabase_client", object())
    monkeypatch.setattr(app_module.site_summary, "fetch", lambda sb, kind, rpc, params: value)
    monkeypatch.setattr(app_module, "_estimate_cache", {})
    app_module._load_estimate_cache_inner(startup=False)
    assert "灰流うらら" in app_module._estimate_cache


def test_estimate_cache_falls_to_paged_route_when_summary_is_not_a_list(monkeypatch):
    import app as app_module
    called = []

    class _PagedSb:
        def rpc(self, name, params):
            called.append(name)
            class _Q:
                def order(self, *_a): return self
                def range(self, *_a): return self
                def execute(self):
                    return type("R", (), {"data": [{"card_name": "増殖するG", "best_price": 500}]})()
            return _Q()
    monkeypatch.setattr(app_module, "_supabase_client", _PagedSb())
    monkeypatch.setattr(app_module.site_summary, "fetch", lambda sb, kind, rpc, params: {"unexpected": True})
    monkeypatch.setattr(app_module, "_estimate_cache", {})
    app_module._load_estimate_cache_inner(startup=False)
    assert called == ["get_card_best_prices"]
    assert "増殖するG" in app_module._estimate_cache
