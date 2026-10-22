"""DB保存の連続失敗で収集を打ち切るための見張り役。

経緯（2026-10-22）: Supabase（無料プラン Nano）が Disk IO budget 枯渇とメモリ不足で固まったあと、
collect_prices.py は保存（RPC upsert_price_rows）が1件ごとに約2分の読み取りタイムアウトで失敗するのに
次のカードへ進み続け、残り約1,200件ぶん DB に要求を送り続けた（23:15〜00:19 UTC に49回失敗。
ユーザーが手で Cancel するまで止まらなかった）。

判定の考え方:
  - 数えるのは「DBへの書き込みが例外で失敗した」ときだけ。店舗側の取得失敗・有効な価格なし・
    RPC が 0 行と返した（例外ではない）ときは数えない（DB の異常ではないため）
  - 1回の書き込み成功で数え直す。続けて MAX_CONSECUTIVE_FAILURES 回失敗したら DbWriteAborted を送出する
  - 失敗のあと次の書き込みの前に BACKOFF_SEC だけ待つ。一時的な瞬断を待ってやり過ごすのと、
    固まった DB に間を置かず要求を重ねないため
"""
from __future__ import annotations

import time
from typing import Callable

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


class DbWriteAborted(RuntimeError):
    """DB保存が続けて失敗したので収集を打ち切る、という合図。"""

    def __init__(self, consecutive: int, last_error: str):
        super().__init__(f"DB保存が {consecutive} 回続けて失敗: {last_error}")
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
            print(f"  ⚠ DB保存失敗 {self.consecutive}回連続（上限 {self.max_consecutive}回）。"
                  f"{wait:.0f}秒待ってから次へ進みます")
            (self._sleep or time.sleep)(wait)
