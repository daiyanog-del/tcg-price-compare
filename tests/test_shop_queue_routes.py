"""
tests/test_shop_queue_routes.py — 店舗検索の取得口（2026-10-22 作り直し）の経路側の回帰テスト

- 混雑で順番が回ってこなかった店は「混雑で取得できず」とし、手元に残る前回の結果で代わりに埋める
  （キャッシュにも DB にも書き戻さない）
- 店舗へ取りに行く検索（SSE）の同時数の上限を超えたら、すぐ混雑応答（X-Search-Busy）を返す。
  枠は検索が終わったとき・途中で閉じられたとき・一度も読まれずに閉じられたときに必ず返る
- デッキ一括検索の途中で利用者が切断したら、まだ始まっていない取得を実行しない
- 前回の結果は通常の期限（15分）を過ぎても STALE_CACHE_MAX_MINUTES までは残る

Flask test_client と偽の店舗でネットワーク不使用。
"""
import json
import threading
import time
from concurrent.futures import Future
from datetime import datetime, timedelta

import pytest

import scraper
import app as app_module
from shop_search_pool import PoolBusyError, ShopSearchPool


def _row(shop, price=100, name="テストカード"):
    return {"shop": shop, "name": name, "rarity": "レア", "code": "",
            "condition": "-", "price": price, "stock": 1, "sold_out": False,
            "url": "", "image": ""}


def _parse_sse(data: bytes) -> list[dict]:
    return [json.loads(line[6:]) for line in data.decode("utf-8").splitlines()
            if line.startswith("data: ")]


def _age_cache_entry(cache_dir, key, shop, minutes):
    fp = cache_dir / f"{key}.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    data["shops"][shop]["timestamp"] = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "sell")
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR", tmp_path / "buy")
    monkeypatch.setattr(app_module, "_supabase_client", None)
    monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
    monkeypatch.setattr(app_module, "_correct_cardname", lambda name: name)
    monkeypatch.setattr(app_module, "_search_stream_slots", app_module._SearchStreamSlots(2, 2))
    app_module.app.config.update(TESTING=True)
    app_module._reset_rate_limits()
    return app_module.app.test_client()


def _busy_future():
    future = Future()
    future.set_exception(PoolBusyError("混雑"))
    return future


# ── 前回の結果（キャッシュ層） ──

def test_stale_entries_survive_store_and_are_read_only_as_fallback(env):
    scraper.cache_store_shops("テストカード", {"店A": [_row("店A", 120)], "店B": [_row("店B", 90)]})
    key = scraper._cache_key("テストカード")
    _age_cache_entry(scraper.CACHE_DIR, key, "店A", 60)  # 通常の期限切れ・前回の結果としては有効
    _age_cache_entry(scraper.CACHE_DIR, key, "店B", scraper.STALE_CACHE_MAX_MINUTES + 5)
    hit, missing = scraper.cache_get_shops("テストカード", ["店A", "店B"])
    assert hit == {} and missing == ["店A", "店B"]  # 命中の扱いは変えない
    # 別の店の書き込みで、期限切れの店Aは消えない（前回の結果として残す）・古すぎる店Bは間引く
    scraper.cache_store_shops("テストカード", {"店C": [_row("店C", 80)]})
    stale = scraper.cache_get_shops_stale("テストカード", ["店A", "店B", "店C"])
    assert set(stale) == {"店A", "店C"}
    assert stale["店A"]["results"][0]["price"] == 120
    raw = json.loads((scraper.CACHE_DIR / f"{key}.json").read_text(encoding="utf-8"))
    assert set(raw["shops"]) == {"店A", "店C"}


# ── 混雑時の代わりの表示 ──

