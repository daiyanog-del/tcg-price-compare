"""tests/frozen_clock.py — テストの「今日」と本体の「今日」を同じ時刻にそろえる。

テストモジュールは読み込み時に「今日」(JST) を作って期待値の日付にするが、本体は実行時に
datetime.now(JST) を読む。pytest は全モジュールを先に読み込んでから実行するので、収集と実行の
あいだに JST の0時をまたぐと1日ずれて落ちる（2026-10-29 15:00 UTC の CI: 収集 23:59 JST・実行 0:00 JST で5件）。
freeze_now で本体モジュールの datetime.now をテスト側と同じ時刻に固定して、そろえる。
"""

from datetime import datetime


def freeze_now(monkeypatch, module, now: datetime) -> None:
    """module の中の `datetime.now()` が now（タイムゾーンつき）を返すようにする。
    now(tz) は tz に直した値、now() は手元の時刻（タイムゾーンなし）を返す（本物と同じ形）"""

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.astimezone().replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", _FrozenDatetime)
