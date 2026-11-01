"""有料化の合図の見張り（upgrade_signals.py）。Discord には送らない（送信関数を差し替えて中身を確かめる）。"""
from datetime import datetime
import threading
from types import SimpleNamespace

import httpx
import pytest

import upgrade_signals as us
from constants import JST
from shop_search_pool import PRIORITY_BULK, PRIORITY_SINGLE, ShopSearchPool
from tests.fake_supabase import FakeSB


def ts(*args):
    return datetime(*args, tzinfo=JST).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def _no_real_discord(monkeypatch):
    """万一、差し替え忘れの経路があっても本番の Discord に送らない。"""
    def fail(*_a, **_k):
        raise AssertionError("テストから本物の send_discord_message を呼んだ")
    monkeypatch.setattr(us.discord_notify, "send_discord_message", fail)
    monkeypatch.setattr(us.monitor, "_send", fail)


def make(t=None, instance_id="p1"):
    sent = []
    clock = Clock(t or ts(2026, 10, 22, 12, 0))
    mon = us.SignalMonitor(send=sent.append, clock=clock, instance_id=instance_id)
    return mon, clock, sent


def pool_snap(instance="a", rejected=0, expired=0):
    return {"instance_id": instance, "capacity_rejected": rejected,
            "by_priority": {"single": {"busy_expired": expired}}}


# ── すぐ知らせる通知 ──

def test_db_failures_in_a_row_alert_once_per_hour():
    mon, clock, sent = make()
    for _ in range(3):
        mon.record_db_response(0.1, True)
    mon.tick()
    assert len(sent) == 1 and "DB が応答しません" in sent[0]
    # 60分以内は同じ知らせを送らない
    for _ in range(5):
        mon.record_db_response(0.1, True)
    clock.t += 30 * 60
    mon.tick()
    assert len(sent) == 1
    clock.t += 31 * 60
    mon.record_db_response(0.1, True)
    mon.tick()
    assert len(sent) == 2


def test_success_between_failures_resets_streak():
    mon, _clock, sent = make()
    for _ in range(5):
        mon.record_db_response(0.1, True)
        mon.record_db_response(0.1, True)
        mon.record_db_response(0.2, False)
    mon.tick()
    assert sent == []
    assert mon.snapshot()["db_failures"] == 10


def test_requests_failing_together_on_one_broken_connection_count_once(monkeypatch):
    """1本の接続が切られて、相乗りしていた要求が同じ瞬間に落ちても「続いた」は1回（2026-10-30 誤報）。"""
    mono = {"t": 1000.0}
    monkeypatch.setattr(us.time, "monotonic", lambda: mono["t"])
    mon, _clock, sent = make()
    # 3つとも 999 秒台に始めて、1000 秒の切断でそろって落ちる
    for started in (999.0, 999.5, 999.9):
        mon.record_db_response(1000.0 - started, True, started_at=started)
    mon.tick()
    assert sent == []
    assert mon.snapshot()["db_failures"] == 3  # 件数はすべて数える
    assert mon.snapshot()["db_fail_streak"] == 1


def test_failures_of_requests_started_after_a_failure_still_alert(monkeypatch):
    """本当に止まったときは、失敗のあとに始めた要求も落ち続けるので鳴る（10-22 朝の形）。"""
    mono = {"t": 1000.0}
    monkeypatch.setattr(us.time, "monotonic", lambda: mono["t"])
    mon, _clock, sent = make()
    for _ in range(3):
        started = mono["t"]
        mono["t"] += 10  # 10秒待って時間切れ
        mon.record_db_response(10, True, started_at=started)
        mon.record_db_response(10, True, started_at=started - 5)  # 同じ回に落ちた相乗りの要求
    mon.tick()
    assert len(sent) == 1 and "DB が応答しません" in sent[0]


