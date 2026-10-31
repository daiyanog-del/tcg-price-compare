"""価格の検索で、店へ取りに行く前に前回の結果を先に送る（preview）の確認（2026-11-01 案A）。

取りに行く店すべてに前回の結果（24時間以内・partial でない）があるときだけ送る。
preview は表示用で、キャッシュにも DB にも書かない。done は今までどおり最新の結果。
"""
import time
from concurrent.futures import Future
from datetime import datetime, timedelta

import pytest

import scraper
import app as app_module
from tests.test_shop_queue_routes import _age_cache_entry, _parse_sse, _row


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "sell")
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR", tmp_path / "buy")
    monkeypatch.setattr(app_module, "_supabase_client", None)
    monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
    monkeypatch.setattr(app_module, "_correct_cardname", lambda name: name)
    monkeypatch.setattr(app_module, "_search_stream_slots", app_module._SearchStreamSlots(2, 2))
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda _: []), ("店B", lambda _: [])])
    app_module.app.config.update(TESTING=True)
    app_module._reset_rate_limits()
    return app_module.app.test_client()


def _fetch(results_by_shop, errors_by_shop=None):
    """店ごとの取得結果を返す _submit_shop_fetch の代わり"""
    def submit(_req, shop, _fn, _name):
        f = Future()
        f.set_result((results_by_shop.get(shop, []), (errors_by_shop or {}).get(shop, 0)))
        return f
    return submit


def _search(env):
    return _parse_sse(env.get("/api/search", query_string=[
        ("q", "テストカード"), ("confirmed", "true"), ("shops", "店A"), ("shops", "店B")]).get_data())


def _store_old(shops_rows, minutes, partial_shops=frozenset()):
    scraper.cache_store_shops("テストカード", shops_rows, partial_shops)
    for shop in shops_rows:
        _age_cache_entry(scraper.CACHE_DIR, scraper._cache_key("テストカード"), shop, minutes)


def test_preview_is_sent_before_fetch_results_and_done_is_latest(env, monkeypatch):
    _store_old({"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]}, 90)
    monkeypatch.setattr(app_module, "_submit_shop_fetch",
                        _fetch({"店A": [_row("店A", 280)], "店B": [_row("店B", 260)]}))
    events = _search(env)
    types = [e["type"] for e in events]
    assert "preview" in types
    i_preview = types.index("preview")
    first_fetched = min(i for i, e in enumerate(events) if e["type"] == "shop_done" and not e.get("cached"))
    assert i_preview < first_fetched  # 店の結果より先に届く
    preview = events[i_preview]
    assert sorted(r["price"] for r in preview["results"]) == [250, 300]
    assert all(r["stale"] is True and r["stale_at"] for r in preview["results"])
    assert 89 <= preview["age_minutes"] <= 91
    assert preview["total"] == 2
    done = events[-1]
    assert done["type"] == "done" and sorted(r["price"] for r in done["results"]) == [260, 280]
    assert not any(r.get("stale") for r in done["results"])


def test_preview_counts_age_from_oldest_shop(env, monkeypatch):
    _store_old({"店A": [_row("店A", 300)]}, 30)
    _store_old({"店B": [_row("店B", 250)]}, 200)
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({}))
    preview = [e for e in _search(env) if e["type"] == "preview"][0]
    assert 199 <= preview["age_minutes"] <= 201


def test_no_preview_when_one_shop_has_no_previous_result(env, monkeypatch):
    _store_old({"店A": [_row("店A", 300)]}, 60)
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({"店A": [_row("店A", 280)]}))
    assert not any(e["type"] == "preview" for e in _search(env))


