"""受付上限と店舗障害を区別し、共有取得の監視記録を一回にする。"""
import json
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from weakref import WeakSet

import pytest
import app as app_module
import scraper
from shop_search_pool import PoolCapacityError


@pytest.fixture
def records(monkeypatch):
    records = []
    monkeypatch.setattr(app_module, "_shop_health_recorded", WeakSet())
    monkeypatch.setattr(app_module, "tracker", SimpleNamespace(
        record_success=lambda shop, count: records.append(("success", shop, count)),
        record_failure=lambda shop, error: records.append(("failure", shop, error))))
    return records


@pytest.mark.parametrize("errors", [0, 1])
def test_concurrent_waiters_record_actual_fetch_once(records, errors):
    future = Future()
    future.set_result(([], errors))
    barrier = Barrier(4)
    def waiter(_):
        barrier.wait()
        app_module._record_shop_health_once(future, "店A", [], errors)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(waiter, range(4)))
    assert len(records) == 1
    assert records[0][0] == ("failure" if errors else "success")
    another = Future()
    another.set_result(([], errors))
    app_module._record_shop_health_once(another, "店A", [], errors)
    assert len(records) == 2


def test_capacity_is_not_a_shop_health_event(records):
    future = Future()
    future.set_exception(PoolCapacityError("受付上限"))
    app_module._record_shop_health_once(future, "店A", [], 1)
    assert records == []


@pytest.mark.parametrize("route", ["/api/search", "/api/buyback"])
@pytest.mark.parametrize("capacity", [False, True])
def test_route_reports_failure_but_deduplicates_health(monkeypatch, records, route, capacity):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", False)
    monkeypatch.setattr(app_module, "_supabase_client", None)
    monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
    monkeypatch.setattr(app_module, "_correct_cardname", lambda name: name)
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: [])])
    monkeypatch.setattr(app_module, "BUYBACK_SHOPS", [("店A", lambda _: [])])
    future = Future()
    if capacity:
        future.set_exception(PoolCapacityError("受付上限"))
    else:
        future.set_exception(RuntimeError("店舗の取得例外"))
    monkeypatch.setattr(app_module, "_submit_shop_fetch", lambda *_: future)
    client = app_module.app.test_client()
    for _ in range(2):
        app_module._reset_rate_limits()
        response = client.get(route, query_string={"q": "テスト", "confirmed": "true", "shops": "店A"})
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.get_data(as_text=True).splitlines()
                  if line.startswith("data: ")]
        assert any(event["type"] == "shop_error" for event in events)
        assert events[-1]["type"] == "done"
        assert events[-1]["status"] == "failed"
        assert events[-1]["failed_shops"] == ["店A"]
    assert len(records) == (0 if capacity else 1)
