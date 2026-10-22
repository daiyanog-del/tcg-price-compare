"""有料プランへ上げる合図の見張り（2026-10-22）。

利用者がまだ少ないので今は0円の手で進め、数字が合図を超えたら有料にする方針（docs/decisions.md）。
その合図を Discord に知らせる。サーバーの中で常駐して60秒ごとに見る。

1. すぐ知らせる通知（同じ知らせは60分に1回まで。monitor.py の ALERT_COOLDOWN と同じ考え方）
   - DB が応答しない（タイムアウト・5xx・通信断）が続けて起きる
   - DB の遅い応答が続く
   - 取得口が受付を断る・順番待ちが上限を超える・検索の同時数で混雑応答を返す状態が続く
   - メモリが合図（400MB）を超えた
2. 1日1回のまとめ（JST の日ごと。合図を超えた日だけ送る）
   - 単品検索の順番待ちの p95（合図 10秒超 → 同時数の調整）
   - メモリの山（合図 400MB 超 → Render Standard）
   - DB のタイムアウト・遅い応答の件数（→ Supabase Pro）
   - Key Value があふれた件数は、Key Value がまだ無いので省いている。入ったら INFO の evicted_keys を
     SignalMonitor.tick で読み、DailyStats に足す
   集計はサーバーの中だけに持つと再起動で消えるので、プロセスごとに1日1行を Supabase に書く
   （upgrade_signals.sql）。05〜10時 JST は書かない（朝の収集と DB の負荷が重なる時間。
   2026-10-22 はこの時間に IO の余力が切れて約1.5時間止まった）。

失敗の扱い（_cache_warmer と同じく先に決めておく）:
  - 見張りの1周が例外で失敗しても、ログに残して次の周へ進む。続けて TICK_FAIL_ALERT 回失敗したら
    見張り自体の不調を1回だけ知らせる（60分に1回まで）
  - Supabase への書き込みが失敗したら、手元の集計を残したまま 5分→10分→20分→30分と間をあけて書き直す
    （止まった DB に毎分要求を重ねない）。日付が変わったときに書けなかった前日分も手元に残し、
    前日のまとめでは手元の値を使う（DB が止まった日こそ合図を出す日なので、書けないまま「合図なし」にしない）
  - メモリ不足で強制終了されると手元の集計は消える。最初の書き込みを起動2分後にし、メモリが合図を
    超えたらその場で知らせて書く。デプロイなどで正常に終了できたときは atexit で最後の分を書く
    （gunicorn が待ち切れずに強制終了したときは書けない）
  - Supabase が未設定なら、すぐ知らせる通知だけ動かし、1日のまとめは出さない（起動時にログ1行）
  - スレッドを起動できなかったらログに残して先へ進む（サイトの表示は止めない）
  - DB の見張りを差し込めない（ライブラリの版で形が変わった）接続は、ログに1回出して数えない
"""
from __future__ import annotations

import atexit
import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Callable

import httpx

import discord_notify
from constants import JST
from db_write_guard import _HTTPX_UNAVAILABLE

logger = logging.getLogger(__name__)

TICK_SEC = 60
ALERT_COOLDOWN_MINUTES = 60  # 同じ知らせは60分に1回まで（monitor.py と同じ）
TICK_FAIL_ALERT = 10  # 見張りの1周が続けてこの回数失敗したら知らせる

# ── すぐ知らせる通知のしきい値 ──
# 平常時の DB 応答は平均 0.2〜0.4 秒（db_write_guard.py の実測メモ）。起動直後の重い集計は 2〜7 秒かかることがある。
DB_SLOW_SEC = 5.0  # これ以上かかった応答を「遅い」と数える  # TODO: calibrate from data
DB_FAIL_STREAK_ALERT = 3  # DB が応答しない失敗がこの回数続いたら知らせる（間に成功があれば数え直す）  # TODO: calibrate from data
WINDOW_MINUTES = 5  # 「続く」を見る幅（直近の分数）
DB_SLOW_MINUTES_ALERT = 3  # 直近 WINDOW_MINUTES 分のうち遅い応答があった分がこの数以上  # TODO: calibrate from data
POOL_BUSY_MINUTES_ALERT = 3  # 直近 WINDOW_MINUTES 分のうち取得口の断り・待ち切れがあった分がこの数以上  # TODO: calibrate from data