def test_no_preview_from_partial_deck_results(env, monkeypatch):
    _store_old({"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]}, 60, partial_shops={"店B"})
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({}))
    assert not any(e["type"] == "preview" for e in _search(env))


def test_no_preview_when_older_than_stale_window(env, monkeypatch):
    _store_old({"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]}, scraper.STALE_CACHE_MAX_MINUTES + 5)
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({}))
    assert not any(e["type"] == "preview" for e in _search(env))


def test_preview_includes_fresh_cache_hits_of_other_shops(env, monkeypatch):
    """命中した店（15分以内）は今の行のまま、取りに行く店だけ前回の結果で埋める"""
    _store_old({"店B": [_row("店B", 250)]}, 60)
    scraper.cache_store_shops("テストカード", {"店A": [_row("店A", 300)]})  # 命中
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({"店B": [_row("店B", 240)]}))
    preview = [e for e in _search(env) if e["type"] == "preview"][0]
    by_shop = {r["shop"]: r for r in preview["results"]}
    assert by_shop["店A"].get("stale") is None and by_shop["店B"]["stale"] is True


def test_reading_previous_result_failure_does_not_break_search(env, monkeypatch):
    def broken(*_a, **_k):
        raise RuntimeError("KV 不調")
    monkeypatch.setattr(app_module, "cache_get_shops_stale", broken)
    monkeypatch.setattr(app_module, "_submit_shop_fetch",
                        _fetch({"店A": [_row("店A", 280)], "店B": [_row("店B", 260)]}))
    events = _search(env)
    assert not any(e["type"] == "preview" for e in events)
    assert events[-1]["type"] == "done" and events[-1]["total"] == 2


def _watch_db_writes(monkeypatch):
    """DB への書き込みの経路を有効にして（偽のクライアント）、呼ばれた分を記録する"""
    monkeypatch.setattr(app_module, "_supabase_client", object())
    calls = []
    monkeypatch.setattr(app_module, "_persist_scrape_async", lambda name, rows: calls.append(("persist", name, rows)))
    monkeypatch.setattr(app_module, "_track_card_async", lambda name: calls.append(("track", name)))
    monkeypatch.setattr(app_module, "_save_search_to_supabase", lambda name: calls.append(("search_log", name)))
    return calls


def test_preview_is_not_written_back(env, monkeypatch):
    """最新が取れなかった店の前回の結果は、先に見せても新しい取得として書き戻さない・DB にも送らない"""
    _store_old({"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]}, 60)
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({}, errors_by_shop={"店A": 1, "店B": 1}))
    calls = _watch_db_writes(monkeypatch)
    events = _search(env)
    assert any(e["type"] == "preview" for e in events)
    stale = scraper.cache_get_shops_stale("テストカード", ["店A", "店B"])
    for shop in ("店A", "店B"):
        assert datetime.now() - datetime.fromisoformat(stale[shop]["timestamp"]) > timedelta(minutes=50)
    time.sleep(0.2)  # 書き込みは裏のスレッドで始まる
    assert calls == []
    # 全店が取れずに0件で終わる（画面は先出しの価格を残し「最新が取れなかった」と出す・index-main.js）
    done = events[-1]
    assert done["type"] == "done" and done["total"] == 0 and set(done["failed_shops"]) == {"店A", "店B"}


def test_only_fresh_rows_are_written_when_preview_was_sent(env, monkeypatch):
    """先出しのあとに取れた最新の行だけが DB へ送られ、前回の結果（stale）は混ざらない"""
    _store_old({"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]}, 60)
    monkeypatch.setattr(app_module, "_submit_shop_fetch",
                        _fetch({"店A": [_row("店A", 280)]}, errors_by_shop={"店B": 1}))
    calls = _watch_db_writes(monkeypatch)
    events = _search(env)
    assert any(e["type"] == "preview" for e in events)
    time.sleep(0.2)
    persisted = [rows for kind, *rest in calls if kind == "persist" for rows in [rest[1]]]
    assert len(persisted) == 1
    assert [r["price"] for r in persisted[0]] == [280] and not any(r.get("stale") for r in persisted[0])


def test_no_preview_when_all_shops_hit_cache(env, monkeypatch):
    """全店がキャッシュに命中する検索は今までどおり（先出しは送らない・店へも取りに行かない）"""
    scraper.cache_store_shops("テストカード", {"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]})
    monkeypatch.setattr(app_module, "_submit_shop_fetch", lambda *a: pytest.fail("店へ取りに行った"))
    events = _search(env)
    assert not any(e["type"] == "preview" for e in events)
    assert events[-1]["type"] == "done" and events[-1]["total"] == 2


def test_broken_preview_rows_are_skipped_and_search_continues(env, monkeypatch):
    """先出しの組み立てが失敗しても（古い形の行など）検索は最後まで進み done を返す"""
    _store_old({"店A": [_row("店A", 300)], "店B": [_row("店B", 250)]}, 60)
    real_build = app_module._build_done
    def build(results, *a, **k):
        if any(r.get("stale") for r in results):
            raise KeyError("price")
        return real_build(results, *a, **k)
    monkeypatch.setattr(app_module, "_build_done", build)
    monkeypatch.setattr(app_module, "_submit_shop_fetch", _fetch({"店A": [_row("店A", 280)], "店B": [_row("店B", 260)]}))
    events = _search(env)
    assert not any(e["type"] == "preview" for e in events)
    assert events[-1]["type"] == "done" and events[-1]["total"] == 2