@pytest.mark.parametrize("route", ["/api/search", "/api/buyback"])
def test_busy_shop_falls_back_to_previous_result_without_writing(env, monkeypatch, route):
    store = scraper.cache_store_shops if route == "/api/search" else scraper.buyback_cache_store_shops
    cache_dir = scraper.CACHE_DIR if route == "/api/search" else scraper.BUYBACK_CACHE_DIR
    key = scraper._cache_key("テストカード") if route == "/api/search" else scraper._buyback_cache_key("テストカード")
    store("テストカード", {"店A": [_row("店A", 300)]})
    _age_cache_entry(cache_dir, key, "店A", 60)
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: []), ("店B", lambda _: [])])
    monkeypatch.setattr(app_module, "BUYBACK_SHOPS", [("店A", lambda _: []), ("店B", lambda _: [])])
    ok = Future()
    ok.set_result(([_row("店B", 200)], 0))
    monkeypatch.setattr(app_module, "_submit_shop_fetch",
                        lambda _req, shop, _fn, _name: _busy_future() if shop == "店A" else ok)
    written = []
    monkeypatch.setattr(app_module, "_persist_scrape_async", lambda *a, **k: written.append(a))
    records = []
    monkeypatch.setattr(app_module, "tracker", type("T", (), {
        "record_success": staticmethod(lambda shop, n: records.append(("ok", shop))),
        "record_failure": staticmethod(lambda shop, e: records.append(("ng", shop)))})())

    resp = env.get(route, query_string=[("q", "テストカード"), ("confirmed", "true"),
                                         ("shops", "店A"), ("shops", "店B")])
    events = _parse_sse(resp.get_data())
    errors = [e for e in events if e["type"] == "shop_error"]
    assert errors == [{"type": "shop_error", "shop": "店A", "error": app_module.SHOP_BUSY_MESSAGE, "busy": True}]
    busy_done = [e for e in events if e["type"] == "shop_done" and e["shop"] == "店A"][0]
    assert busy_done["status"] == "busy" and busy_done["stale"] is True and busy_done["count"] == 1
    done = events[-1]
    assert done["type"] == "done"
    assert done["busy_shops"] == ["店A"]
    assert done["failed_shops"] == ["店A"] and done["successful_shops"] == ["店B"]
    assert sorted(r["price"] for r in done["results"]) == [200, 300]
    # 混雑は店舗障害として数えない。前回の結果を新しい取得として書き戻さない
    assert ("ng", "店A") not in records and ("ok", "店A") not in records
    raw = json.loads((cache_dir / f"{key}.json").read_text(encoding="utf-8"))
    assert datetime.now() - datetime.fromisoformat(raw["shops"]["店A"]["timestamp"]) > timedelta(minutes=50)
    assert written == []


def test_busy_shop_without_previous_result_is_reported(env, monkeypatch):
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: [])])
    monkeypatch.setattr(app_module, "_submit_shop_fetch", lambda *_: _busy_future())
    events = _parse_sse(env.get("/api/search", query_string={"q": "テストカード", "confirmed": "true",
                                                             "shops": "店A"}).get_data())
    busy_done = [e for e in events if e["type"] == "shop_done"][0]
    assert busy_done["stale"] is False and busy_done["count"] == 0
    assert events[-1]["status"] == "failed" and events[-1]["busy_shops"] == ["店A"]


@pytest.mark.parametrize("route", ["/api/deck", "/api/deck-buy"])
def test_deck_busy_shop_uses_previous_result(env, monkeypatch, route):
    is_buy = route == "/api/deck-buy"
    store = scraper.buyback_cache_store_shops if is_buy else scraper.cache_store_shops
    store("テストカード", {"店A": [_row("店A", 50 if not is_buy else 900)]})
    key = scraper._buyback_cache_key("テストカード") if is_buy else scraper._cache_key("テストカード")
    _age_cache_entry(scraper.BUYBACK_CACHE_DIR if is_buy else scraper.CACHE_DIR, key, "店A", 60)
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: []), ("店B", lambda _: [])])
    monkeypatch.setattr(app_module, "BUYBACK_SHOPS", [("店A", lambda _: []), ("店B", lambda _: [])])
    ok = Future()
    ok.set_result(([_row("店B", 200)], 0))
    monkeypatch.setattr(app_module, "_submit_shop_fetch",
                        lambda _req, shop, _fn, _name: _busy_future() if shop == "店A" else ok)
    resp = env.post(route, data={"cards": "テストカード", "shops": ["店A", "店B"]})
    events = _parse_sse(resp.get_data())
    card = [e for e in events if e["type"] == "card_done"][0]
    assert card["busy_shops"] == ["店A"]
    assert card["failed_shops"] == ["店A"]
    assert card["best"]["shop"] == "店A"  # 前回の結果のほうが有利（販売=安い・買取=高い）
    assert events[-1]["type"] == "done"