def test_timed_transport_passes_start_time():
    seen = []

    class Boom(httpx.BaseTransport):
        def handle_request(self, request):
            raise httpx.WriteError("Connection reset by peer")

    t = us._TimedTransport(Boom(), lambda elapsed, failed, started_at=None: seen.append((failed, started_at)))
    with pytest.raises(httpx.WriteError):
        t.handle_request(httpx.Request("GET", "https://example.invalid/rest/v1/t"))
    assert seen and seen[0][0] is True and isinstance(seen[0][1], float)


def test_slow_db_must_continue_for_several_minutes():
    mon, clock, sent = make()
    for minute in range(5):
        if minute in (1, 3):
            mon.record_db_response(us.DB_SLOW_SEC + 1, False)
        clock.t += 60
        mon.tick()
    assert sent == []  # 5分のうち2分だけ
    mon.record_db_response(us.DB_SLOW_SEC, False)
    clock.t += 60
    mon.tick()
    assert len(sent) == 1 and "DB の応答が遅い" in sent[0]


def test_pool_busy_from_snapshot_and_sse_deltas():
    mon, clock, sent = make()
    mon.tick(pool_snapshot=pool_snap(rejected=4, expired=7), search_busy_total=20)  # 最初は基準だけ
    assert sent == [] and mon.snapshot()["pool_single_busy_expired"] == 0
    for i, (rejected, expired, sse) in enumerate([(4, 8, 20), (4, 8, 25), (5, 8, 25)]):
        clock.t += 60
        mon.tick(pool_snapshot=pool_snap(rejected=rejected, expired=expired), search_busy_total=sse)
    assert len(sent) == 1 and "店舗検索が混んでいます" in sent[0]
    snap = mon.snapshot()
    assert (snap["pool_capacity_rejected"], snap["pool_single_busy_expired"], snap["search_busy"]) == (1, 1, 5)
    assert snap["wait_hist"][-1] == 1  # 待ち切れは上限超えの箱


def test_pool_restart_does_not_count_as_delta():
    mon, clock, sent = make()
    mon.tick(pool_snapshot=pool_snap("a", expired=50), search_busy_total=100)
    clock.t += 60
    mon.tick(pool_snapshot=pool_snap("b", expired=1), search_busy_total=2)  # 別の取得口・数え直し
    assert mon.snapshot()["pool_single_busy_expired"] == 0
    assert mon.snapshot()["search_busy"] == 0
    assert sent == []


def test_tick_failures_alert_once():
    mon, _clock, sent = make()
    for _ in range(us.TICK_FAIL_ALERT - 1):
        mon.note_tick_failed(RuntimeError("x"))
    assert sent == []
    mon.note_tick_failed(RuntimeError("x"))
    mon.note_tick_failed(RuntimeError("x"))
    assert len(sent) == 1 and "見張り" in sent[0]


# ── 1日の集計 ──

def test_wait_only_single_and_p95_bucket():
    stats = us.DailyStats("2026-10-22")
    mon, _clock, _sent = make()
    mon.record_wait(PRIORITY_BULK, 100)
    for _ in range(94):
        mon.record_wait(PRIORITY_SINGLE, 0.3)
    for _ in range(6):
        mon.record_wait(PRIORITY_SINGLE, 12)
    snap = mon.snapshot()
    assert sum(snap["wait_hist"]) == 100
    stats = us.DailyStats.merge("2026-10-22", [snap])
    assert stats.wait_p95_bucket() == (10.0, 15.0)
    assert any("p95" in h for h in us.evaluate_signals(stats))


def test_p95_at_exactly_ten_seconds_is_not_over():
    stats = us.DailyStats("d")
    for _ in range(100):
        stats.add_wait(10.0)
    assert stats.wait_p95_bucket() == (7.0, 10.0)
    assert us.evaluate_signals(stats) == []


