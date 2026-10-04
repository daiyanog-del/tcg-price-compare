"""取得失敗を0件扱いしないAPI契約の回帰検証。"""
import json
import pytest
import app as server


@pytest.mark.parametrize("route,query,shops_attr,cache_get,cache_store", [
    ("/api/search", {"q": "青眼の白龍", "confirmed": "true"}, "SHOPS", "cache_get_shops", "cache_store_shops"),
    ("/api/buyback", {"q": "青眼の白龍", "confirmed": "true"}, "BUYBACK_SHOPS", "buyback_cache_get_shops", "buyback_cache_store_shops"),
    ("/api/deck", {"cards": "青眼の白龍"}, "SHOPS", "cache_get_shops", "cache_store_shops"),
    ("/api/deck-buy", {"cards": "青眼の白龍"}, "BUYBACK_SHOPS", "buyback_cache_get_shops", "buyback_cache_store_shops"),
])
@pytest.mark.parametrize("case,expected", [("empty", "empty"), ("failed", "failed"), ("partial", "partial"), ("exception", "failed")])
def test_failures_survive_completion(monkeypatch, route, query, shops_attr, cache_get, cache_store, case, expected):
    def fetch(_):
        pass
    monkeypatch.setattr(server, shops_attr, [("テスト店", fetch)])
    monkeypatch.setattr(server, cache_get, lambda name, shops, **kw: ({}, shops))
    monkeypatch.setattr(server, cache_store, lambda *a, **kw: None)
    monkeypatch.setattr(server, "_supabase_client", None)
    monkeypatch.setattr(server, "_load_cardnames", lambda: None)
    monkeypatch.setattr(server, "_record_search", lambda *a: None)
    monkeypatch.setattr(server, "_record_deck_search", lambda *a: None)
    monkeypatch.setattr(server.tracker, "record_success", lambda *a: None)
    monkeypatch.setattr(server.tracker, "record_failure", lambda *a: None)
    def result(*_):
        if case == "exception":
            raise RuntimeError("テスト用取得障害")
        items = [{"shop": "テスト店", "name": "青眼の白龍", "price": 20, "rarity": "ノーマル", "sold_out": False}] if case == "partial" else []
        return items, int(case != "empty")
    monkeypatch.setattr(server, "run_shop_with_status", result)
    server._reset_rate_limits()
    response = server.app.test_client().get(route, query_string={**query, "shops": "テスト店"})
    events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines() if line.startswith("data: ")]
    done = next(e for e in events if e["type"] == ("card_done" if route.startswith("/api/deck") else "done"))
    assert done["status"] == expected
    assert done["failed_shops"] == ([] if case == "empty" else ["テスト店"])
    assert done["successful_shops"] == (["テスト店"] if case == "empty" else [])
    if case == "partial":
        assert done.get("best") or done.get("results")
