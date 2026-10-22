"""プロセス内の店舗検索を共有し、店ごとの列で順番に取得する。

- 店ごとに列を分ける。1店あたりの同時取得数と、全体の同時取得数に上限を設ける
- 列の中では単品検索（single）を一括検索（bulk）より先に取る。同じ優先度の中は
  検索（SearchRequest）ごとに1件ずつ交互に回し、1件の一括検索が列を独占しない
- あふれても即失敗にしない。待つ時間の上限（検索ごと）を過ぎた取得だけを
  PoolBusyError にする（呼び出し側は手元のキャッシュで代わりを出す）
- 利用者が画面を閉じたら（SearchRequest.close）、まだ始まっていない取得を取り消す
- 同じ店・同じカード・同じ設定の取得は1回にまとめ、待っている全員に結果を配る

複数のサーバープロセス間では共有されない。検索結果のオブジェクトは同時利用者間で
共有するため、呼び出し元では結果を直接変更しない（コピーしてから使う）。
上限は呼び出し側が指定し、実測で校正する。
"""
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait as _futures_wait
from datetime import datetime, timezone
from functools import partial
from itertools import count
import json
import logging
import math
import os
from threading import Lock
from time import monotonic
from uuid import uuid4

PRIORITY_SINGLE = 'single'
PRIORITY_BULK = 'bulk'
# 先に取る順。数字が小さいほど先
_PRIORITY_RANK = {PRIORITY_SINGLE: 0, PRIORITY_BULK: 1}


class PoolCapacityError(RuntimeError):
    """自サーバーの都合で取得しなかった（店舗の障害ではない）。受付件数の上限に達した。"""


class PoolBusyError(PoolCapacityError):
    """順番待ちが待ち時間の上限を超えた。混雑で取得できなかった。"""


class SearchAbandonedError(PoolCapacityError):
    """利用者が検索をやめた（画面を閉じた）ので、始まる前の取得を取り消した。"""


class _Identity:
    """同名関数や独自の等価比較を持つ callable を取り違えない識別子。"""

    def __init__(self, value):
        self.value = value

    def __hash__(self):
        return id(self.value)

    def __eq__(self, other):
        return isinstance(other, _Identity) and self.value is other.value


def _argument_key(value):
    # 可変オブジェクトは内容が同じでも共有しない。既存の max_pages は整数。
    if type(value) in (str, bytes, int, float, bool, type(None)):
        return type(value), value
    if type(value) is tuple:
        return tuple, tuple(_argument_key(v) for v in value)
    return _Identity(value)


def _callable_key(fn):
    if isinstance(fn, partial):
        return (
            partial, _callable_key(fn.func),
            tuple(_argument_key(v) for v in fn.args),
            tuple(sorted((k, _argument_key(v)) for k, v in (fn.keywords or {}).items())),
        )
    return _Identity(fn)


def iter_with_heartbeat(futures, interval):
    """終わった Future を順に返す。interval 秒なにも終わらなければ None を返す（生存確認の合図）。

    SSE の生成側が None のたびに小さなイベントを送ると、画面側の無応答タイムアウトを
    延ばせるうえ、利用者が切断したことを次の送信で早めに検知できる。
    """
    pending = set(futures)
    while pending:
        done, pending = _futures_wait(pending, timeout=interval, return_when=FIRST_COMPLETED)
        if not done:
            yield None
            continue
        for future in done:
            yield future


class _Job:
    """1件の店舗取得。待っている利用者（waiter）が全員いなくなったら、始まる前なら取り消す。"""

    def __init__(self, key, fn, card_name, runner, shop, accepted_at):
        self.key = key
        self.fn = fn
        self.card_name = card_name
        self.runner = runner
        self.shop = shop
        self.accepted_at = accepted_at
        self.state = 'queued'  # queued → running → done（取り消しも done）
        self.priority = None
        self.started_priority = None  # 実行を始めたときの優先度（待ち時間の集計に使う）
        self.owner = None  # 列の中で、どの検索の順番として並んでいるか
        self.waiters: dict[Future, '_Waiter'] = {}


class _Waiter:
    def __init__(self, job, request, deadline):
        self.job = job
        self.request = request
        self.deadline = deadline


def _new_stats():
    return {'count': 0, 'total': 0.0, 'max': 0.0}