# ── SSE の同時数の枠 ──

def _slow_pool_fetch(monkeypatch, gate):
    def slow(_name):
        assert gate.wait(5)
        return [_row("店A")]
    monkeypatch.setattr(app_module, "SHOPS", [("店A", slow)])
    pool = ShopSearchPool(2, 2, 50)
    monkeypatch.setattr(app_module, "_shop_search_pool", pool)
    return pool


def test_stream_limit_returns_busy_and_slots_come_back(env, monkeypatch):
    gate = threading.Event()
    pool = _slow_pool_fetch(monkeypatch, gate)
    try:
        streams = []
        for n in range(2):
            app_module._reset_rate_limits()
            resp = env.get("/api/search", query_string={"q": f"カード{n}", "confirmed": "true", "shops": "店A"},
                           buffered=False)
            streams.append(resp)
            next(resp.response)  # 生成を始める（shops イベント）
        app_module._reset_rate_limits()
        busy = env.get("/api/search", query_string={"q": "カード9", "confirmed": "true", "shops": "店A"})
        assert busy.status_code == 200
        assert busy.headers["X-Search-Busy"] == "1"
        assert int(busy.headers["Retry-After"]) == app_module.SEARCH_BUSY_RETRY_SEC
        assert _parse_sse(busy.get_data()) == [{"type": "busy", "retry_after": app_module.SEARCH_BUSY_RETRY_SEC,
                                                "message": "混雑しています。数秒後に自動で再開します"}]
        # デッキも同じ枠を使う
        app_module._reset_rate_limits()
        busy_deck = env.post("/api/deck", data={"cards": "カード8", "shops": ["店A"]})
        assert busy_deck.headers.get("X-Search-Busy") == "1"
        # 1本を最後まで読み、もう1本は途中で閉じる → 2本とも枠が戻る
        gate.set()
        events = _parse_sse(b"".join(streams[0].response))
        assert events[-1]["type"] == "done"
        streams[0].close()
        streams[1].close()
        assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 0}
        # 一度も読まれずに閉じられた応答でも枠は戻る
        app_module._reset_rate_limits()
        unread = env.get("/api/search", query_string={"q": "カード7", "confirmed": "true", "shops": "店A"},
                         buffered=False)
        assert app_module._search_stream_slots.in_use() == {"single": 1, "bulk": 0}
        unread.close()
        assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 0}
    finally:
        gate.set()
        pool.shutdown(wait=True)


def test_cached_search_does_not_use_stream_slot(env, monkeypatch):
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: [])])
    scraper.cache_store_shops("テストカード", {"店A": [_row("店A")]})
    monkeypatch.setattr(app_module, "_search_stream_slots", app_module._SearchStreamSlots(1, 1))
    assert app_module._search_stream_slots.try_open() is not None  # 枠を使い切る
    events = _parse_sse(env.get("/api/search", query_string={"q": "テストカード", "confirmed": "true",
                                                             "shops": "店A"}).get_data())
    assert events[-1]["type"] == "done" and events[-1]["total"] == 1


# ── 切断時の取り消し ──