def test_memory_and_db_signals():
    stats = us.DailyStats.merge("d", [
        {"memory_peak_mb": 380, "memory_source": "cgroup v2", "db_failures": 4},
        {"memory_peak_mb": 420, "memory_source": "cgroup v2", "db_failures": 6, "db_slow": 1},
    ])
    hits = us.evaluate_signals(stats)
    assert len(hits) == 2
    assert "420MB" in hits[0] and "Render Standard" in hits[0]
    assert "10件" in hits[1] and "Supabase Pro" in hits[1]
    text = us.format_summary(stats, hits)
    assert text.startswith("💰 **有料化を考える時期です**（d）")
    assert "月 $25" in text and "月 +$18" in text


def test_flush_skips_quiet_hours_and_summary_sent_once():
    db = FakeSB(pks={})
    mon, clock, sent = make(ts(2026, 10, 22, 4, 0))
    mon.record_db_response(us.DB_SLOW_SEC, False)
    clock.t = ts(2026, 10, 22, 6, 0)  # 30分以上たったが 05〜10時は書かない
    mon.tick(store=db)
    assert db.tables.get(us.DAILY_TABLE, []) == []
    clock.t = ts(2026, 10, 22, 10, 0)
    mon.tick(store=db)
    rows = db.tables[us.DAILY_TABLE]
    assert len(rows) == 1 and rows[0]["stats"]["db_slow"] == 1
    # 別プロセスの行（同じ日）。合図を超える
    db.tables[us.DAILY_TABLE].append({"day": "2026-10-22", "instance_id": "p0",
                                      "stats": {"memory_peak_mb": 450, "memory_source": "cgroup v2"}})
    for _ in range(3):
        mon.record_wait(PRIORITY_SINGLE, 1)
    # 日付が変わる → 前日分を書いてから切り替え。0:10 まではまとめない
    clock.t = ts(2026, 10, 23, 0, 1)
    mon.tick(store=db)
    assert sent == []
    mine = [r for r in db.tables[us.DAILY_TABLE] if r["instance_id"] == "p1"]
    assert sum(mine[0]["stats"]["wait_hist"]) == 3
    clock.t = ts(2026, 10, 23, 0, 11)
    mon.tick(store=db)
    assert len(sent) == 1 and "450MB" in sent[0] and "2026-10-22" in sent[0]
    marks = {r["day"]: r["signals"] for r in db.tables[us.SUMMARY_TABLE]}
    assert marks == {"2026-10-21": 0, "2026-10-22": 1}  # 10-21 は 10:00 の周で見た（行なし＝合図なし）
    # 同じ周でもう一度・別プロセスが見ても二重に送らない
    clock.t += 60
    mon.tick(store=db)
    other, other_clock, other_sent = make(ts(2026, 10, 23, 0, 30), instance_id="p2")
    other.tick(store=db)
    assert len(sent) == 1 and other_sent == []


def test_quiet_day_is_marked_but_not_sent():
    db = FakeSB(pks={})
    mon, clock, sent = make(ts(2026, 10, 22, 23, 0))
    clock.t = ts(2026, 10, 23, 0, 20)
    mon.tick(store=db)
    assert sent == []
    assert db.tables[us.SUMMARY_TABLE][0]["signals"] == 0


def test_store_failure_keeps_counting():
    class Broken:
        def table(self, _name):
            raise RuntimeError("DB down")
    mon, clock, sent = make(ts(2026, 10, 22, 12, 0))
    mon.record_db_response(0.1, True)
    clock.t += 31 * 60
    mon.tick(store=Broken())  # 例外は外に出さない
    assert mon.snapshot()["db_failures"] == 1


def test_old_rows_are_deleted():
    db = FakeSB(tables={us.DAILY_TABLE: [{"day": "2026-01-01", "instance_id": "x", "stats": {}}],
                        us.SUMMARY_TABLE: [{"day": "2026-01-01", "signals": 0}]}, pks={})
    mon, _clock, _sent = make(ts(2026, 10, 23, 12, 0))
    mon.tick(store=db)
    assert all(r["day"] != "2026-01-01" for r in db.tables[us.DAILY_TABLE])
    assert [r["day"] for r in db.tables[us.SUMMARY_TABLE]] == ["2026-10-22"]


