"""
tests/test_db_guarded_supabase.py — 同期処理を含む全 DB 呼び出しの見張り（db_write_guard.GuardedSupabase）

背景: 2026-10-22 09:00 JST の買取収集は、DB 停止中に同期処理の最初の読み出し（tracked_cards）が
約2分のタイムアウトで失敗し、例外のまま落ちた（Discord 通知も collection_runs の記録も無し）。
同期処理には `except Exception` で失敗を握りつぶして次の DB 呼び出しへ進む箇所も多い。

確かめること:
  - 「DB が応答しない」系（タイムアウト・5xx・PGRST00x・57014）だけを数え、待って同じ要求を再試行する
  - 4xx など通常のエラーは数えず再試行もせず、そのまま呼び元へ返す
  - 本物の postgrest のビルダー連鎖（select().eq().order().range()）でも見張りが効く（httpx の MockTransport）
  - 打ち切りの合図は `except Exception` に握りつぶされない
  - 収集の main: 同期処理で続けて失敗したら止まり、Discord と collection_runs（包む前のクライアント）に残して終了コード1
"""

import sys
from pathlib import Path

import httpx
import pytest
from postgrest import APIError, SyncPostgrestClient

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import collect_buyback as cb
import collect_prices as cp
import db_write_guard
from db_write_guard import ConsecutiveDbFailureGuard, DbWriteAborted, GuardedSupabase, is_db_unavailable

LIMIT = db_write_guard.MAX_CONSECUTIVE_FAILURES


def _req():
    return httpx.Request("GET", "http://x/rest/v1/t")


class TestIsDbUnavailable:
    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout("The read operation timed out", request=_req()),
        httpx.ConnectError("refused", request=_req()),
        APIError({"message": "Could not query the database for the schema cache. Retrying.", "code": "PGRST002"}),
        APIError({"message": "JSON could not be generated", "code": 504}),
        APIError({"message": "JSON could not be generated", "code": 522}),
        APIError({"message": "canceling statement due to statement timeout", "code": "57014"}),
    ])
    def test_unavailable(self, exc):
        assert is_db_unavailable(exc)

    @pytest.mark.parametrize("exc", [
        APIError({"message": "column does not exist", "code": "42703"}),
        APIError({"message": "duplicate key", "code": "23505"}),
        APIError({"message": "JSON could not be generated", "code": 404}),
        ValueError("x"),
        TimeoutError("x"),  # httpx 以外（テストの偽物などが出すもの）は対象外
    ])
    def test_not_unavailable(self, exc):
        assert not is_db_unavailable(exc)


def _real_client(handler):
    """本物の postgrest クライアント。HTTP は handler が返す（ネットワークに出ない）"""
    base = "http://x/rest/v1"
    return SyncPostgrestClient(base, http_client=httpx.Client(base_url=base, transport=httpx.MockTransport(handler)))