def test_deck_disconnect_cancels_fetches_not_started(env, monkeypatch):
    started = []
    lock = threading.Lock()
    gate = threading.Event()

    def slow(name):
        with lock:
            started.append(name)
        assert gate.wait(5)
        return [_row("店A", name=name)]

    monkeypatch.setattr(app_module, "SHOPS", [("店A", slow)])
    monkeypatch.setattr(app_module, "SEARCH_HEARTBEAT_SEC", 0.1)
    pool = ShopSearchPool(1, 1, 50)
    monkeypatch.setattr(app_module, "_shop_search_pool", pool)
    try:
        cards = "|".join(f"デッキ{n}" for n in range(12))
        resp = env.post("/api/deck", data={"cards": cards, "shops": ["店A"]}, buffered=False)
        iterator = iter(resp.response)
        # 生成器は読まれた分だけ進む。card_start 12件のあと、順番待ちの合図が来るまで読む
        kinds = []
        while "waiting" not in kinds:
            kinds.append(json.loads(next(iterator).decode()[6:])["type"])
        assert kinds.count("card_start") == 12
        assert len(started) == 1  # 1店1件ずつしか取らない
        assert pool.snapshot()["queued"] >= 1
        resp.close()  # 画面を閉じた
        gate.set()
        time.sleep(0.3)
        snapshot = pool.snapshot()
        assert snapshot["queued"] == 0 and snapshot["running"] == 0
        assert snapshot["cancelled"] >= 1
        assert len(started) == 1  # 取り消した取得は実行されない
        assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 0}
    finally:
        gate.set()
        pool.shutdown(wait=True)


# ── レビュー指摘への対応（2026-10-22 reviewer 中2・低1・低2） ──

def test_stale_rows_are_marked_and_kept_out_of_per_shop(env, monkeypatch):
    """購入候補・新商品の店別価格（per_shop）は購入先の決定に使うので、前回の結果を入れない。
    最安（best）には使うが、行に stale の印と取得時刻が付く"""
    scraper.cache_store_shops("テストカード", {"店A": [_row("店A", 50)]})
    _age_cache_entry(scraper.CACHE_DIR, scraper._cache_key("テストカード"), "店A", 60)
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: []), ("店B", lambda _: [])])
    ok = Future()
    ok.set_result(([_row("店B", 200)], 0))
    monkeypatch.setattr(app_module, "_submit_shop_fetch",
                        lambda _req, shop, _fn, _name: _busy_future() if shop == "店A" else ok)
    events = _parse_sse(env.post("/api/deck", data={"cards": "テストカード", "shops": ["店A", "店B"],
                                                    "include_per_shop": "1"}).get_data())
    card = [e for e in events if e["type"] == "card_done"][0]
    assert card["best"]["shop"] == "店A" and card["best"]["stale"] is True and card["best"]["stale_at"]
    assert set(card["per_shop"]) == {"店B"}


def test_capacity_limit_is_treated_as_busy(env, monkeypatch):
    """受付件数の安全上限（PoolCapacityError）も店の障害ではないので、混雑と同じく前回の結果で埋める"""
    from shop_search_pool import PoolCapacityError
    scraper.cache_store_shops("テストカード", {"店A": [_row("店A", 300)]})
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: [])])
    full = Future()
    full.set_exception(PoolCapacityError("受付上限"))
    monkeypatch.setattr(app_module, "_submit_shop_fetch", lambda *_: full)
    _age_cache_entry(scraper.CACHE_DIR, scraper._cache_key("テストカード"), "店A", 60)  # 命中させず前回の結果として読む
    events = _parse_sse(env.get("/api/search", query_string={"q": "テストカード", "confirmed": "true",
                                                             "shops": "店A"}).get_data())
    assert [e for e in events if e["type"] == "shop_error"][0]["busy"] is True
    assert events[-1]["busy_shops"] == ["店A"] and events[-1]["total"] == 1


def test_stale_rows_do_not_feed_code_inference_or_popularity(env, monkeypatch):
    """前回の結果しか無いときは人気の集計に数えず、型番の補完の材料にも使わない"""
    scraper.cache_store_shops("テストカード", {"店A": [dict(_row("店A", 300), code="ABCD-JP001")]})
    _age_cache_entry(scraper.CACHE_DIR, scraper._cache_key("テストカード"), "店A", 60)
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: [])])
    monkeypatch.setattr(app_module, "_submit_shop_fetch", lambda *_: _busy_future())
    recorded, inferred = [], []
    monkeypatch.setattr(app_module, "_record_search", lambda name: recorded.append(name))
    real_infer = app_module.infer_codes
    monkeypatch.setattr(app_module, "infer_codes", lambda rows: inferred.append(list(rows)) or real_infer(rows))
    events = _parse_sse(env.get("/api/search", query_string={"q": "テストカード", "confirmed": "true",
                                                             "shops": "店A"}).get_data())
    assert events[-1]["total"] == 1  # 表示には出す
    assert recorded == []
    assert all(not any(r.get("stale") for r in rows) for rows in inferred)