class SearchRequest:
    """1回の検索（SSE 1本）。列の中で交互に回す単位で、待ち時間の上限もこの単位で決める。

    デッキ一括検索は、何枚のカードを調べても1つの SearchRequest を共有する。
    """

    def __init__(self, pool, priority, queue_timeout, request_id):
        self._pool = pool
        self.priority = priority
        self.queue_timeout = queue_timeout
        self.id = request_id
        self.closed = False
        self._waiters: set[Future] = set()

    def submit(self, fn, card_name: str, *, shop: str, runner) -> Future:
        """この検索の利用者専用の Future を返す。同じ取得をほかの検索と共有していても別の Future。"""
        return self._pool._submit(self, fn, card_name, shop, runner)

    def is_queued(self, future) -> bool:
        """まだ順番待ち（取得が始まっていない）なら True。"""
        return self._pool._is_queued(future)

    def as_completed(self, futures, heartbeat=None):
        """終わった Future を順に返す。待ち時間の上限を過ぎた順番待ちは PoolBusyError にして返す。

        heartbeat 秒なにも終わらなければ None を返す（None を受け取りたくなければ省略する）。
        取得が始まったものは、店舗取得そのもののタイムアウトに任せて最後まで待つ。
        """
        pending = set(futures)
        clock = self._pool._clock
        last_yield = clock()
        while pending:
            now = clock()
            timeout = None
            deadline = self._pool._earliest_deadline(pending)
            if deadline is not None:
                timeout = max(0.0, deadline - now)
            if heartbeat is not None:
                beat = max(0.0, last_yield + heartbeat - now)
                timeout = beat if timeout is None else min(timeout, beat)
            done, _ = _futures_wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:
                self._pool._expire_overdue(pending)
                done = {f for f in pending if f.done()}
            if done:
                for future in done:
                    pending.discard(future)
                    last_yield = clock()
                    yield future
            elif heartbeat is not None and clock() - last_yield >= heartbeat:
                last_yield = clock()
                yield None

    def close(self):
        """検索をやめる。まだ終わっていない取得の受け取りを取り消す（始まる前の取得は実行しない）。

        正常に全部受け取ったあとに呼んでも何も起きない。何度呼んでもよい。
        """
        self._pool._close_request(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


class ShopSearchPool:
    """店ごとの列で取得する共有プール。

    max_workers: 全体の同時取得数。per_shop_limit: 1店あたりの同時取得数。
    max_pending: 順番待ち＋実行中の取得（重複をまとめたあとの件数）の安全上限。
      通常は SSE の同時数で自然に頭打ちになるため、ここに達するのは異常時だけ。
    """

    def __init__(self, max_workers: int, per_shop_limit: int, max_pending: int, *,
                 clock=monotonic, logger=None, log_interval: float = 60.0):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('max_workers は1以上の整数で指定してください')
        if type(per_shop_limit) is not int or not 1 <= per_shop_limit <= max_workers:
            raise ValueError('per_shop_limit は1以上 max_workers 以下の整数で指定してください')
        if type(max_pending) is not int or max_pending < max_workers:
            raise ValueError('max_pending は max_workers 以上の整数で指定してください')
        if not isinstance(log_interval, (int, float)) or not math.isfinite(log_interval) or log_interval <= 0:
            raise ValueError('log_interval は正の有限秒数で指定してください')
        self._max_workers = max_workers
        self._per_shop_limit = per_shop_limit
        self._max_pending = max_pending
        self._lock = Lock()
        self._inflight: dict[tuple, _Job] = {}
        self._waiters: dict[Future, _Waiter] = {}
        # 店 → 優先度 → 検索ID → その検索の順番待ち。OrderedDict の先頭から1件ずつ交互に取る
        self._queues: dict[str, dict[str, OrderedDict]] = {}
        self._running_by_shop: dict[str, int] = {}
        self._running_total = 0
        self._shop_cursor = 0
        self._request_ids = count(1)
        self._closed = False
        # 計測は専用ロックで保護する。同期executorでも元の受付ロックへ再入しない。
        self._metrics_lock = Lock()
        self._clock = clock
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        # 60秒はログ出力量の制御。並列上限を校正した値ではない。
        self._log_interval = log_interval
        self._started_at = self._last_log_at = clock()
        self._process_id = os.getpid()
        self._instance_id = uuid4().hex
        self._started_at_utc = datetime.now(timezone.utc).isoformat()
        self._metrics = dict.fromkeys((
            'accepted', 'shared', 'capacity_rejected', 'shutdown_rejected',
            'completed', 'exceptions', 'cancelled', 'submission_errors',
            'busy_expired', 'abandoned',
            'running', 'pending', 'peak_running', 'peak_pending', 'log_errors',
        ), 0)
        self._wait = _new_stats()
        self._run_time = _new_stats()
        self._by_shop: dict[str, dict] = {}
        self._by_priority = {p: self._new_priority_metrics() for p in _PRIORITY_RANK}
        # 実行は空きがあるときだけ executor に渡すので、executor 側の列には溜まらない
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='shop-search',
        )

    # ── 受付 ──

    def request(self, priority: str, queue_timeout: float) -> SearchRequest:
        """検索1回ぶんの受付口を作る。queue_timeout は1件あたりの順番待ちの上限秒数。"""
        if priority not in _PRIORITY_RANK:
            raise ValueError(f'priority は {sorted(_PRIORITY_RANK)} のどれかで指定してください')
        if not isinstance(queue_timeout, (int, float)) or not math.isfinite(queue_timeout) or queue_timeout <= 0:
            raise ValueError('queue_timeout は正の有限秒数で指定してください')
        return SearchRequest(self, priority, float(queue_timeout), next(self._request_ids))

    def _submit(self, request, fn, card_name, shop, runner) -> Future:
        key = (_callable_key(fn), card_name, _callable_key(runner))
        waiter_future = Future()
        error = None
        to_start = []
        with self._lock:
            now = self._clock()
            if self._closed:
                self._increment('shutdown_rejected')
                error = RuntimeError('店舗検索プールは終了しています')
            elif request.closed:
                self._increment('abandoned')
                self._priority_increment(request.priority, 'abandoned')
                error = SearchAbandonedError('検索は取り消されました')
            else:
                job = self._inflight.get(key)
                if job is not None:
                    self._increment('shared')
                    self._priority_increment(request.priority, 'shared')
                elif len(self._inflight) >= self._max_pending:
                    self._increment('capacity_rejected')
                    error = PoolCapacityError('店舗検索の同時受付上限に達しました')
                else:
                    job = _Job(key, fn, card_name, runner, shop, now)
                    self._inflight[key] = job
                    with self._metrics_lock:
                        self._metrics['accepted'] += 1
                        self._metrics['pending'] += 1
                        self._metrics['peak_pending'] = max(self._metrics['peak_pending'], self._metrics['pending'])
                        self._shop_metrics(shop)['accepted'] += 1
                        self._by_priority[request.priority]['accepted'] += 1
                if error is None:
                    waiter = _Waiter(job, request, now + request.queue_timeout)
                    job.waiters[waiter_future] = waiter
                    self._waiters[waiter_future] = waiter
                    request._waiters.add(waiter_future)
                    if job.state == 'queued':
                        self._requeue_locked(job)
                        to_start = self._dispatch_locked()
            # 同じ取得をまとめた相手を監視の重複記録から外すための目印（app.py の健康記録が使う）
            if error is None:
                waiter_future.fetch_token = job

        self._start(to_start)
        self._maybe_log()
        if error is not None:
            raise error
        return waiter_future

    # ── 列の操作（すべて self._lock の中で呼ぶ） ──

    def _best_owner_locked(self, job):
        """待っている検索のうち、いちばん先に取るべき検索（優先度が高く、先に来たもの）。"""
        return min((w.request for w in job.waiters.values()),
                   key=lambda r: (_PRIORITY_RANK[r.priority], r.id))

    def _unlink_locked(self, job):
        if job.owner is None:
            return
        by_request = self._queues[job.shop][job.priority]
        line = by_request.get(job.owner.id)
        if line is not None:
            try:
                line.remove(job)
            except ValueError:
                pass
            if not line:
                del by_request[job.owner.id]
        job.owner = None
        job.priority = None

    def _requeue_locked(self, job):
        """待っている検索が変わったら、その中で最も先に取るべき検索の順番に並べ直す。"""
        owner = self._best_owner_locked(job)
        if job.owner is owner:
            return
        self._unlink_locked(job)
        queues = self._queues.setdefault(job.shop, {p: OrderedDict() for p in _PRIORITY_RANK})
        queues[owner.priority].setdefault(owner.id, deque()).append(job)
        job.owner = owner
        job.priority = owner.priority

    def _pick_locked(self):
        if self._running_total >= self._max_workers:
            return None
        shops = list(self._queues)
        if not shops:
            return None
        for priority in sorted(_PRIORITY_RANK, key=_PRIORITY_RANK.get):
            for offset in range(len(shops)):
                index = (self._shop_cursor + offset) % len(shops)
                shop = shops[index]
                if self._running_by_shop.get(shop, 0) >= self._per_shop_limit:
                    continue
                by_request = self._queues[shop][priority]
                if not by_request:
                    continue
                request_id, line = next(iter(by_request.items()))
                job = line.popleft()
                if line:
                    by_request.move_to_end(request_id)  # 次は別の検索の番
                else:
                    del by_request[request_id]
                job.owner = None
                # 次に空いたときは隣の店から探す（店のあいだでも偏らせない）
                self._shop_cursor = (index + 1) % len(shops)
                return job
        return None

    def _dispatch_locked(self):
        """空きのぶんだけ順番待ちを実行中にする。executor への投入はロックの外（_start）で行う。"""
        started = []
        while True:
            job = self._pick_locked()
            if job is None:
                return started
            job.state = 'running'
            job.started_priority = job.priority
            self._running_total += 1
            self._running_by_shop[job.shop] = self._running_by_shop.get(job.shop, 0) + 1
            with self._metrics_lock:
                self._metrics['running'] += 1
                self._metrics['peak_running'] = max(self._metrics['peak_running'], self._metrics['running'])
                shop_metrics = self._shop_metrics(job.shop)
                shop_metrics['peak_running'] = max(shop_metrics['peak_running'],
                                                   self._running_by_shop[job.shop])
            started.append(job)

    def _drop_job_locked(self, job):
        """始まる前の取得を取り消す（待っている人がいなくなった）。"""
        self._unlink_locked(job)
        job.state = 'done'
        if self._inflight.get(job.key) is job:
            del self._inflight[job.key]
        with self._metrics_lock:
            self._metrics['cancelled'] += 1
            self._metrics['pending'] -= 1

    def _detach_locked(self, future, error_cls, metric):
        """1人の利用者の受け取りを外す。外したら (future, error) を返す（ロック外で設定する）。"""
        waiter = self._waiters.pop(future, None)
        if waiter is None or future.done():
            return None
        job = waiter.job
        job.waiters.pop(future, None)
        waiter.request._waiters.discard(future)
        self._increment(metric)
        self._priority_increment(waiter.request.priority, metric)
        if metric == 'busy_expired':
            with self._metrics_lock:
                self._shop_metrics(job.shop)['busy_expired'] += 1
        if job.state == 'queued':
            if job.waiters:
                self._requeue_locked(job)
            else:
                self._drop_job_locked(job)
        return future, error_cls

    # ── 実行 ──

    def _start(self, jobs):
        for job in jobs:
            try:
                self._executor.submit(self._run, job)
            except BaseException as exc:
                self._finish(job, error=exc, submission_error=True)

    def _run(self, job):
        started_at = self._clock()
        waited = max(0.0, started_at - job.accepted_at)
        with self._metrics_lock:
            self._observe(self._wait, waited)
            self._observe(self._shop_metrics(job.shop)['wait_seconds'], waited)
            self._observe(self._by_priority[job.started_priority]['wait_seconds'], waited)
        failed = False
        result = error = None
        try:
            # runnerは計測ロックの外で呼ぶ。例外や入力内容は集計・ログに格納しない。
            result = job.runner(job.fn, job.card_name)
        except BaseException as exc:
            failed = True
            error = exc
        finally:
            finished_at = self._clock()
            with self._metrics_lock:
                self._observe(self._run_time, max(0.0, finished_at - started_at))
                self._metrics['completed'] += 1
                self._metrics['exceptions'] += int(failed)
                self._shop_metrics(job.shop)['completed'] += 1
        self._finish(job, result=result, error=error)

    def _finish(self, job, result=None, error=None, submission_error=False):
        with self._lock:
            job.state = 'done'
            self._running_total -= 1
            self._running_by_shop[job.shop] -= 1
            if self._inflight.get(job.key) is job:
                del self._inflight[job.key]
            waiters = list(job.waiters)
            job.waiters.clear()
            for future in waiters:
                waiter = self._waiters.pop(future, None)
                if waiter is not None:
                    waiter.request._waiters.discard(future)
            with self._metrics_lock:
                self._metrics['running'] -= 1
                self._metrics['pending'] -= 1
                if submission_error:
                    self._metrics['submission_errors'] += 1
            to_start = [] if self._closed else self._dispatch_locked()
        # 結果の配布はロックの外。待っている側の処理がロックへ再入しても詰まらない。
        for future in waiters:
            if future.done():
                continue
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)
        self._start(to_start)
        self._maybe_log()

    # ── 待つ側から呼ぶもの ──

    def _is_queued(self, future):
        with self._lock:
            waiter = self._waiters.get(future)
            return waiter is not None and waiter.job.state == 'queued'

    def _earliest_deadline(self, futures):
        with self._lock:
            deadlines = [w.deadline for w in (self._waiters.get(f) for f in futures)
                         if w is not None and w.job.state == 'queued']
        return min(deadlines) if deadlines else None

    def _expire_overdue(self, futures):
        now = self._clock()
        expired = []
        with self._lock:
            for future in futures:
                waiter = self._waiters.get(future)
                if waiter is None or waiter.job.state != 'queued' or waiter.deadline > now:
                    continue
                item = self._detach_locked(future, PoolBusyError, 'busy_expired')
                if item is not None:
                    expired.append(item)
        for future, error_cls in expired:
            future.set_exception(error_cls('混雑で店舗の取得を待ちきれませんでした'))
        if expired:
            self._maybe_log()

    def _close_request(self, request):
        detached = []
        with self._lock:
            request.closed = True
            for future in list(request._waiters):
                item = self._detach_locked(future, SearchAbandonedError, 'abandoned')
                if item is not None:
                    detached.append(item)
        for future, error_cls in detached:
            future.set_exception(error_cls('検索は取り消されました'))
        if detached:
            self._maybe_log()

    # ── 計測 ──

    @staticmethod
    def _new_priority_metrics():
        return {'accepted': 0, 'shared': 0, 'busy_expired': 0, 'abandoned': 0,
                'wait_seconds': _new_stats()}

    def _shop_metrics(self, shop):
        # self._metrics_lock の中で呼ぶ
        metrics = self._by_shop.get(shop)
        if metrics is None:
            metrics = self._by_shop[shop] = {
                'accepted': 0, 'completed': 0, 'busy_expired': 0, 'peak_running': 0,
                'wait_seconds': _new_stats(),
            }
        return metrics

    def _increment(self, name):
        with self._metrics_lock:
            self._metrics[name] += 1

    def _priority_increment(self, priority, name):
        with self._metrics_lock:
            self._by_priority[priority][name] += 1

    @staticmethod
    def _observe(target, seconds):
        target['count'] += 1
        target['total'] += seconds
        target['max'] = max(target['max'], seconds)

    def _snapshot_locked(self, now):
        by_shop = {}
        for shop, metrics in self._by_shop.items():
            by_shop[shop] = {**metrics, 'wait_seconds': dict(metrics['wait_seconds']),
                             'running': self._running_by_shop.get(shop, 0)}
        return {
            'event': 'shop_search_pool', 'scope': 'process',
            # PID再利用・worker再起動・同一プロセス内の別poolを識別して累積差分を取る。
            'pid': self._process_id, 'instance_id': self._instance_id,
            'started_at_utc': self._started_at_utc,
            'uptime_seconds': max(0.0, now - self._started_at),
            'max_workers': self._max_workers, 'per_shop_limit': self._per_shop_limit,
            'max_pending': self._max_pending,
            **self._metrics,
            'queued': self._metrics['pending'] - self._metrics['running'],
            'wait_seconds': dict(self._wait), 'run_seconds': dict(self._run_time),
            'by_shop': by_shop,
            'by_priority': {p: {**m, 'wait_seconds': dict(m['wait_seconds'])}
                            for p, m in self._by_priority.items()},
        }

    def snapshot(self):
        """起動後の累積集計。完了は例外を含み、待機時間の母数は実行開始した仕事。

        accepted は新しく受け付けた取得の数（重複をまとめた分は shared）。
        pending = accepted - completed - cancelled - submission_errors。
        busy_expired・abandoned は利用者単位（共有中の取得から1人が外れたら1）。
        入力ごとの履歴を保持しない。店名は固定の数店ぶんなので集計領域はほぼ一定。
        """
        now = self._clock()
        with self._metrics_lock:
            return self._snapshot_locked(now)

    def _maybe_log(self):
        now = self._clock()
        with self._metrics_lock:
            if now - self._last_log_at < self._log_interval:
                return
            self._last_log_at = now
            snapshot = self._snapshot_locked(now)
        # loggerもロック外。活動のない間にタイマースレッドを作って出力しない。
        try:
            self._logger.info('%s', json.dumps(snapshot, ensure_ascii=True))
        except Exception:
            # 計測出力の失敗で検索結果を失わない。失敗件数を残し、次の出力でも確認可能にする。
            self._increment('log_errors')

    def shutdown(self, wait: bool = True):
        detached = []
        with self._lock:
            self._closed = True
            # 始まっていない取得は実行しない。待っている人には取り消しを返す
            for future in list(self._waiters):
                waiter = self._waiters[future]
                if waiter.job.state == 'queued':
                    item = self._detach_locked(future, SearchAbandonedError, 'abandoned')
                    if item is not None:
                        detached.append(item)
        for future, error_cls in detached:
            future.set_exception(error_cls('店舗検索プールは終了しています'))
        self._executor.shutdown(wait=wait)
        self._maybe_log()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown()