# ── 1日のまとめの合図 ──
SIGNAL_WAIT_P95_SEC = 10.0  # 単品検索の順番待ちの p95（ユーザーと決めた合図）
SIGNAL_MEMORY_MB = 400.0  # メモリの山（ユーザーと決めた合図。Starter は 512MB）
SIGNAL_DB_FAILURES = 10  # 1日の DB の応答なし（タイムアウト・5xx・通信断）  # TODO: calibrate from data
SIGNAL_DB_SLOW = 50  # 1日の DB の遅い応答  # TODO: calibrate from data
SIGNAL_WAIT_MIN_COUNT = 100  # p95 を判定する最低件数（少ない日は1件の遅れで合図になるため）  # TODO: calibrate from data

# 単品検索の待ち時間の区切り（秒）。p95 は区切りの単位でしか出ないので、合図の 10 秒は必ず区切りに入れる。
# 最後の区切り（45秒）は単品の順番待ちの上限（app.py SHOP_QUEUE_TIMEOUT_SINGLE）。待ち切れは上限超えの箱に入れる
WAIT_BUCKETS = (0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30, 45)

# 書き込みを避ける時間（JST の時。start <= 時 < end）
QUIET_WRITE_HOURS = (5, 10)
FLUSH_INTERVAL_SEC = 30 * 60  # 1日の集計を Supabase に書く間隔
FIRST_FLUSH_SEC = 2 * 60  # 起動から最初に書くまで
FLUSH_RETRY_SEC = 5 * 60  # 書き込みに失敗したら、この秒数から倍々に間をあけて書き直す（上限 FLUSH_INTERVAL_SEC）
SUMMARY_AFTER = (0, 10)  # 前日のまとめを出す時刻（JST の 0:10 以降。各プロセスの日替わりの書き込みを待つ）
KEEP_DAYS = 90  # これより古い行は消す

DAILY_TABLE = "upgrade_signal_daily"
SUMMARY_TABLE = "upgrade_signal_summaries"

# 打つ手と費用の目安（2026-10-22 時点）
ACTIONS = {
    "wait_p95": "取得口の同時数の調整（0円。店の WAF の反応を見ながら少しずつ）",
    "memory": "Render Standard へ（1 CPU・2GB。月 +$18 程度）",
    "db": "Supabase Pro へ（月 $25）",
}


def _jst_now(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, JST)


def _in_quiet_hours(ts: float) -> bool:
    start, end = QUIET_WRITE_HOURS
    return start <= _jst_now(ts).hour < end


# ── メモリ ──

def _read_int(path: str):
    try:
        with open(path, encoding="ascii") as f:
            text = f.read().strip()
        return None if text == "max" else int(text)
    except (OSError, ValueError):
        return None


def _read_stat(path: str, key: str):
    try:
        with open(path, encoding="ascii") as f:
            for line in f:
                name, _, value = line.partition(" ")
                if name == key:
                    return int(value)
    except (OSError, ValueError):
        return None
    return None