# ── DB の応答の差し込み ──

def _client_with(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://x.supabase.co/rest/v1")


def test_timed_transport_classifies():
    seen = []
    status = {"code": 200}

    def handler(request):
        if status["code"] == 0:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(status["code"], json={})

    client = _client_with(handler)
    client._transport = us._TimedTransport(client._transport, lambda e, f, started_at=None: seen.append(f))
    for code in (200, 404, 500, 503):
        status["code"] = code
        client.get("/t")
    status["code"] = 0
    with pytest.raises(httpx.ReadTimeout):
        client.get("/t")
    assert seen == [False, False, True, True, True]


def test_install_db_watch_on_real_supabase_client():
    from supabase import create_client
    client = create_client("https://example.supabase.co", "x" * 40)
    seen = []
    assert us.install_db_watch(client, lambda e, f, started_at=None: seen.append(f))
    first = client.postgrest.session._transport
    assert isinstance(first, us._TimedTransport)
    assert us.install_db_watch(client, lambda e, f, started_at=None: seen.append(f))  # 二度目は包み直さない
    assert client.postgrest.session._transport is first
    assert us.install_db_watch(None, lambda e, f: None) is False


def test_timed_transport_counts_only_rest_requests():
    """storage など DB 以外の要求は数えない（2026-11-01 supabase_conn で postgrest と storage が同じクライアントを共用）。"""
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})),
                          base_url="https://x.supabase.co")
    client._transport = us._TimedTransport(client._transport, lambda e, f, started_at=None: seen.append(f))
    client.post("/storage/v1/object/official-card-images/a.png")
    assert seen == []
    client.get("/rest/v1/t")
    assert seen == [True]


def test_install_db_watch_on_shared_client():
    from supabase_conn import create_shared_client
    client = create_shared_client("https://example.supabase.co", "x" * 40)
    assert us.install_db_watch(client, lambda e, f, started_at=None: None)
    assert isinstance(client.postgrest.session._transport, us._TimedTransport)


def test_broken_recorder_does_not_fail_request():
    client = _client_with(lambda r: httpx.Response(200, json={}))

    def boom(_e, _f):
        raise RuntimeError("bug")
    client._transport = us._TimedTransport(client._transport, boom)
    assert client.get("/t").status_code == 200


# ── メモリ ──

def test_read_memory_v2_v1_and_rss(tmp_path):
    v2 = tmp_path / "a" / "sys/fs/cgroup"
    v2.mkdir(parents=True)
    (v2 / "memory.current").write_text(str(300 * 1024 * 1024))
    (v2 / "memory.stat").write_text("anon 1\ninactive_file %d\n" % (50 * 1024 * 1024))
    assert us.read_memory_mb(str(tmp_path / "a")) == (250.0, "cgroup v2")

    v1 = tmp_path / "b" / "sys/fs/cgroup/memory"
    v1.mkdir(parents=True)
    (v1 / "memory.usage_in_bytes").write_text(str(200 * 1024 * 1024))
    (v1 / "memory.stat").write_text("total_inactive_file 0\n")
    assert us.read_memory_mb(str(tmp_path / "b")) == (200.0, "cgroup v1")

    proc = tmp_path / "c" / "proc/self"
    proc.mkdir(parents=True)
    (proc / "status").write_text("Name: x\nVmRSS:\t  102400 kB\n")
    assert us.read_memory_mb(str(tmp_path / "c")) == (100.0, "VmRSS")
    assert us.read_memory_mb(str(tmp_path / "none")) == (None, "")


def test_memory_peak_is_kept():
    mon, clock, _sent = make()
    for mb in (230, 410, 260):
        clock.t += 60
        mon.tick(memory=(mb, "cgroup v2"))
    assert mon.snapshot()["memory_peak_mb"] == 410


# ── 取得口からの受け渡し ──

