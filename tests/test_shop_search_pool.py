"""通信なしで共有検索の競合・上限・失敗後の再試行を検証する。"""
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import Event, Lock

import pytest

from shop_search_pool import PoolCapacityError, ShopSearchPool


def run(fn, name):
    return fn(name)


@pytest.mark.parametrize('workers', [2, 6])
def test_shared_jobs_and_execution_capacity(workers):
    release, full = Event(), Event()
    lock = Lock()
    active = peak = calls = 0

    def search(name):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == workers:
                full.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return name

    with ShopSearchPool(workers, workers * 2) as pool:
        try:
            originals = [pool.submit(search, str(i), runner=run) for i in range(workers * 2)]
            assert full.wait(3)
            with ThreadPoolExecutor(max_workers=2) as callers:
                duplicates = list(callers.map(lambda i: pool.submit(search, str(i), runner=run),
                                              range(workers * 2)))
            assert all(a is b for a, b in zip(originals, duplicates))
            with pytest.raises(PoolCapacityError):
                pool.submit(search, '枠を超えるカード', runner=run)
        finally:
            release.set()
        assert [f.result(timeout=3) for f in originals] == [str(i) for i in range(workers * 2)]
    assert peak == workers
    assert calls == workers * 2
    assert not pool._inflight
    metrics = pool.snapshot()
    assert metrics['peak_running'] == workers
    assert metrics['peak_pending'] == workers * 2
    assert metrics['accepted'] == workers * 2 and metrics['shared'] == workers * 2
    assert metrics['capacity_rejected'] == 1
    assert metrics['completed'] == workers * 2 and metrics['exceptions'] == 0


def test_function_and_partial_settings_are_not_mixed():
    release = Event()

    def search(name, max_pages=None):
        assert release.wait(3)
        return name, max_pages

    def other(name):
        assert release.wait(3)
        return '別店舗', name

    with ShopSearchPool(2, 6) as pool:
        try:
            full = pool.submit(search, '青眼', runner=run)
            fast = pool.submit(partial(search, max_pages=1), '青眼', runner=run)
            same_fast = pool.submit(partial(search, max_pages=1), '青眼', runner=run)
            second = pool.submit(partial(search, max_pages=2), '青眼', runner=run)
            different_shop = pool.submit(other, '青眼', runner=run)
            different_card = pool.submit(search, '別カード', runner=run)
            assert fast is same_fast
            assert len({full, fast, second, different_shop, different_card}) == 5
        finally:
            release.set()
        assert full.result(3) == ('青眼', None)
        assert fast.result(3) == ('青眼', 1)
        assert second.result(3) == ('青眼', 2)
        assert different_shop.result(3) == ('別店舗', '青眼')


def test_failure_is_removed_and_can_be_retried():
    attempts = 0

    def search(name):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError('店舗取得失敗')
        return name

    with ShopSearchPool(2, 2) as pool:
        failed = pool.submit(search, '青眼', runner=run)
        with pytest.raises(OSError, match='店舗取得失敗'):
            failed.result(3)
        retry = pool.submit(search, '青眼', runner=run)
        assert retry is not failed
        assert retry.result(3) == '青眼'
    assert attempts == 2
    assert not pool._inflight


def test_synchronous_completion_does_not_deadlock(monkeypatch):
    # add_done_callback がその場で呼び出される経路を確実に通す。
    pool = ShopSearchPool(2, 2)

    def immediate(runner, *args):
        future = Future()
        future.set_result(runner(*args))
        return future

    monkeypatch.setattr(pool._executor, 'submit', immediate)
    # 不具合時にも pytest 自体を永久停止させないよう daemon thread で監視する。
    from threading import Thread
    done = Event()
    results = []

    def submit():
        results.append(pool.submit(lambda name: name, '青眼', runner=run))
        done.set()

    Thread(target=submit, daemon=True).start()
    assert done.wait(3), '同期完了コールバックがデッドロックしました'
    assert results[0].result() == '青眼'
    assert not pool._inflight
    pool.shutdown()


def test_shutdown_rejects_new_requests():
    pool = ShopSearchPool(2, 2)
    pool.shutdown()
    with pytest.raises(RuntimeError, match='終了'):
        pool.submit(str, '青眼', runner=run)


