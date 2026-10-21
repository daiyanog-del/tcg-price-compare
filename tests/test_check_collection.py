"""価格収集の見張り（check_collection.evaluate）の判定"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_collection as cc

HIST = [5000, 5200, 4800, 0, 5100]  # 0 の日（抜けた日）は中央値に入れない


def test_ok_when_usual():
    assert cc.evaluate("買取", 4900, HIST, [{"finished_at": "x"}], "tcg-collect-buyback") is None


def test_zero_without_run_means_cron_did_not_start():
    msg = cc.evaluate("買取", 0, HIST, [], "tcg-collect-buyback")
    assert "1件もありません" in msg and "起動していない" in msg and "tcg-collect-buyback" in msg


def test_zero_with_finished_run_and_skipped_shops():
    msg = cc.evaluate("販売", 0, HIST, [{"finished_at": "x", "skipped_shops": {"カーナベル": "health_check_failed"}}], "tcg-collect-prices")
    assert "実行は終わったが0件" in msg and "カーナベル（health_check_failed）" in msg


def test_zero_with_unfinished_run():
    assert "実行中のまま" in cc.evaluate("販売", 0, HIST, [{"finished_at": None}], "tcg-collect-prices")


def test_low_compared_to_median():
    # 2026-10-11 の販売（普段の約28%）は知らせる
    msg = cc.evaluate("販売", 11059, [39000, 40000, 41000], [{"finished_at": "x"}], "tcg-collect-prices")
    assert msg and "28%" in msg


def test_usual_low_day_is_not_alerted():
    # 2026-10-11・10-13 の買取（普段の約58%）は知らせない
    assert cc.evaluate("買取", 2924, [5000, 5100, 4900], [{"finished_at": "x"}], "tcg-collect-buyback") is None


def test_no_history_is_not_alerted():
    assert cc.evaluate("買取", 10, [0, 0], [{"finished_at": "x"}], "tcg-collect-buyback") is None
