"""プロセス内の店舗検索を共有し、実行数と待機数を制限する。

複数のサーバープロセス間では共有されない。戻り値の Future と検索結果は
同時利用者間で共有するため、呼び出し元では cancel や結果の直接変更をしない。
上限は呼び出し側が既存の並列枠を根拠に指定し、実測で校正する。
"""
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from threading import Lock


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

    def __init__(self, max_workers: int, max_pending: int):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError('max_workers は1以上の整数で指定してください')
        if type(max_pending) is not int or max_pending < max_workers:
            raise ValueError('max_pending は max_workers 以上の整数で指定してください')
        self._max_pending = max_pending
        self._lock = Lock()
        self._inflight: dict[tuple, Future] = {}
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='shop-search',
        )

    def submit(self, fn, card_name: str, *, runner) -> Future:
        key = (_callable_key(fn), card_name, _callable_key(runner))
        with self._lock:
            if self._closed:
                raise RuntimeError('店舗検索プールは終了しています')
            # 完了コールバックがロックを待っていても、その枠を再利用する。
            for old_key, old_future in list(self._inflight.items()):
                if old_future.done():
                    del self._inflight[old_key]
            if key in self._inflight:
                return self._inflight[key]
            if len(self._inflight) >= self._max_pending:
                raise PoolCapacityError('店舗検索の同時受付上限に達しました')
            future = self._executor.submit(runner, fn, card_name)
            self._inflight[key] = future

        # 完了済み Future では callback が同期実行されるので、必ずロック外で登録する。
        future.add_done_callback(lambda completed: self._remove(key, completed))
        return future

    def _remove(self, key, future):
        with self._lock:
            # 同じキーの次の検索が始まっていたら、その Future は削除しない。
            if self._inflight.get(key) is future:
                del self._inflight[key]

    def shutdown(self, wait: bool = True):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=wait)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.shutdown()