def test_different_runners_are_not_shared():
    release = Event()

    def search(name):
        assert release.wait(3)
        return name

    def with_status(fn, name):
        return fn(name), 0

    with ShopSearchPool(2, 2) as pool:
        try:
            raw = pool.submit(search, '青眼', runner=run)
            status = pool.submit(search, '青眼', runner=with_status)
            assert raw is not status
        finally:
            release.set()
        assert raw.result(3) == '青眼'
        assert status.result(3) == ('青眼', 0)


def test_executor_submit_failure_does_not_consume_capacity(monkeypatch):
    with ShopSearchPool(2, 2) as pool:
        original = pool._executor.submit

        def fail(*args):
            raise RuntimeError('executor の起動失敗')

        monkeypatch.setattr(pool._executor, 'submit', fail)
        with pytest.raises(RuntimeError, match='起動失敗'):
            pool.submit(str, '青眼', runner=run)
        assert not pool._inflight
        monkeypatch.setattr(pool._executor, 'submit', original)
        assert pool.submit(str, '青眼', runner=run).result(3) == '青眼'


@pytest.mark.parametrize('workers,pending', [(0, 2), (2, 1), (True, 2), (2, 2.5)])
def test_invalid_capacity_is_rejected(workers, pending):
    with pytest.raises(ValueError):
        ShopSearchPool(workers, pending)


class ManualClock:
    """壁時計の変更と無関係に、待機・実行の経過秒を制御する。"""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ManualExecutor:
    """Futureの実際の状態遷移を保ち、キュー開始時刻だけ手動にする。"""

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


def test_metrics_exact_counts_times_and_cancel(monkeypatch):
    clock = ManualClock()
    executor = ManualExecutor()
    pool = ShopSearchPool(2, 2, clock=clock)
    monkeypatch.setattr(pool._executor, 'submit', executor.submit)

    def search(name):
        # runnerからsnapshotを読める（計測ロックを持ったまま実行していない）。
        assert pool.snapshot()['running'] == 1
        if name == '例外になる入力':
            clock.now = 11
            raise OSError('ログに出してはいけない入力を含む例外')
        clock.now = 7
        return name

    clock.now = 1
    success = pool.submit(search, '個人入力', runner=run)
    clock.now = 2
    failure = pool.submit(search, '例外になる入力', runner=run)
    assert pool.submit(search, '個人入力', runner=run) is success
    with pytest.raises(PoolCapacityError):
        pool.submit(search, '受付上限を超えた入力', runner=run)
    assert pool.snapshot()['queued'] == 2
    clock.now = 5
    executor.run_next()
    clock.now = 8
    executor.run_next()
    assert success.result() == '個人入力'
    with pytest.raises(OSError):
        failure.result()
    cancelled = pool.submit(search, 'キャンセル入力', runner=run)
    assert cancelled.cancel()
    executor.run_next()
    data = pool.snapshot()
    for key, expected in {'accepted': 3, 'shared': 1, 'capacity_rejected': 1,
                          'completed': 2, 'exceptions': 1, 'cancelled': 1,
                          'running': 0, 'pending': 0, 'queued': 0,
                          'peak_running': 1, 'peak_pending': 2}.items():
        assert data[key] == expected, key
    assert data['wait_seconds'] == {'count': 2, 'total': 10, 'max': 6}
    assert data['run_seconds'] == {'count': 2, 'total': 5, 'max': 3}
    data['wait_seconds']['total'] = -1
    assert pool.snapshot()['wait_seconds']['total'] == 10
    pool.shutdown()
    with pytest.raises(RuntimeError):
        pool.submit(search, '終了後', runner=run)
    assert pool.snapshot()['shutdown_rejected'] == 1