class TestGuardedSupabaseWithRealPostgrest:
    def _guarded(self, handler):
        waits = []
        return GuardedSupabase(_real_client(handler), ConsecutiveDbFailureGuard(sleep=waits.append)), waits

    def test_retries_through_builder_chain_then_succeeds(self):
        calls = []

        def handler(req):
            calls.append(str(req.url))
            if len(calls) == 1:
                raise httpx.ReadTimeout("The read operation timed out", request=req)
            return httpx.Response(200, json=[{"card_name": "a"}])

        sb, waits = self._guarded(handler)
        resp = sb.table("tracked_cards").select("card_name").eq("active", True) \
            .order("card_name").range(0, 999).execute()
        assert resp.data == [{"card_name": "a"}]
        assert len(calls) == 2 and calls[0] == calls[1]  # 同じ要求をもう一度送った
        assert "active=eq.True" in calls[0] and "limit=1000" in calls[0]
        assert waits == [db_write_guard.BACKOFF_SEC[0]]
        assert sb.guard.consecutive == 0  # 成功で数え直し

    def test_aborts_after_limit(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(504, text="<html>Gateway Timeout</html>")

        sb, _ = self._guarded(handler)
        with pytest.raises(DbWriteAborted) as ei:
            sb.table("tracked_cards").select("card_name").execute()
        assert len(calls) == LIMIT
        assert ei.value.consecutive == LIMIT

    def test_client_error_is_not_retried_or_counted(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(400, json={"message": "column x does not exist", "code": "42703",
                                             "hint": None, "details": None})

        sb, waits = self._guarded(handler)
        with pytest.raises(APIError):
            sb.table("tracked_cards").select("x").execute()
        assert len(calls) == 1 and waits == [] and sb.guard.consecutive == 0

    def test_rpc_is_guarded(self):
        calls = []

        def handler(req):
            calls.append(req.url.path)
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out", request=req)
            return httpx.Response(200, json=[{"saved_rows": 1}])

        sb, _ = self._guarded(handler)
        assert sb.rpc("upsert_price_rows", {"p_rows": []}).execute().data == [{"saved_rows": 1}]
        assert calls == ["/rest/v1/rpc/upsert_price_rows"] * 2


def test_abort_signal_is_not_swallowed_by_except_exception():
    def swallowing():
        try:
            raise DbWriteAborted(3, "x")
        except Exception:
            return "握りつぶした"

    with pytest.raises(DbWriteAborted):
        swallowing()


# ──────────────────────────────────────────────
# 収集の main: 同期処理で止まる
# ──────────────────────────────────────────────

class _Resp:
    def __init__(self, data=None):
        self.data = data or []


class _Q:
    def __init__(self, sb, name):
        self.sb, self.name = sb, name

    def __getattr__(self, _):
        return lambda *a, **k: self

    def insert(self, row):
        self.sb.inserted.setdefault(self.name, []).append(row)
        return self

    def execute(self):
        self.sb.calls.append(self.name)
        if self.name in self.sb.down_tables:
            raise httpx.ReadTimeout("The read operation timed out", request=_req())
        return _Resp()


class _Sb:
    """down_tables への要求は読み取りタイムアウト。それ以外は空で成功"""

    def __init__(self, down_tables):
        self.down_tables = set(down_tables)
        self.calls: list[str] = []
        self.inserted: dict[str, list] = {}

    def table(self, name):
        return _Q(self, name)


@pytest.fixture
def harness(monkeypatch):
    notices = []
    monkeypatch.setattr(db_write_guard.discord_notify, "send_discord_message",
                        lambda msg, **k: notices.append(msg))
    monkeypatch.setattr(db_write_guard.time, "sleep", lambda s: None)
    return notices


def _run(main):
    try:
        main()
    except SystemExit as e:
        return e.code
    return None


def test_buyback_stops_in_sync_like_2026_10_22(monkeypatch, harness):
    """10-22 09:00 の買取: 最新弾同期の tracked_cards 読み出しでタイムアウト → 以前は例外のまま落ちて通知なし"""
    sb = _Sb(down_tables={"tracked_cards"})
    monkeypatch.setattr(cb, "get_supabase", lambda: sb)
    import collect_prices
    monkeypatch.setattr(collect_prices, "get_pack_list", lambda include_empty=False: [{"name": "P"}])
    monkeypatch.setattr(collect_prices, "_try_wiki_page_variants", lambda name: name)
    monkeypatch.setattr(collect_prices, "fetch_pack_cards", lambda name, wiki_page=None: {"count": 0, "cards": []})

    code = _run(cb.main)

    assert code == 1
    # 上限回数で止まり、次の同期へ進まない。最後は包む前のクライアントで記録を1回だけ
    assert sb.calls == ["tracked_cards"] * LIMIT + ["collection_runs"]
    assert len(harness) == 1 and "ループの外" in harness[0]
    run = sb.inserted["collection_runs"][0]  # 包む前のクライアントで1回だけ記録
    assert run["run_type"] == "buyback" and run["finished_at"] is None
    assert run["shop_stats"]["_aborted"]["processed"] is None


def test_prices_does_not_continue_after_swallowing_site(monkeypatch, harness):
    """検索ログの読み出しは `except Exception` で握りつぶして次へ進む作り。打ち切りの合図はそこを抜けて止まる"""
    sb = _Sb(down_tables={"search_logs"})
    monkeypatch.setattr(cp, "get_supabase", lambda: sb)
    monkeypatch.setattr(cp, "sync_latest_packs", lambda sb: (set(), set()))
    monkeypatch.setattr(cp, "sync_meta_decks", lambda sb: set())
    monkeypatch.setattr(cp, "sync_regulation", lambda sb: set())

    code = _run(cp.main)

    assert code == 1
    assert sb.calls == ["search_logs"] * LIMIT + ["collection_runs"]  # deck_search_logs 以降へ進まない
    assert len(harness) == 1 and "価格収集を打ち切りました" in harness[0]
    assert sb.inserted["collection_runs"][0]["run_type"] == "prices"


def test_transient_sync_failure_recovers(monkeypatch, harness):
    """同期処理での一時的な失敗（上限未満）は待って再試行し、そのまま進む"""
    sb = _Sb(down_tables=set())
    fails = {"n": LIMIT - 1}
    orig = _Q.execute

    def flaky(self):
        if self.name == "search_logs" and fails["n"] > 0:
            fails["n"] -= 1
            self.sb.calls.append(self.name)
            raise httpx.ReadTimeout("timed out", request=_req())
        return orig(self)

    monkeypatch.setattr(_Q, "execute", flaky)
    guarded = GuardedSupabase(sb, ConsecutiveDbFailureGuard())
    assert cp.sync_searched_cards(guarded, recent_days=7, min_count=2) == set()
    assert sb.calls[:LIMIT] == ["search_logs"] * LIMIT  # 失敗2回＋成功1回
    assert "deck_search_logs" in sb.calls  # 次へ進んだ
    assert harness == []
