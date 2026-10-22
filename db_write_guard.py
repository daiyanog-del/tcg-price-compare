"""DB保存の連続失敗で収集を打ち切るための見張り役。

経緯（2026-10-22）: Supabase（無料プラン Nano）が Disk IO budget 枯渇とメモリ不足で固まったあと、
collect_prices.py は保存（RPC upsert_price_rows）が1件ごとに約2分の読み取りタイムアウトで失敗するのに
次のカードへ進み続け、残り約1,200件ぶん DB に要求を送り続けた（23:15〜00:19 UTC に49回失敗。
ユーザーが手で Cancel するまで止まらなかった）。

判定の考え方:
  - 数えるのは「DBへの要求が例外で失敗した」ときだけ。店舗側の取得失敗・有効な価格なし・
    RPC が 0 行と返した（例外ではない）ときは数えない（DB の異常ではないため）
  - 1回の成功で数え直す。続けて MAX_CONSECUTIVE_FAILURES 回失敗したら DbWriteAborted を送出する
  - 失敗のあと次の要求の前に BACKOFF_SEC だけ待つ。一時的な瞬断を待ってやり過ごすのと、
    固まった DB に間を置かず要求を重ねないため

二つの使い方:
  - GuardedSupabase（2026-10-22 追記）: 収集スクリプトの Supabase クライアントを包み、すべての execute() を
    見張る。「DB が応答しない」系の失敗（is_db_unavailable）だけを数え、待って同じ要求を再試行する。
    同期処理（tracked_cards の読み出し・カード追加など）は失敗を握りつぶして次の DB 呼び出しへ進む箇所が多く、
    10-22 09:00 JST の買取は同期処理の最初の読み出しで落ちて通知も記録も残らなかったため
  - collect_*.collect_and_save*: 保存の例外（4xx なども含む）をカード単位で数える（PR #177・#178）
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Callable

import httpx

import collection_run
import discord_notify
from constants import JST

# 実データ（Render のログ・2026-09-23〜10-22）:
#   - 平常時: 停止前の約1か月（毎晩 約3,800件 × 約10回）で upsert 失敗は 0 件。応答は平均 0.2〜0.4 秒
#   - 10-22 の停止時: 失敗が途切れず 49 回連続。1回あたり 80〜120 秒（読み取りタイムアウト）
# 平常時に失敗が1件も無いため「一時的な失敗がどれだけ続くか」の実測が無い。
# 3回連続（待ち 30秒・60秒を挟む＝約1分半の瞬断までは耐える）で打ち切る仮置き。
# 停止時は 打ち切りまで 約3回×2分＋待ち1分半 ≒ 8分（従来は手で止めるまで1時間以上）。
# TODO: calibrate from data
MAX_CONSECUTIVE_FAILURES = 3
# n 回目の失敗のあとに待つ秒数（最後の失敗のあとは待たずに打ち切る）。TODO: calibrate from data
BACKOFF_SEC = (30, 60)


class DbWriteAborted(BaseException):
    """DBへの要求が続けて失敗したので収集を打ち切る、という合図。

    SystemExit と同じく BaseException から派生させる: 同期処理などの `except Exception` に
    握りつぶされて次の DB 呼び出しへ進まないようにするため（打ち切りの合図は main まで届ける）。
    """

    def __init__(self, consecutive: int, last_error: str):
        super().__init__(f"DBへの要求が {consecutive} 回続けて失敗: {last_error}")
        self.consecutive = consecutive
        self.last_error = last_error


class ConsecutiveDbFailureGuard:
    """DB書き込みの連続失敗を数え、上限に達したら DbWriteAborted を送出する。"""

    def __init__(self, max_consecutive: int = MAX_CONSECUTIVE_FAILURES,
                 backoff_sec: tuple[float, ...] = BACKOFF_SEC,
                 sleep: Callable[[float], None] | None = None):
        if max_consecutive < 1:
            raise ValueError("max_consecutive は 1 以上")
        self.max_consecutive = max_consecutive
        self.backoff_sec = tuple(backoff_sec)
        self._sleep = sleep  # None なら呼ぶ時点の time.sleep（テストで差し替えられるように）
        self.consecutive = 0
        self.total_failures = 0
        self.last_error = ""

    def record_success(self) -> None:
        self.consecutive = 0

    def record_failure(self, error: BaseException | str) -> None:
        """失敗を1回数える。上限に達したら DbWriteAborted、そうでなければ待ってから戻る。"""
        self.consecutive += 1
        self.total_failures += 1
        self.last_error = str(error)
        if self.consecutive >= self.max_consecutive:
            raise DbWriteAborted(self.consecutive, self.last_error)
        if self.backoff_sec:
            wait = self.backoff_sec[min(self.consecutive, len(self.backoff_sec)) - 1]
            print(f"  ⚠ DBへの要求が失敗 {self.consecutive}回連続（上限 {self.max_consecutive}回）。"
                  f"{wait:.0f}秒待ちます")
            (self._sleep or time.sleep)(wait)


def is_db_unavailable(exc: BaseException) -> bool:
    """DB（Supabase）が応答しない系の失敗かどうか。待って再試行する対象。

    2026-10-22 の停止時に出たもの: 読み取りタイムアウト（httpx）、504/522（Cloudflare の HTML 応答 →
    postgrest は code=HTTPステータスの APIError にする）、PGRST002（スキーマキャッシュを読めない）、
    57014（statement timeout）。4xx や列・制約のエラーは DB の異常ではないので対象外（呼び元の扱いに任せる）。
    """
    if isinstance(exc, httpx.TransportError):  # タイムアウト・接続失敗・切断
        return True
    code = getattr(exc, "code", None)
    if code is None:
        return False
    code = str(code)
    if code.startswith("PGRST00") or code == "57014":
        return True
    return code.isdigit() and 500 <= int(code) <= 599


class GuardedSupabase:
    """Supabase クライアントを包み、table()/rpc() から作る要求の execute() を見張る。

    DB が応答しない系の失敗（is_db_unavailable）は guard に数え、待ってから同じ要求を再試行する。
    続けて上限回数失敗したら DbWriteAborted。それ以外の例外は数えずにそのまま呼び元へ返す。
    既知の制約: タイムアウトが DB 側の書き込み完了後に起きた insert を再試行すると、行が重なりうる
    （収集で insert を使うのは collection_runs と pack_list のみ。upsert・update・delete は重ねても同じ結果）。
    """

    def __init__(self, client, guard: "ConsecutiveDbFailureGuard"):
        self.raw = client
        self.guard = guard

    def table(self, *a, **k):
        return _GuardedBuilder(self.raw.table(*a, **k), self.guard)

    def rpc(self, *a, **k):
        return _GuardedBuilder(self.raw.rpc(*a, **k), self.guard)

    def __getattr__(self, name):
        return getattr(self.raw, name)


class _GuardedBuilder:
    """postgrest の要求ビルダーの包み。連鎖の途中のビルダーも包み、execute() だけを見張る。"""

    def __init__(self, builder, guard):
        self._b = builder
        self._guard = guard

    def execute(self):
        while True:
            try:
                resp = self._b.execute()  # postgrest の execute() は呼ぶたびに要求を組み立て直す（再試行できる）
            except Exception as e:
                if not is_db_unavailable(e):
                    raise
                print(f"  ⚠ DB 応答なし: {type(e).__name__}: {e}")
                self._guard.record_failure(e)  # 上限で DbWriteAborted。そうでなければ待ってから再試行
                continue
            self._guard.record_success()
            return resp

    def __getattr__(self, name):
        attr = getattr(self._b, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            res = attr(*a, **k)
            if res is self._b:
                return self
            if type(res).__module__.split(".")[0] in ("postgrest", "supabase"):
                return _GuardedBuilder(res, self._guard)
            return res
        return call


def report_db_abort(sb, err: DbWriteAborted, *, label: str, table: str, started_at, today: str,
                    processed: int | None = None, total: int | None = None, success_count: int = 0,
                    run_type: str | None = None, record_kwargs: dict | None = None) -> None:
    """DB保存の連続失敗で収集を打ち切ったことを、Discord と（run_type があれば）collection_runs に残す。

    DB が固まっている前提なので、DB に頼らない Discord を先に送る。collection_runs への記録は
    1回だけ試す（失敗しても record_collection_run が握りつぶす）。finished_at は NULL（異常終了）、
    理由は shop_stats._aborted に入る。終了コードは呼び元が 1 にする（Render の失敗通知にも出る）。
    run_type=None は collection_runs に記録しない収集（collect_featured.py。run_type の CHECK 制約に無い）。
    processed/total が None のときは、カード収集のループの外（前半の同期処理・後処理）で止まったとして知らせる。
    sb が GuardedSupabase なら包む前のクライアントで記録する（記録の失敗で再試行・打ち切りを重ねないため）。
    """
    sb = getattr(sb, "raw", sb)
    where = (f"処理 {processed}/{total}件・成功 {success_count}件" if processed is not None
             else "カード収集のループの外（前半の同期処理か後処理）で止まりました")
    elapsed_min = (datetime.now(JST) - started_at).total_seconds() / 60
    print(f"\n=== 打ち切り（{elapsed_min:.0f}分）: {err} ===")
    print(where)
    discord_notify.send_discord_message(
        f"⚠ **{label}を打ち切りました {today}**\n"
        f"DBへの要求（{table}）が {err.consecutive} 回続けて失敗したため、DB に要求を送り続けないよう止めました。\n"
        f"{where}・所要 {elapsed_min:.0f}分\n"
        f"最後のエラー: {err.last_error[:300]}\n"
        f"Supabase の状態（Disk IO budget・メモリ）を確認してください。残りのカードは今日分なしです。",
        no_mentions=True,
    )
    if run_type is None:
        return
    collection_run.record_collection_run(
        sb, run_type, started_at, None,
        **(record_kwargs or {}),
        aborted={
            "reason": "db_write_failures",
            "consecutive_failures": err.consecutive,
            "last_error": err.last_error[:500],
            "processed": processed,  # None=カード収集のループの外で止まった
            "aborted_at": datetime.now(JST).isoformat(),
        },
    )
