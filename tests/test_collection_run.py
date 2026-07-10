"""
tests/test_collection_run.py — collection_run.record_collection_run の単体テスト
（フェーズ3 P5、docs/design-phase3-generation-side.md 「P5: 観測店舗集合の非記録」）

検証方針:
  Supabaseクライアントをモックし、
    1. 正常時: collection_runs.insert() が正しい行内容で呼ばれること
    2. 書き込み失敗時（テーブル未作成含む）: 例外を送出せず False を返し、
       呼び出し元（収集本体）を止めないこと
  をテストする。ネットワーク・実DBは使用しない。
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from collection_run import record_collection_run


def _make_sb(execute_side_effect=None):
    """sb.table("collection_runs").insert(row).execute() のチェーンをモックする"""
    sb = MagicMock()
    insert_mock = sb.table.return_value.insert
    if execute_side_effect is not None:
        insert_mock.return_value.execute.side_effect = execute_side_effect
    return sb


class TestRecordSuccess:
    def test_writes_expected_row(self):
        sb = _make_sb()
        started = datetime(2026, 7, 10, 5, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 7, 10, 5, 30, tzinfo=timezone.utc)

        ok = record_collection_run(
            sb, "prices", started, finished,
            attempted_shops=["カードラッシュ", "トレコロCB"],
            available_shops=["カードラッシュ"],
            skipped_shops={"トレコロCB": "health_check_failed"},
            shop_stats={"カードラッシュ": {"ok": 10, "empty": 2, "error": 0}},
            success_count=10, fail_count=2, cutoff_count=0, saved_rows=25,
        )

        assert ok is True
        sb.table.assert_any_call("collection_runs")
        insert_call_args = sb.table.return_value.insert.call_args
        row = insert_call_args[0][0]
        assert row["run_type"] == "prices"
        assert row["started_at"] == started.isoformat()
        assert row["finished_at"] == finished.isoformat()
        assert row["attempted_shops"] == ["カードラッシュ", "トレコロCB"]
        assert row["available_shops"] == ["カードラッシュ"]
        assert row["skipped_shops"] == {"トレコロCB": "health_check_failed"}
        assert row["shop_stats"] == {"カードラッシュ": {"ok": 10, "empty": 2, "error": 0}}
        assert row["success_count"] == 10
        assert row["fail_count"] == 2
        assert row["cutoff_count"] == 0
        assert row["saved_rows"] == 25
        sb.table.return_value.insert.return_value.execute.assert_called_once()

    def test_defaults_for_optional_args(self):
        # skipped_shops / shop_stats を省略しても空dictで埋まり、収集本体側は分岐不要
        sb = _make_sb()
        started = datetime(2026, 7, 10, 5, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 7, 10, 5, 5, tzinfo=timezone.utc)

        ok = record_collection_run(
            sb, "buyback", started, finished,
            attempted_shops=[], available_shops=[],
        )

        assert ok is True
        row = sb.table.return_value.insert.call_args[0][0]
        assert row["skipped_shops"] == {}
        assert row["shop_stats"] == {}
        assert row["success_count"] == 0


class TestRecordFailureDoesNotRaise:
    """テーブル未作成・書き込みエラー時も例外を送出せず、収集本体を止めないこと"""

    def test_execute_raises_returns_false_without_exception(self):
        sb = _make_sb(execute_side_effect=Exception("relation \"collection_runs\" does not exist"))
        started = datetime(2026, 7, 10, 5, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 7, 10, 5, 5, tzinfo=timezone.utc)

        # 例外が外に漏れないことそのものが担保内容（raisesされたらテスト失敗）
        ok = record_collection_run(
            sb, "prices", started, finished,
            attempted_shops=["カードラッシュ"], available_shops=["カードラッシュ"],
        )
        assert ok is False

    def test_logs_warning_on_failure(self, caplog):
        sb = _make_sb(execute_side_effect=Exception("boom"))
        started = datetime(2026, 7, 10, 5, 0, tzinfo=timezone.utc)
        finished = datetime(2026, 7, 10, 5, 5, tzinfo=timezone.utc)

        with caplog.at_level(logging.WARNING, logger="collection_run"):
            record_collection_run(
                sb, "prices", started, finished,
                attempted_shops=[], available_shops=[],
            )
        assert any("記録失敗" in rec.message for rec in caplog.records)
