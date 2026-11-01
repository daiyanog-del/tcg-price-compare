"""お店ごとの1日の取得回数を DB（shop_request_daily）に残す（2026-11-01）。

まんぞく屋が本番の接続元を止めた（11-01）のは、機能ごとには正しい取得の合計を誰も見ていなかったのが根。
scraper.safe_get などが数えているホストごとの回数（scraper.get_host_request_counts）を、出どころ
（毎朝の収集 prices／買取 buyback／新弾の収集 featured／サイトの検索 web）ごと・JST の日ごとに書く。

- プロセスごとに1行ずつ（day, source, instance_id, host）。読むときに足し合わせる
- 回数はプロセスの起動からの累積なので、前回見た値との差を、見たときの日付に足す（日付の境目は最大1分ずれる）
- web は60秒ごとに見て、10分ごとに書く。05〜10時 JST（朝の収集中）は1時間ごと（1回は十数行の upsert で軽いが、
  DB の IO を空けたい時間なので間をあける）。メモリ不足の強制終了などで落ちると、最後に書いてからの分は消える。
  正常に終了できたとき（atexit）は時刻にかかわらず書く（検索の接続が残って gunicorn に強制終了されると届かない）
- 収集のジョブは終了時（atexit）に1回だけ書く。途中で打ち切って sys.exit した回も残る
- 表が無い・DB が止まっていても本体を止めない（warning のログだけ）
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from constants import JST

logger = logging.getLogger(__name__)

TABLE = "shop_request_daily"
TICK_SEC = 60
FLUSH_INTERVAL_SEC = 10 * 60
QUIET_FLUSH_INTERVAL_SEC = 60 * 60  # 朝の収集中（QUIET_WRITE_HOURS）の書く間隔
QUIET_WRITE_HOURS = (5, 10)  # 間をあける時間（JST の時。start <= 時 < end）。upgrade_signals.QUIET_WRITE_HOURS と同じ
STORE_TIMEOUT_SEC = 10  # 書き込み専用の接続のタイムアウト（DB が止まっていても終了を長く待たせない）
KEEP_DAYS = 400  # これより古い行は毎朝の収集が消す（1日数十行。季節での比較に1年ぶん残す）


def _jst_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, JST).strftime("%Y-%m-%d")


class DailyHostCounter:
    """累積の回数（{host: {"requests": n, "skipped": m}}）を見て、日ごとの回数に積む。"""

    def __init__(self, source: str, *, clock: Callable[[], float] = time.time, instance_id: str | None = None):
        self.source = source
        self._clock = clock
        self.instance_id = instance_id or f"{os.getpid()}-{int(clock())}"
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()  # 見張りのスレッドと終了時の書き込みを重ねない（古い写しが後から届かないように）
        self._prev: dict[str, dict] = {}
        self._days: dict[str, dict[str, dict]] = {}  # {day: {host: {"requests": n, "skipped": m}}}

    def observe(self, counts: dict):
        day = _jst_day(self._clock())
        with self._lock:
            for host, c in (counts or {}).items():
                prev = self._prev.get(host) or {}
                bucket = self._days.setdefault(day, {}).setdefault(host, {"requests": 0, "skipped": 0})
                for key in ("requests", "skipped"):
                    now_v = int(c.get(key) or 0)
                    # 累積が減った（reset_host_request_counts が呼ばれた）ときは今の値をそのまま差とする
                    delta = now_v - int(prev.get(key) or 0)
                    bucket[key] += now_v if delta < 0 else delta
                self._prev[host] = {"requests": int(c.get("requests") or 0), "skipped": int(c.get("skipped") or 0)}

    def rows(self) -> list[dict]:
        with self._lock:
            return [{"day": day, "source": self.source, "instance_id": self.instance_id, "host": host,
                     "requests": c["requests"], "skipped": c["skipped"]}
                    for day, hosts in sorted(self._days.items()) for host, c in sorted(hosts.items())]

    def flush(self, store) -> bool:
        """持っている日の行をすべて upsert する（その日のこのプロセスの累計なので、何度書いても同じ）。
        書けたら今日より前の日は手放す。"""
        with self._flush_lock:
            return self._flush_locked(store)

    def _flush_locked(self, store) -> bool:
        rows = self.rows()
        if store is None or not rows:
            return True
        try:
            store.table(TABLE).upsert(
                [{**r, "updated_at": datetime.now(JST).isoformat()} for r in rows],
                on_conflict="day,source,instance_id,host",
            ).execute()
        except Exception as e:
            logger.warning("shop_request_log: 取得回数の書き込みに失敗（%s・%d行）: %s", self.source, len(rows), e)
            return False
        today = _jst_day(self._clock())
        written = {(r["day"], r["host"]): (r["requests"], r["skipped"]) for r in rows}
        with self._lock:
            for day in [d for d in self._days if d < today]:
                # 写しを取ってから足された分（日付の変わり目の差）があれば、次に書くまで持ち続ける
                if all(written.get((day, h)) == (c["requests"], c["skipped"]) for h, c in self._days[day].items()):
                    self._days.pop(day, None)
        return True


def _host_counts() -> dict:
    import scraper  # app.py・収集のジョブはどちらも読み込み済み。テストで差し替えやすいよう呼ぶときに読む
    return scraper.get_host_request_counts()


def make_store():
    """書き込み専用の Supabase クライアント（短いタイムアウト）。環境変数が無い・作れなければ None。"""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_KEY")
    if not url or not key:
        return None
    try:
        from supabase import create_client
        from supabase.lib.client_options import SyncClientOptions
        return create_client(url, key, options=SyncClientOptions(postgrest_client_timeout=STORE_TIMEOUT_SEC))
    except Exception:
        logger.exception("shop_request_log: 書き込み用の Supabase クライアントを作れませんでした")
        return None


def install_job(source: str, store, *, get_counts: Callable[[], dict] = _host_counts,
                prune: bool = False) -> DailyHostCounter:
    """収集のジョブ用。終了時に1回だけ書く。prune=True（毎朝の収集）なら KEEP_DAYS より古い行も消す。"""
    counter = DailyHostCounter(source)

    def _at_exit():
        try:
            counter.observe(get_counts())
            if counter.flush(store) and prune and store is not None:
                cutoff = (datetime.now(JST) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
                store.table(TABLE).delete().lt("day", cutoff).execute()
            for r in counter.rows():
                print(f"  [取得回数] {r['host']}: {r['requests']}回（ブレーカで飛ばした {r['skipped']}回）")
        except Exception as e:
            logger.warning("shop_request_log: 終了時の書き込みに失敗（%s）: %s", source, e)

    atexit.register(_at_exit)
    return counter


def _in_quiet_hours(ts: float) -> bool:
    start, end = QUIET_WRITE_HOURS
    return start <= datetime.fromtimestamp(ts, JST).hour < end


def run_web_forever(counter: DailyHostCounter, store, *, get_counts: Callable[[], dict] = _host_counts,
                    sleep=time.sleep, clock: Callable[[], float] = time.time):
    last_flush = clock()
    while True:
        sleep(TICK_SEC)
        try:
            counter.observe(get_counts())
            now = clock()
            interval = QUIET_FLUSH_INTERVAL_SEC if _in_quiet_hours(now) else FLUSH_INTERVAL_SEC
            if now - last_flush >= interval:
                # 失敗しても次は間隔のあと（止まった DB に要求を重ねない。行は手元に残り、次に書ける）
                counter.flush(store)
                last_flush = now
        except Exception:
            logger.exception("shop_request_log: 取得回数の見張りの1周に失敗")


def start_web(store) -> DailyHostCounter | None:
    """app.py（web）用。60秒ごとに見て10分ごと（朝の収集中は1時間ごと）に書くスレッドを起こし、正常終了時にも書く。"""
    if store is None:
        logger.info("shop_request_log: Supabase 未設定のため、取得回数は書きません")
        return None
    try:
        counter = DailyHostCounter("web")

        def _at_exit():
            counter.observe(_host_counts())
            counter.flush(store)

        atexit.register(_at_exit)
        threading.Thread(target=run_web_forever, args=(counter, store), daemon=True,
                         name="shop-request-log").start()
        return counter
    except Exception:
        logger.exception("shop_request_log: 取得回数の見張りを起動できませんでした")
        return None