def test_single_search_disconnect_while_waiting_cancels_and_returns_slot(env, monkeypatch):
    """単品検索の順番待ち中に画面を閉じたら、始まっていない取得を取り消し、枠も戻る（finally で送らない）"""
    gate = threading.Event()

    def slow(_name):
        assert gate.wait(5)
        return [_row("店A")]

    monkeypatch.setattr(app_module, "SHOPS", [("店A", slow)])
    monkeypatch.setattr(app_module, "SEARCH_HEARTBEAT_SEC", 0.1)
    pool = ShopSearchPool(1, 1, 50)
    monkeypatch.setattr(app_module, "_shop_search_pool", pool)
    try:
        holder = pool.request("single", 30)
        running = holder.submit(slow, "先客", shop="店A", runner=lambda fn, n: (fn(n), 0))
        resp = env.get("/api/search", query_string={"q": "テストカード", "confirmed": "true", "shops": "店A"},
                       buffered=False)
        kinds = []
        iterator = iter(resp.response)
        while "waiting" not in kinds:
            kinds.append(json.loads(next(iterator).decode()[6:])["type"])
        assert pool.snapshot()["queued"] == 1
        resp.close()
        snapshot = pool.snapshot()
        assert snapshot["queued"] == 0 and snapshot["cancelled"] == 1
        assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 0}
        gate.set()
        running.result(3)
    finally:
        gate.set()
        pool.shutdown(wait=True)


# ── 一括検索の枠（2026-10-22 reviewer 中-1） ──

def test_bulk_streams_cannot_take_the_single_only_slots():
    slots = app_module._SearchStreamSlots(3, 2)
    bulk = [slots.try_open("bulk"), slots.try_open("bulk")]
    assert all(bulk)
    assert slots.try_open("bulk") is None  # 一括は2本まで
    single = slots.try_open("single")
    assert single is not None  # 残りの1本は単品検索が使える
    assert slots.try_open("single") is None  # 全体の上限
    single()
    single()  # 2回呼んでも1回分しか戻らない
    assert slots.in_use() == {"single": 0, "bulk": 2}
    assert slots.try_open("bulk") is None
    bulk[0]()
    assert slots.try_open("bulk") is not None
    with pytest.raises(ValueError):
        slots.try_open("urgent")
    with pytest.raises(ValueError):
        app_module._SearchStreamSlots(2, 3)


def test_deck_uses_bulk_slot_and_single_search_still_gets_in(env, monkeypatch):
    """一括検索が一括の枠を使い切っていても、単品検索は入れる。一括の次の1本は混雑応答"""
    monkeypatch.setattr(app_module, "_search_stream_slots", app_module._SearchStreamSlots(2, 1))
    gate = threading.Event()
    pool = _slow_pool_fetch(monkeypatch, gate)
    try:
        deck = env.post("/api/deck", data={"cards": "デッキ1", "shops": ["店A"]}, buffered=False)
        assert deck.headers.get("X-Search-Busy") is None
        assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 1}
        app_module._reset_rate_limits()
        second_deck = env.post("/api/deck", data={"cards": "デッキ2", "shops": ["店A"]})
        assert second_deck.headers.get("X-Search-Busy") == "1"
        app_module._reset_rate_limits()
        single = env.get("/api/search", query_string={"q": "カード", "confirmed": "true", "shops": "店A"},
                         buffered=False)
        assert single.headers.get("X-Search-Busy") is None
        assert app_module._search_stream_slots.in_use() == {"single": 1, "bulk": 1}
        gate.set()
        single.close()
        deck.close()
        assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 0}
    finally:
        gate.set()
        pool.shutdown(wait=True)