def test_pool_passes_wait_to_observer_and_survives_errors():
    seen = []

    def observer(priority, waited):
        seen.append(priority)
        raise RuntimeError("bug in observer")

    with ShopSearchPool(2, 2, 10, wait_observer=observer) as pool:
        with pool.request(PRIORITY_SINGLE, 5) as req:
            fut = req.submit(lambda name: name.upper(), "abc", shop="s", runner=lambda fn, name: fn(name))
            assert fut.result(timeout=5) == "ABC"
    assert seen == [PRIORITY_SINGLE]


def test_app_import_does_not_start_monitor_thread():
    import app  # noqa: F401  DISABLE_STARTUP_JOBS=1（conftest）
    assert all(t.name != "upgrade-signals" for t in threading.enumerate())
    assert app._shop_search_pool._wait_observer == us.monitor.record_wait


# ── レビュー指摘（2026-10-22 reviewer）への対応 ──

class FlakyStore:
    """fail が True のあいだ書き込み・読み込みが失敗する FakeSB。呼ばれた回数も数える。"""

    def __init__(self):
        self.db = FakeSB(pks={})
        self.fail = False
        self.calls = 0

    def table(self, name):
        self.calls += 1
        if self.fail:
            raise RuntimeError("DB down")
        return self.db.table(name)


def test_day_rolled_while_db_down_still_reports_from_memory():
    """高1: 日付が変わる瞬間に書けなくても、前日分を手元に残し、まとめで合図を出す。"""
    store = FlakyStore()
    mon, clock, sent = make(ts(2026, 10, 22, 23, 50))
    for _ in range(20):
        mon.record_db_response(0.1, True)
    store.fail = True
    clock.t = ts(2026, 10, 23, 0, 0)
    mon.tick(store=store)
    store.fail = False
    clock.t = ts(2026, 10, 23, 0, 11)
    mon.tick(store=store)
    assert any("DB のタイムアウト・応答なしが 20件" in m for m in sent)
    marks = {r["day"]: r["signals"] for r in store.db.tables[us.SUMMARY_TABLE]}
    assert marks["2026-10-22"] == 1


def test_pending_day_is_written_later_and_not_double_counted():
    store = FlakyStore()
    mon, clock, sent = make(ts(2026, 10, 22, 23, 0))
    clock.t = ts(2026, 10, 22, 23, 3)
    mon.tick(store=store)  # 最初の書き込み（起動2分後）
    mon.record_db_response(0.1, True)
    store.fail = True
    clock.t = ts(2026, 10, 23, 0, 0)
    mon.tick(store=store)
    store.fail = False
    clock.t = ts(2026, 10, 23, 0, 30)  # 書き直しの間隔（5分）を過ぎた
    mon.tick(store=store)
    rows = [r for r in store.db.tables[us.DAILY_TABLE] if r["day"] == "2026-10-22"]
    assert len(rows) == 1 and rows[0]["stats"]["db_failures"] == 1


def test_flush_failure_backs_off():
    """中2: 書き込みが失敗し続けても毎分は要求しない。"""
    store = FlakyStore()
    store.fail = True
    mon, clock, _sent = make(ts(2026, 10, 22, 12, 0))
    for _ in range(40):
        clock.t += 60
        mon.tick(store=store)
    # 書き込み: 2分後・7分後・17分後・37分後。まとめの読み込み: 最初の周と30分後
    assert store.calls <= 8


