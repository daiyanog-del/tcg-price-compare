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

    def immediate(runner, fn, name):
        future = Future()
        future.set_result(runner(fn, name))
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
