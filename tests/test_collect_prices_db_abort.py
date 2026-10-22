"""
tests/test_collect_prices_db_abort.py — DB保存の連続失敗で夜間収集を打ち切る仕組みの検証

背景: 2026-10-22 朝、Supabase が固まったあと collect_prices.py が保存（RPC upsert_price_rows）の
約2分ごとの失敗を49回繰り返し、DB に要求を送り続けた。db_write_guard.py で
「続けて上限回数失敗したら打ち切る・1回の失敗では止まらない」を入れた。

ネットワーク・実Supabase・実際の待ち時間は使わない（sleep・通知・店舗取得はすべて差し替える）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import collect_prices as cp
import collection_run
import db_write_guard
from db_write_guard import ConsecutiveDbFailureGuard, DbWriteAborted
from price_persist import upsert_price_rows


# ──────────────────────────────────────────────
# フェイク
# ──────────────────────────────────────────────

class _Resp:
    def __init__(self, data=None):
        self.data = data or []


class _FakeQuery:
    def __init__(self, sb, name):
        self.sb = sb
        self.name = name

    def __getattr__(self, _):
        return lambda *a, **k: self

    def insert(self, row):
        self.sb.inserted.setdefault(self.name, []).append(row)
        return self

    def execute(self):
        return _Resp()


class _FakeRpc:
    def __init__(self, sb, rows):
        self.sb = sb
        self.rows = rows

    def execute(self):
        self.sb.rpc_calls += 1
        outcome = self.sb.outcomes.pop(0) if self.sb.outcomes else "ok"
        if outcome == "fail":
            raise TimeoutError("The read operation timed out")
        if outcome == "zero":
            return _Resp([{"saved_rows": 0}])
        return _Resp([{"saved_rows": len(self.rows)}])


class _FakeSupabase:
    """rpc の結果を outcomes（"ok" / "fail" / "zero" の列）で順に決める。尽きたら "ok"。"""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.rpc_calls = 0
        self.inserted: dict[str, list] = {}

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        assert name == "upsert_price_rows"
        return _FakeRpc(self, params["p_rows"])


def _fake_compare_prices(card_name, shop_names=None, status_out=None):
    if status_out is not None:
        for s in shop_names or []:
            status_out[s] = {"count": 1}
    return [{"shop": "カーナベル", "name": card_name, "rarity": "ウルトラ", "code": "X-JP001",
             "condition": "-", "price": 500, "stock": 1, "sold_out": False, "url": "", "image": ""}]


def _no_sleep_guard(**kw):
    waits = []
    g = ConsecutiveDbFailureGuard(sleep=waits.append, **kw)
    return g, waits


# ──────────────────────────────────────────────
# 見張り役そのもの
# ──────────────────────────────────────────────

class TestGuard:
    def test_single_failure_does_not_abort_and_waits(self):
        g, waits = _no_sleep_guard()
        g.record_failure(TimeoutError("x"))
        assert g.consecutive == 1
        assert waits == [db_write_guard.BACKOFF_SEC[0]]

    def test_success_resets_count(self):
        g, _ = _no_sleep_guard()
        for _ in range(db_write_guard.MAX_CONSECUTIVE_FAILURES - 1):
            g.record_failure("x")
        g.record_success()
        # 数え直しなので、また上限-1回までは止まらない
        for _ in range(db_write_guard.MAX_CONSECUTIVE_FAILURES - 1):
            g.record_failure("x")
        assert g.total_failures == 2 * (db_write_guard.MAX_CONSECUTIVE_FAILURES - 1)

    def test_aborts_at_limit_without_waiting_after_last(self):
        g, waits = _no_sleep_guard(max_consecutive=3, backoff_sec=(30, 60))
        g.record_failure("a")
        g.record_failure("b")
        with pytest.raises(DbWriteAborted) as ei:
            g.record_failure("c")
        assert ei.value.consecutive == 3
        assert ei.value.last_error == "c"
        assert waits == [30, 60]  # 最後の失敗のあとは待たない

    def test_backoff_reuses_last_value_when_short(self):
        g, waits = _no_sleep_guard(max_consecutive=4, backoff_sec=(5,))
        g.record_failure("a")
        g.record_failure("b")
        g.record_failure("c")
        assert waits == [5, 5, 5]


# ──────────────────────────────────────────────
# upsert_price_rows の raise_on_error
# ──────────────────────────────────────────────

class TestUpsertRaiseOnError:
    ROWS = [{"card_name": "a", "shop": "s", "rarity": "r", "min_price": 1, "recorded_at": "2026-10-22"}]

    def test_default_swallows_and_returns_zero(self):
        assert upsert_price_rows(_FakeSupabase(["fail"]), self.ROWS) == 0

    def test_raise_on_error_reraises(self):
        with pytest.raises(TimeoutError):
            upsert_price_rows(_FakeSupabase(["fail"]), self.ROWS, raise_on_error=True)


# ──────────────────────────────────────────────
# collect_and_save と見張り役の配線
# ──────────────────────────────────────────────

class TestCollectAndSaveWithGuard:
    def test_db_exception_counts_as_failure(self, monkeypatch):
        monkeypatch.setattr(cp, "compare_prices", _fake_compare_prices)
        g, _ = _no_sleep_guard()
        assert cp.collect_and_save(_FakeSupabase(["fail"]), "a", "2026-10-22", ["カーナベル"], db_guard=g) == 0
        assert g.consecutive == 1

    def test_zero_saved_rows_is_not_a_db_failure(self, monkeypatch):
        """RPC が応答して0行と返したのは DB の異常ではないので数えない（数え直す）"""
        monkeypatch.setattr(cp, "compare_prices", _fake_compare_prices)
        g, _ = _no_sleep_guard()
        g.record_failure("前の失敗")
        assert cp.collect_and_save(_FakeSupabase(["zero"]), "a", "2026-10-22", ["カーナベル"], db_guard=g) == 0
        assert g.consecutive == 0

    def test_scrape_failure_is_not_counted(self, monkeypatch):
        def _raise(*a, **k):
            raise RuntimeError("店舗側")
        monkeypatch.setattr(cp, "compare_prices", _raise)
        g, _ = _no_sleep_guard()
        sb = _FakeSupabase()
        assert cp.collect_and_save(sb, "a", "2026-10-22", ["カーナベル"], db_guard=g) == 0
        assert g.consecutive == 0 and sb.rpc_calls == 0


# ──────────────────────────────────────────────
# main() 全体: 打ち切り・通知・記録
# ──────────────────────────────────────────────

@pytest.fixture
def run_main(monkeypatch):
    """main() を外部に出さずに走らせる。戻り値: (sb, notices, runs, cleanup_calls, exit_code)"""

    def _run(outcomes, n_cards=10):
        sb = _FakeSupabase(outcomes)
        notices, cleanup_calls = [], []
        monkeypatch.setattr(cp, "get_supabase", lambda: sb)
        monkeypatch.setattr(cp, "sync_latest_packs", lambda sb: ({f"c{i}" for i in range(n_cards)}, set()))
        for name in ("sync_meta_decks", "sync_regulation"):
            monkeypatch.setattr(cp, name, lambda sb: set())
        monkeypatch.setattr(cp, "sync_searched_cards", lambda sb, **k: set())
        for name in ("sync_recipe_decks", "sync_theme_wiki_cards", "sync_trending_cards", "save_pack_list"):
            monkeypatch.setattr(cp, name, lambda sb: None)
        monkeypatch.setattr(cp, "sync_remake_themes", lambda sb, new: None)
        monkeypatch.setattr(cp, "fetch_tracked_cards_with_meta",
                            lambda sb: [{"card_name": f"c{i}", "last_collected_at": None} for i in range(n_cards)])
        monkeypatch.setattr(cp, "SHOPS", [("カーナベル", None)])
        monkeypatch.setattr(cp, "SKIP_SHOPS_IN_CI", set())
        monkeypatch.setenv("KANABELL_CLOUD_ID", "x")
        monkeypatch.setenv("KANABELL_API_KEY", "x")
        monkeypatch.setattr(cp, "check_shop_availability", lambda shops: list(shops))
        monkeypatch.setattr(cp, "compare_prices", _fake_compare_prices)
        monkeypatch.setattr(cp.time, "sleep", lambda s: None)  # 見張り役の待ちもここを通る
        monkeypatch.setattr(db_write_guard.discord_notify, "send_discord_message", lambda msg, **k: notices.append(msg))
        monkeypatch.setattr(cp, "send_daily_report", lambda *a, **k: None)
        monkeypatch.setattr(cp, "cleanup_old_data", lambda sb: cleanup_calls.append(1))
        monkeypatch.setattr(cp, "get_host_request_counts", lambda: {})
        exit_code = None
        try:
            cp.main()
        except SystemExit as e:
            exit_code = e.code
        return sb, notices, sb.inserted.get("collection_runs", []), cleanup_calls, exit_code

    return _run


class TestMainAbort:
    def test_stops_after_consecutive_failures(self, run_main):
        sb, notices, runs, cleanup_calls, code = run_main(["ok", "ok"] + ["fail"] * 50, n_cards=10)
        limit = db_write_guard.MAX_CONSECUTIVE_FAILURES
        assert code == 1
        # 2件成功のあと、上限回数だけ試して止まる（残りのカードへは要求を送らない）
        assert sb.rpc_calls == 2 + limit
        assert len(notices) == 1 and "打ち切り" in notices[0]
        assert cleanup_calls == []  # 固まった DB へ掃除の削除を送らない
        assert len(runs) == 1
        run = runs[0]
        assert run["finished_at"] is None  # NULL=異常終了（collection_runs.sql の注記）
        assert run["success_count"] == 2
        assert run["fail_count"] == limit
        assert run["cutoff_count"] == 10 - (2 + limit)
        aborted = run["shop_stats"]["_aborted"]
        assert aborted["reason"] == "db_write_failures"
        assert aborted["consecutive_failures"] == limit
        assert aborted["processed"] == 2 + limit
        assert "timed out" in aborted["last_error"]

    def test_transient_failures_do_not_stop(self, run_main):
        limit = db_write_guard.MAX_CONSECUTIVE_FAILURES
        outcomes = ["ok", "fail", "ok"] + ["fail"] * (limit - 1) + ["ok"]
        sb, notices, runs, cleanup_calls, code = run_main(outcomes, n_cards=10)
        assert code is None
        assert sb.rpc_calls == 10  # 全件処理した
        assert notices == []
        assert cleanup_calls == [1]
        assert runs[0]["finished_at"] is not None
        assert "_aborted" not in runs[0]["shop_stats"]
        assert runs[0]["fail_count"] == limit


class TestRecordCollectionRunAborted:
    def test_finished_at_none_and_aborted_in_shop_stats(self):
        from datetime import datetime
        sb = _FakeSupabase()
        ok = collection_run.record_collection_run(
            sb, "prices", datetime(2026, 10, 22, 5), None, ["a"], ["a"],
            shop_stats={"a": {"ok": 1, "empty": 0, "error": 0}}, aborted={"reason": "x"},
        )
        assert ok
        row = sb.inserted["collection_runs"][0]
        assert row["finished_at"] is None
        assert row["shop_stats"] == {"a": {"ok": 1, "empty": 0, "error": 0}, "_aborted": {"reason": "x"}}

    def test_without_aborted_shop_stats_unchanged(self):
        from datetime import datetime
        sb = _FakeSupabase()
        collection_run.record_collection_run(
            sb, "prices", datetime(2026, 10, 22, 5), datetime(2026, 10, 22, 9), ["a"], ["a"],
            shop_stats={"a": {"ok": 1}},
        )
        row = sb.inserted["collection_runs"][0]
        assert row["shop_stats"] == {"a": {"ok": 1}}
        assert row["finished_at"].startswith("2026-10-22T09")