def test_metrics_log_is_throttled_private_and_outside_locks(monkeypatch):
    import json

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

    pool = ShopSearchPool(2, 2, clock=clock, logger=Logger())
    monkeypatch.setattr(pool._executor, 'submit', executor.submit)
    pool.submit(str, '秘密のカード名', runner=run)
    clock.now = 59
    pool.submit(str, '秘密のカード名', runner=run)
    assert lines == []
    clock.now = 60
    pool.submit(str, '秘密のカード名', runner=run)
    assert len(lines) == 1
    assert json.loads(lines[0])['shared'] == 2
    executor.run_next()
    assert len(lines) == 1
    clock.now = 120
    pool.submit(str, '別の秘密のカード名', runner=run)
    assert len(lines) == 2
    executor.run_next()
    pool.shutdown()
    assert len(lines) == 2
    for line in lines:
        assert '秘密' not in line
        data = json.loads(line)
        assert data['event'] == 'shop_search_pool'
        assert data['max_workers'] == 2 and data['max_pending'] == 2
        assert data['pid'] > 0 and data['instance_id']
        assert data['started_at_utc'].endswith('+00:00')
    assert json.loads(lines[0])['instance_id'] == json.loads(lines[1])['instance_id']


def test_metrics_synchronous_executor_and_submission_error(monkeypatch):
    clock = ManualClock()
    pool = ShopSearchPool(2, 2, clock=clock)

    def immediate(fn, *args):
        future = Future()
        future.set_running_or_notify_cancel()
        future.set_result(fn(*args))
        return future

    def runner(fn, name):
        assert pool.snapshot()['running'] == 1
        clock.now += 2
        return fn(name)

    monkeypatch.setattr(pool._executor, 'submit', immediate)
    assert pool.submit(str, '同期完了', runner=runner).result() == '同期完了'

    def rejected(*args):
        raise RuntimeError('executorは終了済み')

    monkeypatch.setattr(pool._executor, 'submit', rejected)
    with pytest.raises(RuntimeError, match='終了済み'):
        pool.submit(str, '投入失敗', runner=runner)
    data = pool.snapshot()
    assert data['accepted'] == 2 and data['completed'] == 1
    assert data['submission_errors'] == 1 and data['exceptions'] == 0
    assert data['pending'] == 0 and data['running'] == 0
    assert data['wait_seconds']['total'] == 0
    assert data['run_seconds']['total'] == 2
    pool.shutdown(wait=False)


@pytest.mark.parametrize('interval', [0, -1, float('inf'), float('nan')])
def test_invalid_log_interval(interval):
    with pytest.raises(ValueError):
        ShopSearchPool(2, 2, log_interval=interval)


def test_logger_failure_does_not_fail_search_or_change_capacity(monkeypatch):
    class BrokenLogger:
        def info(self, *args):
            raise RuntimeError('ログ収集先の障害')

    clock = ManualClock()
    executor = ManualExecutor()
    pool = ShopSearchPool(2, 2, clock=clock, logger=BrokenLogger())
    monkeypatch.setattr(pool._executor, 'submit', executor.submit)
    clock.now = 60
    future = pool.submit(str, 'ログ障害でも返す結果', runner=run)
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
    with ShopSearchPool(2, 2, clock=clock, logger=UnexpectedLogger()) as first, \
            ShopSearchPool(2, 2, clock=clock, logger=UnexpectedLogger()) as second:
        assert first.snapshot()['pid'] == second.snapshot()['pid']
        assert first.snapshot()['instance_id'] != second.snapshot()['instance_id']
        clock.now = 120
        assert first.snapshot()['uptime_seconds'] == 120
        assert first.snapshot()['log_errors'] == 0
        clock.now = 0


def test_shutdown_without_wait_preserves_running_job_metrics():
    started, release = Event(), Event()
    pool = ShopSearchPool(2, 2)

    def search(name):
        started.set()
        assert release.wait(3)
        return name

    future = pool.submit(search, '実行中', runner=run)
    try:
        assert started.wait(3)
        assert not future.cancel(), '実行中のFutureをキャンセルできてはいけない'
        pool.shutdown(wait=False)
        assert pool.snapshot()['running'] == 1
        assert pool.snapshot()['pending'] == 1
        with pytest.raises(RuntimeError):
            pool.submit(search, '終了後の新規', runner=run)
    finally:
        release.set()
    assert future.result(3) == '実行中'
    pool.shutdown()
    data = pool.snapshot()
    assert data['completed'] == 1 and data['cancelled'] == 0
    assert data['pending'] == 0 and data['running'] == 0
    assert data['shutdown_rejected'] == 1