def test_first_flush_soon_after_start_and_memory_cross_writes_now():
    """高2: 起動2分後に1行書く。メモリが合図を超えたらすぐ知らせて書く。"""
    db = FakeSB(pks={})
    mon, clock, sent = make(ts(2026, 10, 22, 12, 0))
    clock.t += 60
    mon.tick(memory=(300, "cgroup v2"), store=db)
    assert [r for r in db.tables.get(us.DAILY_TABLE, []) if r["day"] == "2026-10-22"] == []
    clock.t += 60
    mon.tick(memory=(310, "cgroup v2"), store=db)
    assert db.tables[us.DAILY_TABLE][0]["stats"]["memory_peak_mb"] == 310
    clock.t += 60
    mon.tick(memory=(430, "cgroup v2"), store=db)
    assert db.tables[us.DAILY_TABLE][0]["stats"]["memory_peak_mb"] == 430
    assert len(sent) == 1 and "メモリが合図" in sent[0] and "430MB" in sent[0]
    clock.t += 60
    mon.tick(memory=(450, "cgroup v2"), store=db)  # 既に超えているので二度目は送らない
    assert len(sent) == 1


def test_flush_now_skips_quiet_hours():
    db = FakeSB(pks={})
    mon, clock, _sent = make(ts(2026, 10, 22, 6, 0))
    mon.flush_now(db)
    assert db.tables.get(us.DAILY_TABLE, []) == []
    clock.t = ts(2026, 10, 22, 11, 0)
    mon.flush_now(db)
    assert len(db.tables[us.DAILY_TABLE]) == 1


def test_p95_needs_enough_samples():
    """中4: 件数が少ない日は1件の遅れで合図にしない。"""
    stats = us.DailyStats("d")
    stats.add_wait(12)
    stats.add_wait(0.2)
    stats.add_wait(0.2)
    assert stats.wait_p95_bucket() == (10.0, 15.0)
    assert us.evaluate_signals(stats) == []


def test_broken_row_is_skipped_whole():
    """低4: 読めない行は丸ごと飛ばし、ほかの行は足す。"""
    stats = us.DailyStats.merge("d", [{"db_failures": "x", "db_slow": 5}, {"db_failures": 2}])
    assert (stats.db_failures, stats.db_slow) == (2, 0)


def test_remote_protocol_error_counts_as_failure():
    """中1: http2 の切断（RemoteProtocolError）も DB の応答なしに数える。"""
    seen = []

    def handler(request):
        raise httpx.RemoteProtocolError("Server disconnected", request=request)
    client = _client_with(handler)
    client._transport = us._TimedTransport(client._transport, lambda e, f, started_at=None: seen.append(f))
    with pytest.raises(httpx.RemoteProtocolError):
        client.get("/t")
    assert seen == [True]


def test_install_wraps_proxy_mounts():
    from supabase import create_client
    client = create_client("https://example.supabase.co", "x" * 40)
    session = client.postgrest.session
    session._mounts = {httpx._utils.URLPattern("https://"): httpx.HTTPTransport()}
    assert us.install_db_watch(client, lambda e, f: None)
    assert all(isinstance(t, us._TimedTransport) for t in session._mounts.values())


def test_lazy_clients_are_resolved_each_time():
    """中5: 遅れて作られる接続は、作られたあとの周で差し込む。"""
    from supabase import create_client
    holder = {"c": None}
    us.install_all([lambda: holder["c"], None], set())  # まだ無い・None は飛ばす
    holder["c"] = create_client("https://example.supabase.co", "x" * 40)
    us.install_all([lambda: holder["c"]], set())
    assert isinstance(holder["c"].postgrest.session._transport, us._TimedTransport)


def test_promoted_bulk_fetch_measures_single_wait_from_join():
    """中3: 一括で受け付けた取得に単品が相乗りしたら、単品が並んだ時刻から測る。"""
    seen = []
    now = {"t": 100.0}
    gate = threading.Event()

    def runner(fn, name):
        return fn(name)

    def blocking(name):
        gate.wait(5)
        return name

    def fetch(name):
        return name

    with ShopSearchPool(1, 1, 10, clock=lambda: now["t"],
                        wait_observer=lambda p, w: seen.append((p, w))) as pool:
        with pool.request(PRIORITY_BULK, 300) as bulk_a, pool.request(PRIORITY_BULK, 300) as bulk_b,                 pool.request(PRIORITY_SINGLE, 300) as single:
            blocker = bulk_a.submit(blocking, "block", shop="s", runner=runner)
            queued = bulk_b.submit(fetch, "card", shop="s", runner=runner)  # t=100 に一括で並ぶ
            now["t"] = 160.0
            shared = single.submit(fetch, "card", shop="s", runner=runner)  # t=160 に単品が相乗り
            now["t"] = 170.0
            gate.set()
            assert blocker.result(timeout=5) == "block"
            assert queued.result(timeout=5) == shared.result(timeout=5) == "card"
            snap = pool.snapshot()
    assert snap["shared"] == 1
    assert seen == [(PRIORITY_BULK, 0.0), (PRIORITY_SINGLE, 10.0)]  # 70秒ではなく10秒


