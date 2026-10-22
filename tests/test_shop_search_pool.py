"""通信なしで、店ごとの列・優先度・交互回し・待ち時間の上限・取り消し・共有・集計を検証する。"""
from concurrent.futures import Future
from functools import partial
from threading import Event, Lock, Thread
import json
import time

import pytest

from shop_search_pool import (
    PRIORITY_BULK, PRIORITY_SINGLE, PoolBusyError, PoolCapacityError,
    SearchAbandonedError, ShopSearchPool, iter_with_heartbeat,
)


def run(fn, name):
    return fn(name)


class Gate:
    """取得を手で止めておき、始まった順と同時実行数を記録する。"""

    def __init__(self):
        self.lock = Lock()
        self.started = []
        self.active = {}
        self.peak = {}
        self.peak_total = 0
        self.release_all = Event()
        self.events = {}

    def fn(self, shop):
        def search(name):
            with self.lock:
                self.started.append((shop, name))
                self.active[shop] = self.active.get(shop, 0) + 1
                self.peak[shop] = max(self.peak.get(shop, 0), self.active[shop])
                self.peak_total = max(self.peak_total, sum(self.active.values()))
                event = self.events.setdefault((shop, name), Event())
                if self.release_all.is_set():
                    event.set()
            assert event.wait(5)
            with self.lock:
                self.active[shop] -= 1
            return (shop, name)
        search.__name__ = f'search_{shop}'
        return search

    def release(self, shop, name):
        with self.lock:
            self.events.setdefault((shop, name), Event()).set()

    def wait_started(self, count, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                if len(self.started) >= count:
                    return list(self.started)
            time.sleep(0.005)
        with self.lock:
            raise AssertionError(f'{count}件始まりませんでした: {self.started}')

    def finish_all(self):
        self.release_all.set()
        with self.lock:
            for event in self.events.values():
                event.set()


@pytest.fixture
def gate():
    g = Gate()
    yield g
    g.finish_all()


def test_per_shop_and_total_limits(gate):
    shops = [f'店{i}' for i in range(4)]
    fns = {s: gate.fn(s) for s in shops}
    with ShopSearchPool(6, 2, 100) as pool:
        req = pool.request(PRIORITY_SINGLE, 30)
        futures = [req.submit(fns[s], f'カード{n}', shop=s, runner=run)
                   for s in shops for n in range(4)]
        gate.wait_started(6)
        time.sleep(0.05)
        assert len(gate.started) == 6  # 全体の上限
        assert all(v <= 2 for v in gate.active.values())  # 1店の上限
        assert pool.snapshot()['queued'] == 16 - 6
        gate.finish_all()
        assert sorted(f.result(3) for f in futures) == sorted((s, f'カード{n}') for s in shops for n in range(4))
    assert gate.peak_total == 6
    assert max(gate.peak.values()) == 2
    snapshot = pool.snapshot()
    assert snapshot['peak_running'] == 6
    # 4店×2件＝8件ぶんの枠があっても、全体の6件が先に効く
    assert max(snapshot['by_shop'][s]['peak_running'] for s in shops) == 2
    assert sum(snapshot['by_shop'][s]['peak_running'] for s in shops) >= 6
    assert snapshot['by_shop']['店0']['accepted'] == 4
    assert snapshot['by_priority']['single']['accepted'] == 16


def test_single_jumps_ahead_of_bulk_in_the_same_shop(gate):
    search = gate.fn('店A')
    with ShopSearchPool(2, 1, 100) as pool:
        bulk = pool.request(PRIORITY_BULK, 180)
        single = pool.request(PRIORITY_SINGLE, 30)
        first = bulk.submit(search, 'デッキ0', shop='店A', runner=run)
        gate.wait_started(1)
        queued = [bulk.submit(search, f'デッキ{n}', shop='店A', runner=run) for n in range(1, 4)]
        mine = single.submit(search, '単品', shop='店A', runner=run)
        gate.release('店A', 'デッキ0')
        assert gate.wait_started(2)[1] == ('店A', '単品')
        gate.finish_all()
        for f in [first, mine, *queued]:
            f.result(3)


def test_round_robin_between_searches_of_same_priority(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 100) as pool:
        deck1 = pool.request(PRIORITY_BULK, 180)
        deck2 = pool.request(PRIORITY_BULK, 180)
        blocker = deck1.submit(search, 'D1-0', shop='店A', runner=run)
        gate.wait_started(1)
        rest1 = [deck1.submit(search, f'D1-{n}', shop='店A', runner=run) for n in range(1, 5)]
        rest2 = [deck2.submit(search, f'D2-{n}', shop='店A', runner=run) for n in range(3)]
        order = []
        for step in range(1, 9):
            started = gate.wait_started(step)
            gate.release(*started[-1])
            order.append(started[-1][1])
        for f in [blocker, *rest1, *rest2]:
            f.result(3)
    # 先に並んだ一括検索が5件あっても、後から来た一括検索と1件ずつ交互に取る
    assert order == ['D1-0', 'D1-1', 'D2-0', 'D1-2', 'D2-1', 'D1-3', 'D2-2', 'D1-4']


def test_idle_shop_is_not_blocked_by_full_shop(gate):
    a, b = gate.fn('店A'), gate.fn('店B')
    with ShopSearchPool(6, 2, 100) as pool:
        req = pool.request(PRIORITY_BULK, 180)
        futures = [req.submit(a, f'A{n}', shop='店A', runner=run) for n in range(5)]
        single = pool.request(PRIORITY_SINGLE, 30)
        other = single.submit(b, '単品', shop='店B', runner=run)
        started = gate.wait_started(3)
        assert ('店B', '単品') in started
        gate.finish_all()
        for f in [*futures, other]:
            f.result(3)


def test_shared_fetch_runs_once_and_each_waiter_gets_own_future(gate):
    search = gate.fn('店A')
    with ShopSearchPool(2, 2, 100) as pool:
        r1 = pool.request(PRIORITY_SINGLE, 30)
        r2 = pool.request(PRIORITY_BULK, 180)
        f1 = r1.submit(search, '青眼', shop='店A', runner=run)
        f2 = r2.submit(search, '青眼', shop='店A', runner=run)
        assert f1 is not f2
        assert f1.fetch_token is f2.fetch_token
        gate.finish_all()
        assert f1.result(3) == f2.result(3) == ('店A', '青眼')
    assert gate.started == [('店A', '青眼')]
    snapshot = pool.snapshot()
    assert snapshot['accepted'] == 1 and snapshot['shared'] == 1
    assert not pool._inflight and not pool._waiters


def test_single_sharing_a_queued_bulk_fetch_lifts_its_priority(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 100) as pool:
        deck = pool.request(PRIORITY_BULK, 180)
        blocker = deck.submit(search, 'D0', shop='店A', runner=run)
        gate.wait_started(1)
        others = [deck.submit(search, f'D{n}', shop='店A', runner=run) for n in range(1, 4)]
        single = pool.request(PRIORITY_SINGLE, 30)
        shared = single.submit(search, 'D3', shop='店A', runner=run)  # 一括検索の最後と同じ取得
        gate.release('店A', 'D0')
        assert gate.wait_started(2)[1] == ('店A', 'D3')
        gate.finish_all()
        assert shared.result(3) == others[2].result(3)
        blocker.result(3)


def test_function_and_partial_settings_are_not_mixed(gate):
    release = Event()

    def search(name, max_pages=None):
        assert release.wait(3)
        return name, max_pages

    def other(name):
        assert release.wait(3)
        return '別店舗', name

    with ShopSearchPool(2, 2, 100) as pool:
        req = pool.request(PRIORITY_SINGLE, 30)
        try:
            full = req.submit(search, '青眼', shop='店A', runner=run)
            fast = req.submit(partial(search, max_pages=1), '青眼', shop='店A', runner=run)
            same_fast = req.submit(partial(search, max_pages=1), '青眼', shop='店A', runner=run)
            second = req.submit(partial(search, max_pages=2), '青眼', shop='店A', runner=run)
            different_shop = req.submit(other, '青眼', shop='店B', runner=run)
            different_card = req.submit(search, '別カード', shop='店A', runner=run)
            assert fast.fetch_token is same_fast.fetch_token
            assert len({id(f.fetch_token) for f in (full, fast, second, different_shop, different_card)}) == 5
        finally:
            release.set()
        assert full.result(3) == ('青眼', None)
        assert fast.result(3) == ('青眼', 1)
        assert second.result(3) == ('青眼', 2)
        assert different_shop.result(3) == ('別店舗', '青眼')


def test_queue_timeout_marks_only_waiting_fetch_busy(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 100) as pool:
        holder = pool.request(PRIORITY_SINGLE, 30)
        running = holder.submit(search, '実行中', shop='店A', runner=run)
        gate.wait_started(1)
        req = pool.request(PRIORITY_SINGLE, 0.2)
        waiting = req.submit(search, '順番待ち', shop='店A', runner=run)
        t0 = time.monotonic()
        results = list(req.as_completed([waiting]))
        assert results == [waiting]
        assert 0.15 <= time.monotonic() - t0 < 2
        with pytest.raises(PoolBusyError):
            waiting.result()
        assert isinstance(waiting.exception(), PoolCapacityError)  # 店舗障害として数えない
        gate.release('店A', '実行中')
        running.result(3)
        time.sleep(0.05)
    # 待ちきれずに外れた取得は実行されない（待つ人がいないので取り消し）
    assert gate.started == [('店A', '実行中')]
    snapshot = pool.snapshot()
    assert snapshot['busy_expired'] == 1 and snapshot['cancelled'] == 1
    assert snapshot['by_shop']['店A']['busy_expired'] == 1
    assert snapshot['by_priority']['single']['busy_expired'] == 1
    assert snapshot['pending'] == 0


def test_running_fetch_is_waited_for_even_after_queue_timeout(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 100) as pool:
        req = pool.request(PRIORITY_SINGLE, 0.1)
        future = req.submit(search, '始まった', shop='店A', runner=run)
        gate.wait_started(1)
        Thread(target=lambda: (time.sleep(0.4), gate.release('店A', '始まった')), daemon=True).start()
        assert list(req.as_completed([future])) == [future]
        assert future.result() == ('店A', '始まった')


def test_busy_waiter_leaves_shared_fetch_for_others(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 100) as pool:
        holder = pool.request(PRIORITY_SINGLE, 30)
        running = holder.submit(search, '実行中', shop='店A', runner=run)
        gate.wait_started(1)
        impatient = pool.request(PRIORITY_SINGLE, 0.1)
        patient = pool.request(PRIORITY_BULK, 30)
        f_short = impatient.submit(search, '共有', shop='店A', runner=run)
        f_long = patient.submit(search, '共有', shop='店A', runner=run)
        list(impatient.as_completed([f_short]))
        assert isinstance(f_short.exception(), PoolBusyError)
        gate.release('店A', '実行中')
        gate.release('店A', '共有')
        assert f_long.result(3) == ('店A', '共有')
        running.result(3)


def test_close_cancels_not_started_and_keeps_shared(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 100) as pool:
        deck = pool.request(PRIORITY_BULK, 180)
        started = deck.submit(search, '実行中', shop='店A', runner=run)
        gate.wait_started(1)
        mine_only = [deck.submit(search, f'自分だけ{n}', shop='店A', runner=run) for n in range(3)]
        other = pool.request(PRIORITY_SINGLE, 30)
        shared_mine = deck.submit(search, '共有', shop='店A', runner=run)
        shared_other = other.submit(search, '共有', shop='店A', runner=run)
        deck.close()
        for f in [started, *mine_only, shared_mine]:
            assert isinstance(f.exception(0), SearchAbandonedError)
        # 閉じたあとの依頼はすぐ取り消しで返る（app.py の _submit_shop_fetch が失敗の Future に包む）
        with pytest.raises(SearchAbandonedError):
            deck.submit(search, '閉じたあと', shop='店A', runner=run)
        gate.release('店A', '実行中')
        gate.release('店A', '共有')
        assert shared_other.result(3) == ('店A', '共有')
        time.sleep(0.05)
    # 自分だけが待っていた3件は実行されない。実行中の1件と、ほかの人も待つ1件は実行される
    assert gate.started == [('店A', '実行中'), ('店A', '共有')]
    snapshot = pool.snapshot()
    assert snapshot['cancelled'] == 3
    assert snapshot['abandoned'] == 6  # 取り消した受け取り（実行中1・自分だけ3・共有1・閉じたあと1）
    assert snapshot['pending'] == 0 and not pool._waiters


def test_close_after_normal_completion_is_harmless(gate):
    search = gate.fn('店A')
    with ShopSearchPool(2, 2, 100) as pool:
        with pool.request(PRIORITY_SINGLE, 30) as req:
            future = req.submit(search, '青眼', shop='店A', runner=run)
            gate.finish_all()
            assert future.result(3) == ('店A', '青眼')
        req.close()
    assert pool.snapshot()['abandoned'] == 0


def test_failure_is_removed_and_can_be_retried():
    attempts = 0

    def search(name):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError('店舗取得失敗')
        return name

    with ShopSearchPool(2, 2, 2) as pool:
        req = pool.request(PRIORITY_SINGLE, 30)
        failed = req.submit(search, '青眼', shop='店A', runner=run)
        with pytest.raises(OSError, match='店舗取得失敗'):
            failed.result(3)
        retry = req.submit(search, '青眼', shop='店A', runner=run)
        assert retry.fetch_token is not failed.fetch_token
        assert retry.result(3) == '青眼'
    assert attempts == 2
    assert not pool._inflight


def test_capacity_safety_limit_rejects_distinct_fetches(gate):
    search = gate.fn('店A')
    with ShopSearchPool(1, 1, 2) as pool:
        req = pool.request(PRIORITY_BULK, 180)
        a = req.submit(search, 'A', shop='店A', runner=run)
        b = req.submit(search, 'B', shop='店A', runner=run)
        assert req.submit(search, 'A', shop='店A', runner=run).fetch_token is a.fetch_token  # 重複は受け付ける
        with pytest.raises(PoolCapacityError):
            req.submit(search, 'C', shop='店A', runner=run)
        gate.finish_all()
        a.result(3), b.result(3)
    assert pool.snapshot()['capacity_rejected'] == 1


def test_synchronous_completion_does_not_deadlock(monkeypatch):
    # add_done_callback や結果配布がその場で呼び出される経路を確実に通す。
    pool = ShopSearchPool(2, 2, 2)

    def immediate(fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    monkeypatch.setattr(pool._executor, 'submit', immediate)
    done = Event()
    results = []

    def submit():
        req = pool.request(PRIORITY_SINGLE, 30)
        results.append(req.submit(lambda name: name, '青眼', shop='店A', runner=run))
        done.set()

    Thread(target=submit, daemon=True).start()
    assert done.wait(3), '同期完了でデッドロックしました'
    assert results[0].result() == '青眼'
    assert not pool._inflight
    pool.shutdown()


def test_shutdown_rejects_new_requests():
    pool = ShopSearchPool(2, 2, 2)
    pool.shutdown()
    with pytest.raises(RuntimeError, match='終了'):
        pool.request(PRIORITY_SINGLE, 30).submit(str, '青眼', shop='店A', runner=run)
    assert pool.snapshot()['shutdown_rejected'] == 1


def test_different_runners_are_not_shared():
    release = Event()

    def search(name):
        assert release.wait(3)
        return name

    def with_status(fn, name):
        return fn(name), 0

    with ShopSearchPool(2, 2, 2) as pool:
        req = pool.request(PRIORITY_SINGLE, 30)
        try:
            raw = req.submit(search, '青眼', shop='店A', runner=run)
            status = req.submit(search, '青眼', shop='店A', runner=with_status)
            assert raw.fetch_token is not status.fetch_token
        finally:
            release.set()
        assert raw.result(3) == '青眼'
        assert status.result(3) == ('青眼', 0)


def test_executor_submit_failure_does_not_consume_capacity(monkeypatch):
    with ShopSearchPool(2, 2, 2) as pool:
        original = pool._executor.submit

        def fail(*args):
            raise RuntimeError('executor の起動失敗')

        monkeypatch.setattr(pool._executor, 'submit', fail)
        req = pool.request(PRIORITY_SINGLE, 30)
        future = req.submit(str, '青眼', shop='店A', runner=run)
        with pytest.raises(RuntimeError, match='起動失敗'):
            future.result(0)
        assert not pool._inflight
        monkeypatch.setattr(pool._executor, 'submit', original)
        assert req.submit(str, '青眼', shop='店A', runner=run).result(3) == '青眼'
    snapshot = pool.snapshot()
    assert snapshot['submission_errors'] == 1 and snapshot['pending'] == 0 and snapshot['running'] == 0


@pytest.mark.parametrize('workers,per_shop,pending', [
    (0, 1, 2), (2, 0, 2), (2, 3, 2), (2, 1, 1), (True, 1, 2), (2, 1, 2.5)])
def test_invalid_capacity_is_rejected(workers, per_shop, pending):
    with pytest.raises(ValueError):
        ShopSearchPool(workers, per_shop, pending)


@pytest.mark.parametrize('priority,timeout', [('urgent', 30), (PRIORITY_SINGLE, 0),
                                              (PRIORITY_BULK, float('inf'))])
def test_invalid_request_is_rejected(priority, timeout):
    with ShopSearchPool(2, 2, 2) as pool:
        with pytest.raises(ValueError):
            pool.request(priority, timeout)


class ManualClock:
    """壁時計の変更と無関係に、待機・実行の経過秒を制御する。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ManualExecutor:
    """Futureの実際の状態遷移を保ち、実行開始の時刻だけ手動にする。"""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((future, fn, args))
        return future

    def run_next(self):
        future, fn, args = self.jobs.pop(0)
        if future.set_running_or_notify_cancel():
            try:
                future.set_result(fn(*args))
            except BaseException as error:
                future.set_exception(error)


def test_metrics_exact_counts_and_times(monkeypatch):
    clock = ManualClock()
    executor = ManualExecutor()
    pool = ShopSearchPool(2, 2, 3, clock=clock)
    monkeypatch.setattr(pool._executor, 'submit', executor.submit)

    def search(name):
        # runnerからsnapshotを読める（計測ロックを持ったまま実行していない）。
        assert pool.snapshot()['running'] >= 1
        if name == '例外になる入力':
            clock.now = 11
            raise OSError('ログに出してはいけない入力を含む例外')
        clock.now = 7
        return name

    req = pool.request(PRIORITY_SINGLE, 30)
    clock.now = 1
    success = req.submit(search, '個人入力', shop='店A', runner=run)
    clock.now = 2
    failure = req.submit(search, '例外になる入力', shop='店A', runner=run)
    assert req.submit(search, '個人入力', shop='店A', runner=run).fetch_token is success.fetch_token
    clock.now = 5
    executor.run_next()
    clock.now = 8
    executor.run_next()
    assert success.result() == '個人入力'
    with pytest.raises(OSError):
        failure.result()
    data = pool.snapshot()
    for key, expected in {'accepted': 2, 'shared': 1, 'capacity_rejected': 0,
                          'completed': 2, 'exceptions': 1, 'cancelled': 0,
                          'running': 0, 'pending': 0, 'queued': 0,
                          'peak_running': 2, 'peak_pending': 2}.items():
        assert data[key] == expected, key
    assert data['wait_seconds'] == {'count': 2, 'total': 10, 'max': 6}
    assert data['run_seconds'] == {'count': 2, 'total': 5, 'max': 3}
    assert data['by_shop']['店A']['wait_seconds'] == {'count': 2, 'total': 10, 'max': 6}
    assert data['by_priority']['single']['wait_seconds']['count'] == 2
    assert data['by_priority']['bulk']['wait_seconds']['count'] == 0
    data['wait_seconds']['total'] = -1
    data['by_shop']['店A']['wait_seconds']['total'] = -1
    assert pool.snapshot()['wait_seconds']['total'] == 10
    assert pool.snapshot()['by_shop']['店A']['wait_seconds']['total'] == 10
    pool.shutdown()


def test_metrics_log_is_throttled_private_and_outside_locks(monkeypatch):
    clock = ManualClock()
    executor = ManualExecutor()
    lines = []

    class Logger:
        def info(self, format_string, value):
            # 元の受付ロックも計測ロックも解放してからloggerを呼ぶ。
            for lock in (pool._lock, pool._metrics_lock):
                assert lock.acquire(blocking=False)
                lock.release()
            pool.snapshot()
            lines.append(format_string % value)

    pool = ShopSearchPool(2, 2, 2, clock=clock, logger=Logger())
    monkeypatch.setattr(pool._executor, 'submit', executor.submit)
    req = pool.request(PRIORITY_BULK, 1000)
    req.submit(str, '秘密のカード名', shop='店A', runner=run)
    clock.now = 59
    req.submit(str, '秘密のカード名', shop='店A', runner=run)
    assert lines == []
    clock.now = 60
    req.submit(str, '秘密のカード名', shop='店A', runner=run)
    assert len(lines) == 1
    assert json.loads(lines[0])['shared'] == 2
    executor.run_next()
    assert len(lines) == 1
    clock.now = 120
    req.submit(str, '別の秘密のカード名', shop='店B', runner=run)
    assert len(lines) == 2
    executor.run_next()
    pool.shutdown()
    assert len(lines) == 2
    for line in lines:
        assert '秘密' not in line
        data = json.loads(line)
        assert data['event'] == 'shop_search_pool'
        assert data['max_workers'] == 2 and data['per_shop_limit'] == 2 and data['max_pending'] == 2
        assert data['pid'] > 0 and data['instance_id']
        assert data['started_at_utc'].endswith('+00:00')
        assert set(data['by_priority']) == {'single', 'bulk'}
    last = json.loads(lines[1])
    assert set(last['by_shop']) == {'店A', '店B'}
    assert last['by_priority']['bulk']['accepted'] == 2
    assert json.loads(lines[0])['instance_id'] == last['instance_id']


@pytest.mark.parametrize('interval', [0, -1, float('inf'), float('nan')])
def test_invalid_log_interval(interval):
    with pytest.raises(ValueError):
        ShopSearchPool(2, 2, 2, log_interval=interval)


def test_logger_failure_does_not_fail_search_or_change_capacity(monkeypatch):
    class BrokenLogger:
        def info(self, *args):
            raise RuntimeError('ログ収集先の障害')

    clock = ManualClock()
    executor = ManualExecutor()
    pool = ShopSearchPool(2, 2, 2, clock=clock, logger=BrokenLogger())
    monkeypatch.setattr(pool._executor, 'submit', executor.submit)
    clock.now = 60
    future = pool.request(PRIORITY_SINGLE, 1000).submit(str, 'ログ障害でも返す結果', shop='店A', runner=run)
    assert pool.snapshot()['log_errors'] == 1
    clock.now = 120
    executor.run_next()
    assert future.result() == 'ログ障害でも返す結果'
    assert pool.snapshot()['log_errors'] == 2
    assert pool.snapshot()['completed'] == 1 and pool.snapshot()['pending'] == 0
    pool.shutdown()


def test_instances_are_identifiable_and_snapshot_does_not_emit_logs():
    class UnexpectedLogger:
        def info(self, *args):
            raise AssertionError('snapshotだけでログを出してはいけない')

    clock = ManualClock()
    with ShopSearchPool(2, 2, 2, clock=clock, logger=UnexpectedLogger()) as first, \
            ShopSearchPool(2, 2, 2, clock=clock, logger=UnexpectedLogger()) as second:
        assert first.snapshot()['pid'] == second.snapshot()['pid']
        assert first.snapshot()['instance_id'] != second.snapshot()['instance_id']
        clock.now = 120
        assert first.snapshot()['uptime_seconds'] == 120
        assert first.snapshot()['log_errors'] == 0
        clock.now = 0


def test_shutdown_without_wait_preserves_running_job_and_cancels_queued(gate):
    search = gate.fn('店A')
    pool = ShopSearchPool(1, 1, 5)
    req = pool.request(PRIORITY_SINGLE, 30)
    running = req.submit(search, '実行中', shop='店A', runner=run)
    gate.wait_started(1)
    queued = req.submit(search, '順番待ち', shop='店A', runner=run)
    pool.shutdown(wait=False)
    assert isinstance(queued.exception(0), SearchAbandonedError)
    assert pool.snapshot()['running'] == 1
    with pytest.raises(RuntimeError):
        req.submit(search, '終了後の新規', shop='店A', runner=run)
    gate.release('店A', '実行中')
    assert running.result(3) == ('店A', '実行中')
    pool.shutdown()
    data = pool.snapshot()
    assert data['completed'] == 1 and data['cancelled'] == 1
    assert data['pending'] == 0 and data['running'] == 0
    assert gate.started == [('店A', '実行中')]


def test_iter_with_heartbeat_yields_none_while_waiting():
    future = Future()
    Thread(target=lambda: (time.sleep(0.25), future.set_result(1)), daemon=True).start()
    seen = list(iter_with_heartbeat([future], 0.1))
    assert seen[-1] is future
    assert seen[:-1] and all(item is None for item in seen[:-1])


def test_request_as_completed_heartbeat_and_foreign_futures(gate):
    search = gate.fn('店A')
    foreign = Future()
    foreign.set_result('外の Future')
    with ShopSearchPool(1, 1, 5) as pool:
        req = pool.request(PRIORITY_SINGLE, 30)
        future = req.submit(search, '青眼', shop='店A', runner=run)
        gate.wait_started(1)
        Thread(target=lambda: (time.sleep(0.3), gate.release('店A', '青眼')), daemon=True).start()
        seen = list(req.as_completed([foreign, future], heartbeat=0.1))
        assert foreign in seen and future in seen
        assert None in seen
        assert req.is_queued(future) is False
