"""shop_request_log（お店ごとの1日の取得回数）のテスト。"""
from datetime import datetime

import pytest

import shop_request_log as srl
from constants import JST


def _ts(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=JST).timestamp()


class _Clock:
    def __init__(self, ts):
        self.ts = ts

    def __call__(self):
        return self.ts


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._op = None

    def upsert(self, rows, on_conflict=None):
        self._op = ("upsert", rows, on_conflict)
        return self

    def delete(self):
        self._op = ("delete",)
        return self

    def lt(self, col, val):
        self._op = self._op + (col, val)
        return self

    def execute(self):
        if self.store.fail:
            raise RuntimeError("DB down")
        self.store.calls.append((self.name,) + self._op)
        return self


class _FakeStore:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def table(self, name):
        return _FakeTable(self, name)


def test_observe_accumulates_deltas_per_day():
    clock = _Clock(_ts(2026, 11, 1, 23, 50))
    c = srl.DailyHostCounter("web", clock=clock, instance_id="i1")
    c.observe({"a.jp": {"requests": 10, "skipped": 1}})
    c.observe({"a.jp": {"requests": 15, "skipped": 1}})
    clock.ts = _ts(2026, 11, 2, 0, 1)
    c.observe({"a.jp": {"requests": 18, "skipped": 3}, "b.jp": {"requests": 2, "skipped": 0}})
    rows = {(r["day"], r["host"]): (r["requests"], r["skipped"]) for r in c.rows()}
    assert rows == {
        ("2026-11-01", "a.jp"): (15, 1),
        ("2026-11-02", "a.jp"): (3, 2),
        ("2026-11-02", "b.jp"): (2, 0),
    }
    assert all(r["source"] == "web" and r["instance_id"] == "i1" for r in c.rows())


def test_counter_reset_counts_current_value():
    """累積が減った（reset_host_request_counts）ら、今の値をそのまま足す（負の差にしない）。"""
    c = srl.DailyHostCounter("prices", clock=_Clock(_ts(2026, 11, 1)), instance_id="i")
    c.observe({"a.jp": {"requests": 10, "skipped": 0}})
    c.observe({"a.jp": {"requests": 4, "skipped": 0}})
    assert [(r["requests"]) for r in c.rows()] == [14]


def test_flush_upserts_and_drops_past_days():
    clock = _Clock(_ts(2026, 11, 1, 23, 59))
    c = srl.DailyHostCounter("web", clock=clock, instance_id="i")
    c.observe({"a.jp": {"requests": 5, "skipped": 0}})
    clock.ts = _ts(2026, 11, 2, 0, 5)
    c.observe({"a.jp": {"requests": 7, "skipped": 0}})
    store = _FakeStore()
    assert c.flush(store) is True
    name, op, rows, conflict = store.calls[0]
    assert (name, op, conflict) == ("shop_request_daily", "upsert", "day,source,instance_id,host")
    assert [(r["day"], r["requests"]) for r in rows] == [("2026-11-01", 5), ("2026-11-02", 2)]
    # 前日は手放し、今日の分だけ持ち続ける（次に書くときも今日の累計を書く）
    assert [r["day"] for r in c.rows()] == ["2026-11-02"]


def test_flush_failure_keeps_rows():
    clock = _Clock(_ts(2026, 11, 1, 23, 59))
    c = srl.DailyHostCounter("web", clock=clock, instance_id="i")
    c.observe({"a.jp": {"requests": 5, "skipped": 0}})
    clock.ts = _ts(2026, 11, 2, 1, 0)
    assert c.flush(_FakeStore(fail=True)) is False
    assert [r["day"] for r in c.rows()] == ["2026-11-01"]


def test_flush_without_store_or_rows_is_noop():
    c = srl.DailyHostCounter("web", clock=_Clock(_ts(2026, 11, 1)), instance_id="i")
    store = _FakeStore()
    assert c.flush(store) is True and store.calls == []
    c.observe({"a.jp": {"requests": 1, "skipped": 0}})
    assert c.flush(None) is True


def test_install_job_writes_at_exit_and_prunes(monkeypatch):
    registered = []
    monkeypatch.setattr(srl.atexit, "register", lambda fn: registered.append(fn))
    store = _FakeStore()
    srl.install_job("prices", store, get_counts=lambda: {"a.jp": {"requests": 3, "skipped": 1}}, prune=True)
    assert len(registered) == 1
    registered[0]()
    ops = [c[1] for c in store.calls]
    assert ops == ["upsert", "delete"]
    assert store.calls[0][2][0]["source"] == "prices"
    assert store.calls[1][2] == "day"


def test_install_job_survives_db_failure(monkeypatch):
    registered = []
    monkeypatch.setattr(srl.atexit, "register", lambda fn: registered.append(fn))
    srl.install_job("buyback", _FakeStore(fail=True), get_counts=lambda: {"a.jp": {"requests": 3, "skipped": 0}},
                    prune=True)
    registered[0]()  # 例外を出さない


@pytest.mark.parametrize("hour,quiet", [(4, False), (5, True), (9, True), (10, False)])
def test_quiet_hours(hour, quiet):
    assert srl._in_quiet_hours(_ts(2026, 11, 1, hour, 30)) is quiet


def _run_web(counter, store, clock, minutes):
    counts = {"n": 0}

    def get_counts():
        counts["n"] += 1
        return {"a.jp": {"requests": counts["n"], "skipped": 0}}

    ticks = {"n": 0}

    class _Stop(Exception):
        pass

    def sleep(sec):
        ticks["n"] += 1
        if ticks["n"] > minutes:
            raise _Stop
        clock.ts += sec

    with pytest.raises(_Stop):
        srl.run_web_forever(counter, store, get_counts=get_counts, sleep=sleep, clock=clock)


