"""
tests/test_collect_buyback_featured_db_abort.py — 買取・新弾フィーチャー収集の、DB保存の連続失敗での打ち切り

collect_prices.py と同じ見張り役（db_write_guard.py）を collect_buyback.py・collect_featured.py にも入れた。
- 続けて上限回数失敗したら止まり、残りのカードへ要求を送らない・Discord で1回知らせる・終了コード1
- 一時的な失敗（上限未満）では最後まで走る
- 買取は collection_runs に finished_at=NULL・shop_stats._aborted で残す。新弾は collection_runs に記録しない
- 新弾: DB保存の失敗は店舗の取得失敗ではないので、再試行の対象（失敗ペア）に入れない

ネットワーク・実Supabase・実際の待ち時間は使わない。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import collect_buyback as cb
import collect_featured as cf
import db_write_guard
from price_persist import upsert_buyback_rows

LIMIT = db_write_guard.MAX_CONSECUTIVE_FAILURES


class _Resp:
    def __init__(self, data=None):
        self.data = data or []


class _FakeQuery:
    """table(...) の連鎖。buyback_history の upsert は outcomes で成否を決める"""

    def __init__(self, sb, name):
        self.sb = sb
        self.name = name
        self.op = None

    def __getattr__(self, _):
        return lambda *a, **k: self

    def insert(self, row):
        self.sb.inserted.setdefault(self.name, []).append(row)
        return self

    def upsert(self, rows, **k):
        self.op = ("upsert", rows)
        return self

    def execute(self):
        if self.op and self.op[0] == "upsert":
            return self.sb._next(self.op[1])
        return _Resp()


class _FakeRpc:
    def __init__(self, sb, rows):
        self.sb = sb
        self.rows = rows

    def execute(self):
        resp = self.sb._next(self.rows)
        return _Resp([{"saved_rows": len(self.rows)}]) if resp is not None else None


class _FakeSupabase:
    """書き込み（buyback の upsert・price の RPC）の結果を outcomes（"ok"/"fail"）で順に決める。尽きたら "ok"。"""

    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.write_calls = 0
        self.inserted: dict[str, list] = {}

    def _next(self, rows):
        self.write_calls += 1
        if (self.outcomes.pop(0) if self.outcomes else "ok") == "fail":
            raise TimeoutError("The read operation timed out")
        return _Resp()

    def table(self, name):
        return _FakeQuery(self, name)

    def rpc(self, name, params):
        assert name == "upsert_price_rows"
        return _FakeRpc(self, params["p_rows"])


def _item(card_name, shop):
    return {"shop": shop, "name": card_name, "rarity": "ウルトラ", "code": "X-JP001",
            "condition": "-", "price": 500, "stock": 1, "sold_out": False, "url": "", "image": ""}


def _fake_fetch(shop):
    def _f(card_name, shop_names=None, status_out=None):
        if status_out is not None:
            for s in shop_names or []:
                status_out[s] = {"count": 1}
        return [_item(card_name, shop)]
    return _f


@pytest.fixture
def notices(monkeypatch):
    got = []
    monkeypatch.setattr(db_write_guard.discord_notify, "send_discord_message",
                        lambda msg, **k: got.append(msg))
    return got


# ──────────────────────────────────────────────
# 買取
# ──────────────────────────────────────────────

class TestUpsertBuybackRaiseOnError:
    ROWS = [{"card_name": "a", "shop": "s", "rarity": "r", "max_price": 1, "recorded_at": "2026-10-22"}]

    def test_default_swallows_and_returns_zero(self):
        assert upsert_buyback_rows(_FakeSupabase(["fail"]), self.ROWS) == 0

    def test_raise_on_error_reraises(self):
        with pytest.raises(TimeoutError):
            upsert_buyback_rows(_FakeSupabase(["fail"]), self.ROWS, raise_on_error=True)


@pytest.fixture
def run_buyback(monkeypatch, notices):
    def _run(outcomes, n_cards=10):
        sb = _FakeSupabase(outcomes)
        cleanup_calls = []
        names = [f"c{i}" for i in range(n_cards)]
        monkeypatch.setattr(cb, "get_supabase", lambda: sb)
        monkeypatch.setattr(cb, "sync_latest_packs", lambda sb: (set(names), set()))
        monkeypatch.setattr(cb, "sync_meta_decks", lambda sb: set())
        monkeypatch.setattr(cb, "sync_regulation", lambda sb: set())
        monkeypatch.setattr(cb, "sync_searched_cards", lambda sb, **k: set())
        monkeypatch.setattr(cb, "fetch_tracked_cards_with_meta",
                            lambda sb: [{"card_name": n, "last_collected_at": None} for n in names])
        monkeypatch.setattr(cb, "BUYBACK_SHOPS", [("カードラッシュ", None)])
        monkeypatch.setattr(cb, "SKIP_BUYBACK_SHOPS_IN_CI", set())
        monkeypatch.setattr(cb, "check_shop_availability", lambda shops: list(shops))
        monkeypatch.setattr(cb, "compare_buyback", _fake_fetch("カードラッシュ"))
        monkeypatch.setattr(cb.time, "sleep", lambda s: None)  # 見張り役の待ちもここを通る
        monkeypatch.setattr(cb, "cleanup_old_buyback_data", lambda sb: cleanup_calls.append(1))
        monkeypatch.setattr(cb, "get_host_request_counts", lambda: {})
        code = None
        try:
            cb.main()
        except SystemExit as e:
            code = e.code
        return sb, sb.inserted.get("collection_runs", []), cleanup_calls, code

    return _run


class TestBuybackAbort:
    def test_stops_after_consecutive_failures(self, run_buyback, notices):
        sb, runs, cleanup_calls, code = run_buyback(["ok", "ok"] + ["fail"] * 50)
        assert code == 1
        assert sb.write_calls == 2 + LIMIT  # 残りのカードへは要求を送らない
        assert len(notices) == 1 and "買取価格収集を打ち切りました" in notices[0]
        assert cleanup_calls == []
        assert len(runs) == 1
        run = runs[0]
        assert run["run_type"] == "buyback"
        assert run["finished_at"] is None
        assert run["success_count"] == 2 and run["fail_count"] == LIMIT
        assert run["cutoff_count"] == 0  # collect_prices.py 専用の列
        assert run["shop_stats"]["_aborted"]["processed"] == 2 + LIMIT
        assert run["shop_stats"]["_aborted"]["consecutive_failures"] == LIMIT

    def test_transient_failures_do_not_stop(self, run_buyback, notices):
        sb, runs, cleanup_calls, code = run_buyback(["fail", "ok"] + ["fail"] * (LIMIT - 1) + ["ok"])
        assert code is None
        assert sb.write_calls == 10
        assert notices == []
        assert cleanup_calls == [1]
        assert runs[0]["finished_at"] is not None
        assert "_aborted" not in runs[0]["shop_stats"]


# ──────────────────────────────────────────────
# 新弾フィーチャー
# ──────────────────────────────────────────────

@pytest.fixture
def run_featured(monkeypatch, notices):
    def _run(outcomes, n_cards=10, fetch=None):
        sb = _FakeSupabase(outcomes)
        sleeps = []
        names = [f"c{i}" for i in range(n_cards)]
        monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
        monkeypatch.setattr(cf, "get_supabase", lambda: sb)
        monkeypatch.setattr(cf, "_resolve_pack", lambda sb, a: {"pack_name": "P"})
        monkeypatch.setattr(cf, "get_featured_cards", lambda sb, p: names)
        monkeypatch.setattr(cf, "SHOPS", [("カードラッシュ", None)])
        monkeypatch.setattr(cf, "SKIP_SHOPS_IN_CI", set())
        monkeypatch.setattr(cf, "check_shop_availability", lambda shops: list(shops))
        monkeypatch.setattr(cf, "fetch_cards_collected_today", lambda *a: set())
        monkeypatch.setattr(cf, "compare_prices", fetch or _fake_fetch("カードラッシュ"))
        monkeypatch.setattr(cf.time, "sleep", lambda s: sleeps.append(s))
        monkeypatch.setattr(cf, "get_host_request_counts", lambda: {})
        code = None
        try:
            cf.main()
        except SystemExit as e:
            code = e.code
        return sb, sleeps, code

    return _run


class TestFeaturedAbort:
    def test_stops_after_consecutive_failures(self, run_featured, notices):
        sb, _, code = run_featured(["ok", "ok"] + ["fail"] * 50)
        assert code == 1
        assert sb.write_calls == 2 + LIMIT
        assert len(notices) == 1 and "新弾フィーチャー価格収集を打ち切りました" in notices[0]
        assert f"処理 {2 + LIMIT}/10件・成功 2件" in notices[0]
        assert sb.inserted.get("collection_runs") is None  # 新弾は collection_runs に記録しない

    def test_transient_failures_do_not_stop_and_are_not_retried(self, run_featured, notices):
        """DB保存の失敗は店舗の取得失敗ではないので、再試行ラウンドに回らない"""
        sb, sleeps, code = run_featured(["fail", "ok"] + ["fail"] * (LIMIT - 1) + ["ok"])
        assert code is None
        assert sb.write_calls == 10  # 第1パスだけ（再試行なし）
        assert notices == []

    def test_failures_spanning_retry_round_still_abort(self, run_featured, notices):
        """第1パスの最後と再試行ラウンドにまたがって続けて失敗しても止まる（見張り役は共有）"""
        seen = {"c9": 0}

        def fetch(card_name, shop_names=None, status_out=None):
            # c9 は1回目だけ店舗取得に失敗（書き込みなし）→ 再試行ラウンドで取れて書き込みへ進む
            if card_name == "c9":
                seen["c9"] += 1
                if seen["c9"] == 1:
                    if status_out is not None:
                        status_out["カードラッシュ"] = {"count": 0, "fetch_errors": 1}
                    return []
            return _fake_fetch("カードラッシュ")(card_name, shop_names, status_out)

        # 第1パス: c0〜c6 成功・c7 と c8 で保存失敗（2回連続）・c9 は取得失敗
        # 再試行: c9 の保存も失敗 → 3回連続で打ち切り
        sb, _, code = run_featured(["ok"] * 7 + ["fail"] * 10, fetch=fetch)
        assert seen["c9"] == 2
        assert code == 1
        assert sb.write_calls == 7 + LIMIT
        assert len(notices) == 1