def test_search_stream_slots_busy_total_keeps_counting():
    import app
    slots = app._SearchStreamSlots(2, 1, log_interval=3600)
    assert slots.note_busy() == 1  # 最初は出す
    assert slots.note_busy() is None  # 間隔内はまとめる（ログ用の数は戻すが累計は残す）
    assert slots.busy_total() == 2


def test_store_client_has_short_timeout_and_is_separate():
    c = us.make_store_client("https://example.supabase.co", "x" * 40)
    assert c.postgrest.session.timeout.read == us.STORE_TIMEOUT_SEC
    assert us.make_store_client(None, "k") is None


# ── DB の大きさの合図（2026-10-31） ──

class SizedStore:
    """FakeSB に db_size_bytes の呼び出しを足したもの。size_bytes が None なら関数が無いときと同じく失敗する。"""

    def __init__(self, size_bytes):
        self.db = FakeSB(pks={})
        self.tables = self.db.tables
        self.size_bytes = size_bytes
        self.rpc_calls = []

    def table(self, name):
        return self.db.table(name)

    def rpc(self, name, params=None):
        self.rpc_calls.append(name)
        if self.size_bytes is None:
            raise RuntimeError("Could not find the function public.db_size_bytes")
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=self.size_bytes))


MB = 1024 * 1024


def test_db_size_signal_threshold():
    stats = us.DailyStats("d")
    assert us.evaluate_signals(stats, 450.0) == []  # ちょうど合図は超えていない
    hits = us.evaluate_signals(stats, 460.4)
    assert len(hits) == 1 and "460MB" in hits[0] and "500MB" in hits[0] and "url/code" in hits[0]
    assert us.evaluate_signals(stats, None) == []


def test_summary_sends_db_size_once_a_day():
    store = SizedStore(470 * MB)
    mon, clock, sent = make(ts(2026, 10, 22, 23, 0))
    clock.t = ts(2026, 10, 23, 0, 20)
    mon.tick(store=store)
    assert len(sent) == 1 and "DB の大きさが合図" in sent[0] and "DB の大きさ 470MB" in sent[0]
    clock.t += 60
    mon.tick(store=store)
    assert store.rpc_calls == ["db_size_bytes"]  # まとめを決めた周の1回だけ


def test_summary_without_size_function_still_reports_other_signals():
    store = SizedStore(None)
    mon, clock, sent = make(ts(2026, 10, 22, 23, 50))
    for _ in range(20):
        mon.record_db_response(0.1, True)
    clock.t = ts(2026, 10, 23, 0, 0)
    mon.tick(store=store)
    clock.t = ts(2026, 10, 23, 0, 11)
    mon.tick(store=store)
    summary = [m for m in sent if m.startswith("💰")]  # すぐ知らせる「DB が応答しません」も別に届く
    assert len(summary) == 1 and "応答なしが 20件" in summary[0] and "DB の大きさ 測れず" in summary[0]


def test_small_db_is_quiet():
    store = SizedStore(385 * MB)
    mon, clock, sent = make(ts(2026, 10, 22, 23, 0))
    clock.t = ts(2026, 10, 23, 0, 20)
    mon.tick(store=store)
    assert sent == []