def read_memory_mb(root: str = "/") -> tuple[float | None, str]:
    """コンテナのメモリ使用量（MB）と、どこから読んだか。

    ページキャッシュ（inactive_file）は空きとして扱えるので差し引く（Render の表示に近い working set）。
    cgroup v2 → cgroup v1 → 自プロセスの RSS の順に試す。読めなければ (None, "")。
    """
    v2 = os.path.join(root, "sys/fs/cgroup")
    current = _read_int(os.path.join(v2, "memory.current"))
    if current is not None:
        inactive = _read_stat(os.path.join(v2, "memory.stat"), "inactive_file") or 0
        return max(0, current - inactive) / 1024 / 1024, "cgroup v2"
    v1 = os.path.join(root, "sys/fs/cgroup/memory")
    usage = _read_int(os.path.join(v1, "memory.usage_in_bytes"))
    if usage is not None:
        inactive = _read_stat(os.path.join(v1, "memory.stat"), "total_inactive_file") or 0
        return max(0, usage - inactive) / 1024 / 1024, "cgroup v1"
    try:
        with open(os.path.join(root, "proc/self/status"), encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024, "VmRSS"
    except (OSError, ValueError, IndexError):
        pass
    return None, ""


# ── DB の応答を数える（Supabase クライアントの通信部分に差し込む） ──

class _TimedTransport(httpx.BaseTransport):
    """postgrest の httpx の通信部分を包み、応答までの時間と失敗を見張りへ渡す。

    応答の中身は読まない（PostgREST は問い合わせが終わってから返すので、見出しまでの時間≒DB の処理時間）。
    数える失敗は db_write_guard.is_db_unavailable と同じ系統: 通信のタイムアウト・接続失敗・切断、5xx
    （statement timeout 57014 は 500、PGRST002 は 503、Cloudflare の 504/522）。4xx は DB の異常ではないので数えない。
    """

    def __init__(self, inner: httpx.BaseTransport, on_response: Callable[[float, bool], None]):
        self._inner = inner
        self._on_response = on_response

    def handle_request(self, request):
        start = time.monotonic()
        try:
            response = self._inner.handle_request(request)
        except _HTTPX_UNAVAILABLE:  # db_write_guard と同じ系統（http2 の切断 RemoteProtocolError を含む）
            self._notify(time.monotonic() - start, True)
            raise
        self._notify(time.monotonic() - start, response.status_code >= 500)
        return response

    def _notify(self, elapsed, failed):
        try:
            self._on_response(elapsed, failed)
        except Exception:  # 見張りの不具合で DB の要求を失敗させない
            logger.exception("upgrade_signals: DB 応答の記録に失敗")

    def close(self):
        self._inner.close()


def install_db_watch(client, on_response) -> bool:
    """Supabase クライアントの postgrest の通信部分に見張りを差し込む。差し込めた（済みを含む）なら True。

    httpx.Client の非公開属性（_transport・_mounts）を使うので、ライブラリの版で形が変わったら
    差し込まずに False を返す（見張りが DB の件数を数えなくなるだけで、サイトは動く）。
    """
    if client is None:
        return False
    try:
        session = client.postgrest.session
        transport = getattr(session, "_transport", None)
        if not isinstance(transport, httpx.BaseTransport):
            return False
        if not isinstance(transport, _TimedTransport):
            session._transport = _TimedTransport(transport, on_response)
        # 環境変数のプロキシ設定があると、URL ごとの通信部分（_mounts）が先に使われる
        mounts = getattr(session, "_mounts", None)
        if isinstance(mounts, dict):
            for pattern, mounted in list(mounts.items()):
                if isinstance(mounted, httpx.BaseTransport) and not isinstance(mounted, _TimedTransport):
                    mounts[pattern] = _TimedTransport(mounted, on_response)
        return True
    except Exception:
        logger.exception("upgrade_signals: DB の見張りを差し込めませんでした")
        return False


# ── 1日の集計 ──

class DailyStats:
    """JST の1日ぶんの集計。プロセスごとに持ち、Supabase には jsonb で1行書く。足し合わせられる形にする。"""

    def __init__(self, day: str):
        self.day = day
        self.wait_hist = [0] * (len(WAIT_BUCKETS) + 1)  # 最後は上限超え（待ち切れ）
        self.memory_peak_mb = 0.0
        self.memory_source = ""
        self.db_requests = 0
        self.db_failures = 0
        self.db_slow = 0
        self.pool_capacity_rejected = 0
        self.pool_single_busy_expired = 0
        self.search_busy = 0

    def add_wait(self, seconds: float):
        for i, edge in enumerate(WAIT_BUCKETS):
            if seconds <= edge:
                self.wait_hist[i] += 1
                return
        self.wait_hist[-1] += 1

    def to_json(self) -> dict:
        return {
            "wait_hist": list(self.wait_hist), "wait_buckets": list(WAIT_BUCKETS),
            "memory_peak_mb": round(self.memory_peak_mb, 1), "memory_source": self.memory_source,
            "db_requests": self.db_requests, "db_failures": self.db_failures, "db_slow": self.db_slow,
            "pool_capacity_rejected": self.pool_capacity_rejected,
            "pool_single_busy_expired": self.pool_single_busy_expired,
            "search_busy": self.search_busy,
        }

    @classmethod
    def merge(cls, day: str, rows: list[dict]) -> "DailyStats":
        total = cls(day)
        for row in rows:
            try:
                total._add_row(row, day)
            except (TypeError, ValueError, AttributeError) as e:
                logger.warning("upgrade_signals: 読めない集計の行を飛ばしました（%s）: %s", day, e)
        return total

    def _add_row(self, row: dict, day: str):
        """1行を足す。先に全部を読んでから足すので、途中で読めない値があっても半端に足さない。"""
        hist = [int(b) for b in (row.get("wait_hist") or [])]
        same_buckets = list(row.get("wait_buckets") or []) == list(WAIT_BUCKETS) and len(hist) == len(self.wait_hist)
        peak = float(row.get("memory_peak_mb") or 0)
        counts = {name: int(row.get(name) or 0) for name in (
            "db_requests", "db_failures", "db_slow", "pool_capacity_rejected",
            "pool_single_busy_expired", "search_busy")}
        if same_buckets:
            self.wait_hist = [a + b for a, b in zip(self.wait_hist, hist)]
        elif hist:
            logger.warning("upgrade_signals: 待ち時間の区切りが違う行を飛ばしました（%s）", day)
        if peak > self.memory_peak_mb:
            self.memory_peak_mb = peak
            self.memory_source = row.get("memory_source") or ""
        for name, value in counts.items():
            setattr(self, name, getattr(self, name) + value)

    def wait_count(self) -> int:
        return sum(self.wait_hist)

    def wait_p95_bucket(self):
        """p95 が入る区切りを (下端, 上端) で返す。上端 None は上限超え。件数0なら None。"""
        total = self.wait_count()
        if total == 0:
            return None
        need = 0.95 * total
        seen = 0
        for i, n in enumerate(self.wait_hist):
            seen += n
            if seen >= need:
                low = 0.0 if i == 0 else float(WAIT_BUCKETS[i - 1])
                high = float(WAIT_BUCKETS[i]) if i < len(WAIT_BUCKETS) else None
                return low, high
        return float(WAIT_BUCKETS[-1]), None


def evaluate_signals(stats: DailyStats) -> list[str]:
    """合図を超えた項目の文。超えていなければ空。"""
    hits = []
    p95 = stats.wait_p95_bucket()
    if p95 is not None and p95[0] >= SIGNAL_WAIT_P95_SEC and stats.wait_count() >= SIGNAL_WAIT_MIN_COUNT:
        rng = f"{p95[0]:g}秒超" if p95[1] is None else f"{p95[0]:g}〜{p95[1]:g}秒"
        hits.append(f"単品検索の順番待ちの p95 が合図（{SIGNAL_WAIT_P95_SEC:g}秒）を超えました"
                    f"（{rng}・店ごとの取得 {stats.wait_count()}件）→ {ACTIONS['wait_p95']}")
    if stats.memory_peak_mb > SIGNAL_MEMORY_MB:
        hits.append(f"メモリの山が合図（{SIGNAL_MEMORY_MB:g}MB）を超えました"
                    f"（{stats.memory_peak_mb:.0f}MB・{stats.memory_source}）→ {ACTIONS['memory']}")
    if stats.db_failures >= SIGNAL_DB_FAILURES or stats.db_slow >= SIGNAL_DB_SLOW:
        hits.append(f"DB のタイムアウト・応答なしが {stats.db_failures}件（合図 {SIGNAL_DB_FAILURES}件）、"
                    f"遅い応答（{DB_SLOW_SEC:g}秒以上）が {stats.db_slow}件（合図 {SIGNAL_DB_SLOW}件）"
                    f"→ {ACTIONS['db']}")
    return hits


def format_summary(stats: DailyStats, hits: list[str]) -> str:
    p95 = stats.wait_p95_bucket()
    p95_text = "なし" if p95 is None else (f"{p95[0]:g}秒超" if p95[1] is None else f"{p95[0]:g}〜{p95[1]:g}秒")
    lines = [f"💰 **有料化を考える時期です**（{stats.day}）"]
    lines += [f"・{h}" for h in hits]
    lines.append("")
    lines.append(
        f"その日の数字: 単品の順番待ち p95 {p95_text}（{stats.wait_count()}件・待ち切れ "
        f"{stats.pool_single_busy_expired}件）／メモリの山 {stats.memory_peak_mb:.0f}MB／"
        f"DB 応答なし {stats.db_failures}件・遅い {stats.db_slow}件（全{stats.db_requests}件）／"
        f"取得口の受付上限 {stats.pool_capacity_rejected}件・検索の混雑応答 {stats.search_busy}件"
    )
    return "\n".join(lines)


# ── 見張り本体 ──

class SignalMonitor:
    """数える口（record_*）は検索・DB のスレッドから呼ばれる。tick は見張りのスレッドから60秒ごと。"""

    def __init__(self, *, send: Callable[[str], None] | None = None, clock: Callable[[], float] = time.time,
                 instance_id: str | None = None):
        self._send = send or discord_notify.send_discord_message
        self._clock = clock
        self._lock = threading.Lock()
        self.instance_id = instance_id or f"{os.getpid()}-{int(clock())}"
        now = clock()
        self._day = _jst_now(now).strftime("%Y-%m-%d")
        self._stats = DailyStats(self._day)
        self._last_alert: dict[str, float] = {}
        # DB
        self._db_fail_streak = 0
        self._db_fail_streak_alert = False
        self._minute_slow = 0
        # 取得口（累積値の前回分。instance_id が変わったら取り直す）
        self._pool_prev = None
        self._sse_prev = None
        self._recent = deque(maxlen=WINDOW_MINUTES)  # 分ごとの {"slow": n, "pool_busy": n}
        # 書き込み。最初は起動の少しあと（FIRST_FLUSH_SEC）に書く。メモリ不足で30分以内に落ちる繰り返しでも1行は残すため
        self._next_flush_at = now + FIRST_FLUSH_SEC
        self._flush_failures = 0
        self._pending: dict[str, DailyStats] = {}  # 日付が変わったときにまだ書けていない前日以前の分
        self._flush_lock = threading.Lock()  # 見張りのスレッドと終了時（atexit）の書き込みを重ねない
        self._summary_done_for = None
        self._summary_retry_at = 0.0
        self._tick_failures = 0

    # 数える口
    def record_db_response(self, elapsed: float, failed: bool):
        with self._lock:
            self._stats.db_requests += 1
            if failed:
                self._stats.db_failures += 1
                self._db_fail_streak += 1
                if self._db_fail_streak >= DB_FAIL_STREAK_ALERT:
                    self._db_fail_streak_alert = True
            else:
                self._db_fail_streak = 0
            if elapsed >= DB_SLOW_SEC:
                self._stats.db_slow += 1
                self._minute_slow += 1

    def record_wait(self, priority: str, seconds: float):
        """取得口が取得を始めたときの順番待ちの秒数（ShopSearchPool の wait_observer）。単品だけ数える。"""
        if priority != "single":
            return
        with self._lock:
            self._stats.add_wait(seconds)

    def snapshot(self) -> dict:
        with self._lock:
            return {"day": self._day, **self._stats.to_json(), "db_fail_streak": self._db_fail_streak}

    # 60秒ごと
    def tick(self, *, pool_snapshot: dict | None = None, search_busy_total: int | None = None,
             memory: tuple[float | None, str] | None = None, store=None):
        now = self._clock()
        day = _jst_now(now).strftime("%Y-%m-%d")
        if day != self._day:
            self._roll_day(day, now)

        pool_busy = self._pool_delta(pool_snapshot) + self._sse_delta(search_busy_total)
        mem_mb, mem_source = memory if memory is not None else (None, "")
        memory_crossed = False
        with self._lock:
            if mem_mb is not None and mem_mb > self._stats.memory_peak_mb:
                memory_crossed = self._stats.memory_peak_mb <= SIGNAL_MEMORY_MB < mem_mb
                self._stats.memory_peak_mb = mem_mb
                self._stats.memory_source = mem_source
            self._recent.append({"slow": self._minute_slow, "pool_busy": pool_busy})
            self._minute_slow = 0
            db_streak_alert, self._db_fail_streak_alert = self._db_fail_streak_alert, False
            streak = self._db_fail_streak
            slow_minutes = sum(1 for m in self._recent if m["slow"] > 0)
            busy_minutes = sum(1 for m in self._recent if m["pool_busy"] > 0)

        if db_streak_alert:
            self._alert("db_down", now,
                        f"🔴 **DB が応答しません**: タイムアウト・応答なしが {max(streak, DB_FAIL_STREAK_ALERT)}回続きました。"
                        f"Supabase の IO の余力切れ・停止の可能性があります（10-22 朝と同じ形）。"
                        f"Supabase の管理画面で Disk IO と状態を確かめてください。続くなら {ACTIONS['db']}")
        if slow_minutes >= DB_SLOW_MINUTES_ALERT:
            self._alert("db_slow", now,
                        f"🟠 **DB の応答が遅い状態が続いています**: 直近{WINDOW_MINUTES}分のうち{slow_minutes}分で"
                        f"{DB_SLOW_SEC:g}秒以上かかりました。IO の余力が減っている可能性があります。")
        if busy_minutes >= POOL_BUSY_MINUTES_ALERT:
            self._alert("pool_busy", now,
                        f"🟠 **店舗検索が混んでいます**: 直近{WINDOW_MINUTES}分のうち{busy_minutes}分で、"
                        f"取得口の受付上限・順番待ちの上限超え・検索の同時数の混雑応答がありました。"
                        f"毎日続くなら {ACTIONS['wait_p95']}")
        if memory_crossed:
            # Starter（512MB）を超えると強制終了されて山が残らないので、超えたその場で知らせて書く
            self._alert("memory", now,
                        f"🟠 **メモリが合図（{SIGNAL_MEMORY_MB:g}MB）を超えました**: {mem_mb:.0f}MB（{mem_source}）。"
                        f"Starter は 512MB で、超えると強制終了されます。続くなら {ACTIONS['memory']}")
            self._next_flush_at = min(self._next_flush_at, now)

        if store is not None:
            self._maybe_flush(store, now)
            self._maybe_summary(store, now)

    def flush_now(self, store):
        """終了時に最後の分を書く（atexit）。書き込みを避ける時間は書かない。"""
        now = self._clock()
        if store is None or _in_quiet_hours(now):
            return
        self._next_flush_at = now
        self._maybe_flush(store, now)

    def note_tick_failed(self, exc: BaseException):
        self._tick_failures += 1
        if self._tick_failures >= TICK_FAIL_ALERT:
            self._alert("monitor_broken", self._clock(),
                        f"⚠️ 有料化の合図の見張りが {self._tick_failures}回続けて失敗しています: {type(exc).__name__}。"
                        f"合図の通知が届かない状態です（Render のログを確認してください）")

    def note_tick_ok(self):
        self._tick_failures = 0

    # 中身
    def _alert(self, key: str, now: float, message: str):
        last = self._last_alert.get(key)
        if last is not None and now - last < ALERT_COOLDOWN_MINUTES * 60:
            return
        self._last_alert[key] = now
        # 送信の失敗は send_discord_message がログに残す（例外は出さない）
        logger.warning("upgrade_signals: 通知 %s", key)
        self._send(message)

    def _pool_delta(self, snap: dict | None) -> int:
        if not snap:
            return 0
        cur = (snap.get("instance_id"), int(snap.get("capacity_rejected") or 0),
               int(((snap.get("by_priority") or {}).get("single") or {}).get("busy_expired") or 0))
        prev, self._pool_prev = self._pool_prev, cur
        if prev is None or prev[0] != cur[0]:
            return 0
        rejected = max(0, cur[1] - prev[1])
        expired = max(0, cur[2] - prev[2])
        with self._lock:
            self._stats.pool_capacity_rejected += rejected
            self._stats.pool_single_busy_expired += expired
            for _ in range(expired):  # 待ち切れは上限超えの箱へ（p95 の母数に入れる）
                self._stats.add_wait(float("inf"))
        return rejected + expired

    def _sse_delta(self, total: int | None) -> int:
        if total is None:
            return 0
        prev, self._sse_prev = self._sse_prev, int(total)
        if prev is None or total < prev:
            return 0
        delta = int(total) - prev
        with self._lock:
            self._stats.search_busy += delta
        return delta

    def _roll_day(self, day: str, now: float):
        """日付が変わった。前日の分は書き込み待ちに回し、すぐ次の書き込みの機会を作る。"""
        with self._lock:
            old = self._stats
            self._stats = DailyStats(day)
            self._day = day
        self._pending[old.day] = old
        self._next_flush_at = min(self._next_flush_at, now)

    def _write(self, store, stats: DailyStats) -> bool:
        try:
            store.table(DAILY_TABLE).upsert(
                {"day": stats.day, "instance_id": self.instance_id, "stats": stats.to_json(),
                 "updated_at": datetime.now(JST).isoformat()},
                on_conflict="day,instance_id",
            ).execute()
            return True
        except Exception as e:
            logger.warning("upgrade_signals: %s の集計の書き込みに失敗（あとで書き直します）: %s", stats.day, e)
            return False

    def _maybe_flush(self, store, now: float):
        if _in_quiet_hours(now) or now < self._next_flush_at:
            return
        with self._flush_lock:
            self._flush_locked(store, now)

    def _flush_locked(self, store, now: float):
        with self._lock:
            current = DailyStats.merge(self._day, [self._stats.to_json()])
        ok = True
        for day in sorted(self._pending):
            if self._write(store, self._pending[day]):
                self._pending.pop(day, None)
            else:
                ok = False
                break
        if ok:
            ok = self._write(store, current)
        if ok:
            self._flush_failures = 0
            self._next_flush_at = now + FLUSH_INTERVAL_SEC
        else:
            # 止まった DB・テーブル未作成に毎分要求を重ねない。5分→10分→20分→30分と間をあける
            self._flush_failures += 1
            self._next_flush_at = now + min(FLUSH_INTERVAL_SEC, FLUSH_RETRY_SEC * 2 ** (self._flush_failures - 1))

    def _maybe_summary(self, store, now: float):
        jst = _jst_now(now)
        if (jst.hour, jst.minute) < SUMMARY_AFTER or _in_quiet_hours(now):
            return
        yesterday = (jst - timedelta(days=1)).strftime("%Y-%m-%d")
        if self._summary_done_for == yesterday or now < self._summary_retry_at:
            return
        try:
            rows = store.table(DAILY_TABLE).select("instance_id,stats").eq("day", yesterday).execute().data or []
            # 自分の分がまだ書けていなければ手元の値を使う（書けなかった日こそ DB 停止の合図を出す日）
            mine_pending = self._pending.get(yesterday)
            stats_rows = [r.get("stats") or {} for r in rows
                          if not (mine_pending is not None and r.get("instance_id") == self.instance_id)]
            if mine_pending is not None:
                stats_rows.append(mine_pending.to_json())
            stats = DailyStats.merge(yesterday, stats_rows)
            hits = evaluate_signals(stats)
            # 先に「この日は見た」を書いてから送る（複数プロセス・再起動で二重に送らない）。
            # 既にあれば ignore_duplicates で何も返らない＝ほかが済ませた
            claimed = store.table(SUMMARY_TABLE).upsert(
                {"day": yesterday, "signals": len(hits), "instance_id": self.instance_id,
                 "decided_at": datetime.now(JST).isoformat()},
                on_conflict="day", ignore_duplicates=True,
            ).execute().data
            self._summary_done_for = yesterday
            if claimed and hits:
                self._send(format_summary(stats, hits))
            logger.info("upgrade_signals: %s のまとめ（合図 %d件・行 %d・送信 %s）",
                        yesterday, len(hits), len(stats_rows), bool(claimed and hits))
            cutoff = (jst - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            store.table(DAILY_TABLE).delete().lt("day", cutoff).execute()
            store.table(SUMMARY_TABLE).delete().lt("day", cutoff).execute()
        except Exception as e:
            # テーブル未作成・DB 停止のときに毎分やり直してログと DB への要求を重ねない
            self._summary_retry_at = now + FLUSH_INTERVAL_SEC
            logger.warning("upgrade_signals: 前日のまとめに失敗（30分後にやり直します）: %s", e)


monitor = SignalMonitor()


def _resolve(client):
    """クライアントそのもの、または遅れて作られるクライアントを返す関数から、クライアントを取り出す。"""
    try:
        return client() if callable(client) and not hasattr(client, "postgrest") else client
    except Exception:
        logger.exception("upgrade_signals: Supabase クライアントを取り出せませんでした")
        return None


def install_all(db_clients, warned: set) -> None:
    for i, client in enumerate(db_clients):
        resolved = _resolve(client)
        if resolved is None:
            continue
        if not install_db_watch(resolved, monitor.record_db_response) and i not in warned:
            warned.add(i)
            logger.warning("upgrade_signals: DB の見張りを差し込めませんでした（%d番目）。この接続の応答は数えません", i)


def run_forever(*, get_pool_snapshot, get_search_busy_total, db_clients, store, sleep=time.sleep):
    """見張りのスレッドの本体。db_clients は Supabase クライアントか、それを返す関数（毎周確かめ直して差し込む）。"""
    if store is None:
        logger.info("upgrade_signals: Supabase 未設定のため、1日のまとめは出しません（すぐ知らせる通知だけ動かします）")
    warned: set = set()
    while True:
        sleep(TICK_SEC)
        try:
            install_all(db_clients, warned)
            monitor.tick(pool_snapshot=get_pool_snapshot(), search_busy_total=get_search_busy_total(),
                         memory=read_memory_mb(), store=store)
            monitor.note_tick_ok()
        except Exception as e:
            logger.exception("upgrade_signals: 見張りの1周に失敗")
            monitor.note_tick_failed(e)


STORE_TIMEOUT_SEC = 10  # 見張り専用の接続のタイムアウト（DB が止まっても見張りの1周を長く止めない）


def make_store_client(url: str | None, key: str | None):
    """見張りの集計の読み書き専用の Supabase クライアント。サイト本体の接続とタイムアウトを分ける。
    この接続は見張りの対象に入れない（見張り自身の要求を合図の数字に混ぜない）。"""
    if not url or not key:
        return None
    try:
        from supabase import create_client
        from supabase.lib.client_options import SyncClientOptions
        return create_client(url, key, options=SyncClientOptions(postgrest_client_timeout=STORE_TIMEOUT_SEC))
    except Exception:
        logger.exception("upgrade_signals: 集計用の Supabase クライアントを作れませんでした（1日のまとめは出しません）")
        return None


def start(**kwargs) -> bool:
    try:
        # 起動直後の DB 要求（重い集計の statement timeout など）も数えるよう、最初の周を待たずに差し込む
        install_all(kwargs.get("db_clients") or [], set())
        store = kwargs.get("store")
        if store is not None:
            atexit.register(monitor.flush_now, store)
        threading.Thread(target=run_forever, kwargs=kwargs, daemon=True, name="upgrade-signals").start()
        return True
    except Exception:
        logger.exception("upgrade_signals: 見張りのスレッドを起動できませんでした（合図の通知は届きません）")
        return False
