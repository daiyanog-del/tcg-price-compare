"""プロセス内の店舗検索を共有し、実行数と待機数を制限する。

複数のサーバープロセス間では共有されない。戻り値の Future と検索結果は
同時利用者間で共有するため、呼び出し元では cancel や結果の直接変更をしない。
上限は呼び出し側が既存の並列枠を根拠に指定し、実測で校正する。
"""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from functools import partial
import json
import logging
import math
import os
from threading import Lock
from time import monotonic
from uuid import uuid4


class PoolCapacityError(RuntimeError):
    """実行中と待機中の合計が上限に達した。呼び出し元で取得失敗として扱う。"""


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


class ShopSearchPool:
    """max_pending は実行中も含む合計上限。満杯でも重複依頼は受け付ける。"""

    def __init__(self, max_workers: int, max_pending: int, *, clock=monotonic,
                 logger=None, log_interval: float = 60.0):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('max_workers は1以上の整数で指定してください')
        if type(max_pending) is not int or max_pending < max_workers:
            raise ValueError('max_pending は max_workers 以上の整数で指定してください')
        if not isinstance(log_interval, (int, float)) or not math.isfinite(log_interval) or log_interval <= 0:
            raise ValueError('log_interval は正の有限秒数で指定してください')
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._lock = Lock()
        self._inflight: dict[tuple, Future] = {}
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
            'running', 'pending', 'peak_running', 'peak_pending', 'log_errors',
        ), 0)
        self._wait = {'count': 0, 'total': 0.0, 'max': 0.0}
        self._run_time = {'count': 0, 'total': 0.0, 'max': 0.0}
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='shop-search',
        )

    def submit(self, fn, card_name: str, *, runner) -> Future:
        key = (_callable_key(fn), card_name, _callable_key(runner))
        error = None
        is_new = False
        with self._lock:
            if self._closed:
                self._increment('shutdown_rejected')
                error = RuntimeError('店舗検索プールは終了しています')
            else:
                # 完了コールバックがロックを待っていても、その枠を再利用する。
                for old_key, old_future in list(self._inflight.items()):
                    if old_future.done():
                        del self._inflight[old_key]
                if key in self._inflight:
                    self._increment('shared')
                    future = self._inflight[key]
                elif len(self._inflight) >= self._max_pending:
                    self._increment('capacity_rejected')
                    error = PoolCapacityError('店舗検索の同時受付上限に達しました')
                else:
                    accepted_at = self._clock()
                    with self._metrics_lock:
                        self._metrics['accepted'] += 1
                        self._metrics['pending'] += 1
                        self._metrics['peak_pending'] = max(self._metrics['peak_pending'], self._metrics['pending'])
                    try:
                        future = self._executor.submit(self._run, runner, fn, card_name, accepted_at)
                    except BaseException as exc:
                        with self._metrics_lock:
                            self._metrics['submission_errors'] += 1
                            self._metrics['pending'] -= 1
                        error = exc
                    else:
                        self._inflight[key] = future
                        is_new = True

        # 完了済み Future では callback が同期実行されるので、必ずロック外で登録する。
        if is_new:
            future.add_done_callback(lambda completed: self._remove(key, completed))
        self._maybe_log()
        if error is not None:
            raise error
        return future

    def _remove(self, key, future):
        if future.cancelled():
            with self._metrics_lock:
                self._metrics['cancelled'] += 1
                self._metrics['pending'] -= 1
        with self._lock:
            # 同じキーの次の検索が始まっていたら、その Future は削除しない。
            if self._inflight.get(key) is future:
                del self._inflight[key]
        self._maybe_log()

    def _increment(self, name):
        with self._metrics_lock:
            self._metrics[name] += 1

    @staticmethod
    def _observe(target, seconds):
        target['count'] += 1
        target['total'] += seconds
        target['max'] = max(target['max'], seconds)

    def _run(self, runner, fn, card_name, accepted_at):
        started_at = self._clock()
        with self._metrics_lock:
            self._observe(self._wait, max(0.0, started_at - accepted_at))
            self._metrics['running'] += 1
            self._metrics['peak_running'] = max(self._metrics['peak_running'], self._metrics['running'])
        failed = False
        try:
            # runnerは計測ロックの外で呼ぶ。例外や入力内容は集計・ログに格納しない。
            return runner(fn, card_name)
        except BaseException:
            failed = True
            raise
        finally:
            finished_at = self._clock()
            with self._metrics_lock:
                self._observe(self._run_time, max(0.0, finished_at - started_at))
                self._metrics['completed'] += 1
                self._metrics['exceptions'] += int(failed)
                self._metrics['running'] -= 1
                self._metrics['pending'] -= 1

    def _snapshot_locked(self, now):
        return {
            'event': 'shop_search_pool', 'scope': 'process',
            # PID再利用・worker再起動・同一プロセス内の別poolを識別して累積差分を取る。
            'pid': self._process_id, 'instance_id': self._instance_id,
            'started_at_utc': self._started_at_utc,
            'uptime_seconds': max(0.0, now - self._started_at),
            'max_workers': self._max_workers, 'max_pending': self._max_pending,
            **self._metrics,
            'queued': self._metrics['pending'] - self._metrics['running'],
            'wait_seconds': dict(self._wait), 'run_seconds': dict(self._run_time),
        }

    def snapshot(self):
        """起動後の累積集計。完了は例外を含み、待機時間の母数は実行開始した仕事。

        accepted は受付枠を確保した数（executor投入失敗も含む）。
        pending = accepted - completed - cancelled - submission_errors。
        入力ごとの履歴・ラベルを保持せず、件数が増えても集計領域は一定。
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
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)
        self._maybe_log()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown()