def test_run_web_flushes_every_10min_and_hourly_in_quiet_hours():
    # 4:00 から40分: 10分ごとに4回
    clock = _Clock(_ts(2026, 11, 1, 4, 0))
    store = _FakeStore()
    _run_web(srl.DailyHostCounter("web", clock=clock, instance_id="i"), store, clock, 40)
    assert len(store.calls) == 4
    # 5:00 から59分: 朝の収集中は1時間ごとなので0回
    clock = _Clock(_ts(2026, 11, 1, 5, 0))
    store = _FakeStore()
    _run_web(srl.DailyHostCounter("web", clock=clock, instance_id="i"), store, clock, 59)
    assert store.calls == []
    # 9:30 から: 10時を過ぎたら（最後に書いてから10分以上たっていれば）書き始める
    clock = _Clock(_ts(2026, 11, 1, 9, 30))
    store = _FakeStore()
    _run_web(srl.DailyHostCounter("web", clock=clock, instance_id="i"), store, clock, 31)
    assert len(store.calls) == 1


def test_run_web_across_midnight_writes_both_days():
    clock = _Clock(_ts(2026, 11, 1, 23, 55))
    c = srl.DailyHostCounter("web", clock=clock, instance_id="i")
    store = _FakeStore()
    _run_web(c, store, clock, 10)
    days = {r["day"] for r in store.calls[-1][2]}
    assert days == {"2026-11-01", "2026-11-02"}
    assert [r["day"] for r in c.rows()] == ["2026-11-02"]


def test_flush_keeps_past_day_if_it_grew_after_snapshot(monkeypatch):
    """写しを取ってから前日分に差が足されたら、手放さずに次の書き込みに回す（reviewer 低1）。"""
    clock = _Clock(_ts(2026, 11, 1, 23, 59))
    c = srl.DailyHostCounter("web", clock=clock, instance_id="i")
    c.observe({"a.jp": {"requests": 5, "skipped": 0}})

    class _GrowingStore(_FakeStore):
        def table(self, name):
            t = super().table(name)
            orig = t.execute

            def execute():
                # 書き込みの通信中に前日の日付のまま差が足される
                c.observe({"a.jp": {"requests": 6, "skipped": 0}})
                clock.ts = _ts(2026, 11, 2, 0, 1)
                return orig()
            t.execute = execute
            return t

    assert c.flush(_GrowingStore()) is True
    assert [(r["day"], r["requests"]) for r in c.rows()] == [("2026-11-01", 6)]


def test_install_job_prune_cutoff_is_keep_days(monkeypatch):
    registered = []
    monkeypatch.setattr(srl.atexit, "register", lambda fn: registered.append(fn))
    store = _FakeStore()
    srl.install_job("prices", store, get_counts=lambda: {"a.jp": {"requests": 1, "skipped": 0}}, prune=True)
    registered[0]()
    from datetime import timedelta
    expected = (datetime.now(JST) - timedelta(days=srl.KEEP_DAYS)).strftime("%Y-%m-%d")
    assert store.calls[1][1:] == ("delete", "day", expected)


def test_start_web(monkeypatch):
    registered, started = [], []
    monkeypatch.setattr(srl.atexit, "register", lambda fn: registered.append(fn))

    class _T:
        def __init__(self, **kw):
            self.kw = kw

        def start(self):
            started.append(self.kw)

    monkeypatch.setattr(srl.threading, "Thread", _T)
    assert srl.start_web(None) is None and registered == [] and started == []
    counter = srl.start_web(_FakeStore())
    assert counter is not None and counter.source == "web"
    assert len(registered) == 1 and started[0]["name"] == "shop-request-log"


def test_make_store_without_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    assert srl.make_store() is None


def test_jobs_install_with_their_source():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    for f, src in (("collect_prices.py", '"prices"'), ("collect_buyback.py", '"buyback"'),
                   ("collect_featured.py", '"featured"')):
        assert f"shop_request_log.install_job({src}" in (root / f).read_text(encoding="utf-8")


def test_shop_check_counts_requests(monkeypatch):
    """店への接続確認（safe_get を通らない）も数える（reviewer 中1）。"""
    import requests
    import scraper
    import collect_prices

    class _Resp:
        status_code = 200

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    scraper.reset_host_request_counts()
    assert collect_prices.check_shop_availability(["まんぞく屋"], {"まんぞく屋": "https://shopmanzokuya.com/"}) == ["まんぞく屋"]
    assert scraper.get_host_request_counts()["shopmanzokuya.com"]["requests"] == 1
    scraper.reset_host_request_counts()


def test_kanabell_catalog_counts_host(monkeypatch):
    import scraper
    import kanabell_catalog

    class _Res:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": [], "total": {"value": 0}}, "_shards": {}}

    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "https://es.example.test:443")
    monkeypatch.setattr(kanabell_catalog.requests, "post", lambda *a, **k: _Res())
    scraper.reset_host_request_counts()
    try:
        kanabell_catalog._fetch_page(None, 5)
    except Exception:
        pass
    assert scraper.get_host_request_counts()["es.example.test:443"]["requests"] == 1
    scraper.reset_host_request_counts()


def test_kanabell_search_counts_host(monkeypatch):
    import scraper

    class _Res:
        def raise_for_status(self):
            pass

        def json(self):
            return {"hits": {"hits": []}}

    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "https://es.example.test:443")
    monkeypatch.setattr(scraper.requests, "post", lambda *a, **k: _Res())
    scraper.reset_host_request_counts()
    scraper._kanabell_search({"match_all": {}}, 1, [], 5)
    assert scraper.get_host_request_counts()["es.example.test:443"]["requests"] == 1
    scraper.reset_host_request_counts()
