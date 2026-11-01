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
        assert "active=eq.true" in calls[0].lower() and "limit=1000" in calls[0]  # 版によって True/true
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
    assert len(harness) == 1 and "カード収集の前（同期処理）で止まりました" in harness[0]
    run = sb.inserted["collection_runs"][0]  # 包む前のクライアントで1回だけ記録
    assert run["run_type"] == "buyback" and run["finished_at"] is None
    assert run["shop_stats"]["_aborted"]["processed"] is None
    assert run["shop_stats"]["_aborted"]["phase"] == "sync"


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


# ──────────────────────────────────────────────
# 判定の追加分・送り直さないもの（本物の postgrest）
# ──────────────────────────────────────────────

@pytest.mark.parametrize("code", ["53200", "53300", "08006", "57P01", "58030"])
def test_postgres_resource_errors_are_unavailable(code):
    """10-22 の原因はメモリ不足。PostgREST は 503＋JSON で返し APIError.code に SQLSTATE が入る"""
    assert is_db_unavailable(APIError({"message": "x", "code": code}))


def test_config_errors_are_not_unavailable():
    """URL の書き間違いなど設定ミスの類は「DB 停止」と数えない"""
    assert not is_db_unavailable(httpx.UnsupportedProtocol("bad url", request=_req()))


class TestNoResend:
    def _guarded(self, handler):
        waits = []
        return GuardedSupabase(_real_client(handler), ConsecutiveDbFailureGuard(sleep=waits.append)), waits

    def test_statement_timeout_is_counted_but_not_resent(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(500, json={"message": "canceling statement due to statement timeout",
                                             "code": "57014", "hint": None, "details": None})

        sb, waits = self._guarded(handler)
        with pytest.raises(APIError):
            sb.table("price_history_v2").delete().lt("recorded_at", "2026-07-01").execute()
        assert len(calls) == 1  # 同じ重い要求を重ねない
        assert sb.guard.consecutive == 1 and waits == [db_write_guard.BACKOFF_SEC[0]]

    def test_insert_is_not_resent(self):
        calls = []

        def handler(req):
            calls.append(req.method)
            raise httpx.ReadTimeout("timed out", request=req)

        sb, _ = self._guarded(handler)
        with pytest.raises(httpx.ReadTimeout):
            sb.table("collection_runs").insert({"run_type": "prices"}).execute()
        assert calls == ["POST"]  # 書き込み済みかもしれない insert は送り直さない（行が重なるため）
        assert sb.guard.consecutive == 1

    def test_upsert_is_resent(self):
        calls = []

        def handler(req):
            calls.append(req.method)
            if len(calls) == 1:
                raise httpx.ReadTimeout("timed out", request=req)
            return httpx.Response(201, json=[])

        sb, _ = self._guarded(handler)
        sb.table("tracked_cards").upsert([{"card_name": "a"}], on_conflict="card_name",
                                         ignore_duplicates=True).execute()
        assert calls == ["POST", "POST"]

    def test_not_property_and_from_are_guarded(self):
        calls = []

        def handler(req):
            calls.append(str(req.url))
            if len(calls) in (1, 3):
                raise httpx.ReadTimeout("timed out", request=req)
            return httpx.Response(200, json=[])

        sb, _ = self._guarded(handler)
        sb.table("t").select("a").not_.is_("b", "null").execute()
        sb.from_("t").select("a").execute()
        assert len(calls) == 4  # どちらも1回送り直した


# ──────────────────────────────────────────────
# 販売の main: ループ・後処理で止まる／一時的なら最後まで走る
# ──────────────────────────────────────────────

class _Q2:
    def __init__(self, sb, name):
        self.sb, self.name = sb, name
        self.pending = None

    def __getattr__(self, _):
        return lambda *a, **k: self

    def insert(self, row):
        self.pending = row  # 成功したときだけ行を残す
        return self

    def execute(self):
        self.sb.calls.append(self.name)
        left = self.sb.down.get(self.name, 0)
        if left:
            self.sb.down[self.name] = left - 1
            raise httpx.ReadTimeout("The read operation timed out", request=_req())
        if self.pending is not None:
            self.sb.inserted.setdefault(self.name, []).append(self.pending)
        if self.name == "rpc:upsert_price_rows":
            return _Resp([{"saved_rows": 1}])
        return _Resp()


class _Sb2:
    """down: {要求の名前: 失敗させる回数}（rpc は "rpc:名前"）。回数が尽きたら成功"""

    def __init__(self, down):
        self.down = dict(down)
        self.calls: list[str] = []
        self.inserted: dict[str, list] = {}

    def table(self, name):
        return _Q2(self, name)

    def rpc(self, name, params):
        return _Q2(self, f"rpc:{name}")


FOREVER = 10 ** 6


@pytest.fixture
def run_prices(monkeypatch, harness):
    def _go(down, n_cards=5, daily_report=False):
        sb = _Sb2(down)
        monkeypatch.setattr(cp, "get_supabase", lambda: sb)
        monkeypatch.setattr(cp, "sync_latest_packs", lambda sb: ({f"c{i}" for i in range(n_cards)}, set()))
        monkeypatch.setattr(cp, "sync_meta_decks", lambda sb: set())
        monkeypatch.setattr(cp, "sync_regulation", lambda sb: set())
        monkeypatch.setattr(cp, "sync_searched_cards", lambda sb, **k: set())
        for name in ("sync_recipe_decks", "sync_theme_wiki_cards", "sync_trending_cards", "save_pack_list"):
            monkeypatch.setattr(cp, name, lambda sb: None)
        monkeypatch.setattr(cp, "sync_remake_themes", lambda sb, new: None)
        monkeypatch.setattr(cp, "fetch_tracked_cards_with_meta",
                            lambda sb: [{"card_name": f"c{i}", "last_collected_at": None} for i in range(n_cards)])
        monkeypatch.setattr(cp, "SHOPS", [("カーナベル", None)])
        # カーナベルの全件巡回（既定 shadow）は回さない。本物の鍵がある環境で ES へ出ないように
        monkeypatch.setattr(cp, "KANABELL_CATALOG_MODE", "off")
        monkeypatch.setattr(cp, "SKIP_SHOPS_IN_CI", set())
        monkeypatch.setenv("KANABELL_ES_ENDPOINT", "x")
        monkeypatch.setenv("KANABELL_ES_API_KEY", "x")
        monkeypatch.setattr(cp, "check_shop_availability", lambda shops: list(shops))

        def fake_compare(card_name, shop_names=None, status_out=None):
            if status_out is not None:
                for s in shop_names or []:
                    status_out[s] = {"count": 1}
            return [{"shop": "カーナベル", "name": card_name, "rarity": "ウルトラ", "code": "X", "condition": "-",
                     "price": 500, "stock": 1, "sold_out": False, "url": "", "image": ""}]

        monkeypatch.setattr(cp, "compare_prices", fake_compare)
        monkeypatch.setattr(cp.time, "sleep", lambda s: None)
        monkeypatch.setattr(cp, "get_host_request_counts", lambda: {})
        if daily_report:
            import requests
            monkeypatch.setenv("DISCORD_WEBHOOK_URL", "http://example.invalid/hook")
            monkeypatch.setattr(requests, "post", lambda *a, **k: None)
        else:
            monkeypatch.setattr(cp, "send_daily_report", lambda *a, **k: None)
        code = _run(cp.main)
        return sb, code

    return _go


def test_prices_transient_upsert_timeout_resends_same_card(run_prices, harness):
    sb, code = run_prices({"rpc:upsert_price_rows": LIMIT - 1})
    assert code is None and harness == []
    # 失敗した保存は同じカードで送り直す（カードを飛ばさない）→ 5枚とも保存
    assert sb.calls.count("rpc:upsert_price_rows") == 5 + (LIMIT - 1)
    run = sb.inserted["collection_runs"][0]
    assert run["success_count"] == 5 and run["fail_count"] == 0 and run["finished_at"] is not None


def test_prices_persistent_upsert_timeout_stops_in_loop(run_prices, harness):
    sb, code = run_prices({"rpc:upsert_price_rows": FOREVER})
    assert code == 1
    assert sb.calls.count("rpc:upsert_price_rows") == LIMIT  # 1枚目を上限回数送って止まる
    assert "price_history_v2" not in sb.calls  # 掃除へ進まない
    run = sb.inserted["collection_runs"][0]
    assert run["shop_stats"]["_aborted"]["phase"] == "loop"
    assert run["success_count"] == 0 and run["fail_count"] == 1
    assert "処理 1/5件" in harness[0]


def test_prices_abort_on_tracked_cards_update_keeps_success(run_prices, harness):
    sb, code = run_prices({"tracked_cards": FOREVER})
    assert code == 1
    assert sb.calls.count("rpc:upsert_price_rows") == 1
    run = sb.inserted["collection_runs"][0]
    assert run["success_count"] == 1 and run["fail_count"] == 0  # 保存は済んでいる
    assert run["shop_stats"]["_aborted"]["phase"] == "loop"


def test_prices_abort_in_cleanup_reports_post_phase_with_counts(run_prices, harness):
    sb, code = run_prices({"price_history_v2": FOREVER})
    assert code == 1
    assert sb.calls.count("price_history_v2") == LIMIT
    assert len(harness) == 1 and "カード収集は終わったあと" in harness[0] and "処理 5/5件・成功 5件" in harness[0]
    runs = sb.inserted["collection_runs"]
    assert len(runs) == 1  # 正常終了の行は書く前に止まった → 打ち切りの行だけ
    assert runs[0]["finished_at"] is None
    assert runs[0]["success_count"] == 5 and runs[0]["shop_stats"]["_aborted"]["phase"] == "post"


def test_prices_abort_in_daily_report_does_not_add_second_row(run_prices, harness):
    sb, code = run_prices({"search_logs": FOREVER}, daily_report=True)
    assert code == 1
    runs = sb.inserted["collection_runs"]
    assert len(runs) == 1 and runs[0]["finished_at"] is not None  # 正常終了の行だけ（打ち切りの行を重ねない）
    assert len(harness) == 1 and "カード収集は終わったあと" in harness[0]


# ──────────────────────────────────────────────
# 新弾の main: 同期処理（featured_pack の読み出し）で止まる
# ──────────────────────────────────────────────

def test_featured_stops_in_sync(monkeypatch, harness):
    import collect_featured as cf
    sb = _Sb2({"featured_pack": FOREVER})
    monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
    monkeypatch.setattr(cf, "get_supabase", lambda: sb)
    code = _run(cf.main)
    assert code == 1
    assert sb.calls == ["featured_pack"] * LIMIT
    assert len(harness) == 1 and "新弾フィーチャー価格収集を打ち切りました" in harness[0]
    assert "collection_runs" not in sb.inserted  # 新弾は collection_runs に記録しない


def test_prices_record_failure_then_report_abort_still_records(run_prices, harness):
    """正常終了の記録に失敗したあと日次レポートで止まったら、打ち切りの行を残す（記録済みと誤認しない）"""
    sb, code = run_prices({"collection_runs": 1, "search_logs": FOREVER}, daily_report=True)
    assert code == 1
    runs = sb.inserted["collection_runs"]
    assert len(runs) == 1 and runs[0]["finished_at"] is None
    assert runs[0]["shop_stats"]["_aborted"]["phase"] == "post" and runs[0]["success_count"] == 5


def test_prices_abort_in_catalog_save_joins_counted_report(monkeypatch, run_prices, harness):
    """finally のカタログ保存で止まったら、件数つき（phase=post）の報告に合流し、記録は1回だけ"""
    def fake_prepare(selected, available_shops, shop_stats):
        return list(available_shops), {}, {"products": 0, "categories_failed": []}

    def fake_save(sb, processed, matched, today, shop_stats, stats, art_contexts=None):
        sb.rpc("upsert_price_rows_catalog", {}).execute()
        return 0

    monkeypatch.setattr(cp, "_prepare_clabo_catalog", fake_prepare)
    monkeypatch.setattr(cp, "_save_clabo_catalog_rows", fake_save)
    sb, code = run_prices({"rpc:upsert_price_rows_catalog": FOREVER})
    assert code == 1
    assert sb.calls.count("rpc:upsert_price_rows_catalog") == LIMIT
    assert "price_history_v2" not in sb.calls  # 掃除へ進まない
    runs = sb.inserted["collection_runs"]
    assert len(runs) == 1 and runs[0]["finished_at"] is None
    assert runs[0]["shop_stats"]["_aborted"]["phase"] == "post"
    assert runs[0]["success_count"] == 5
    assert len(harness) == 1 and "処理 5/5件・成功 5件" in harness[0] and "カード収集は終わったあと" in harness[0]


def test_collect_and_save_statement_timeout_counted_once(monkeypatch):
    """保存が 57014 → 見張り付きクライアントが1回だけ数えて返す（送り直さない・collect_and_save で重ねない）"""
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(500, json={"message": "canceling statement due to statement timeout",
                                         "code": "57014", "hint": None, "details": None})

    waits = []
    sb = GuardedSupabase(_real_client(handler), ConsecutiveDbFailureGuard(sleep=waits.append))
    monkeypatch.setattr(cp, "compare_prices", lambda card_name, shop_names=None, status_out=None: [
        {"shop": "カーナベル", "name": card_name, "rarity": "ウルトラ", "code": "X", "condition": "-",
         "price": 500, "stock": 1, "sold_out": False, "url": "", "image": ""}])
    assert cp.collect_and_save(sb, "a", "2026-10-22", ["カーナベル"], db_guard=sb.guard) == 0
    assert calls == ["/rest/v1/rpc/upsert_price_rows"]
    assert sb.guard.consecutive == 1


def test_unguarded_client_with_guard_still_counts(monkeypatch):
    """包んでいないクライアントと見張り役を渡したときは、DB が応答しない系も collect_and_save が数える"""
    class Raw:
        def rpc(self, name, params):
            class B:
                def execute(self_inner):
                    raise httpx.ReadTimeout("timed out", request=_req())
            return B()

    monkeypatch.setattr(cp, "compare_prices", lambda card_name, shop_names=None, status_out=None: [
        {"shop": "カーナベル", "name": card_name, "rarity": "ウルトラ", "code": "X", "condition": "-",
         "price": 500, "stock": 1, "sold_out": False, "url": "", "image": ""}])
    guard = ConsecutiveDbFailureGuard(sleep=lambda s: None)
    cp.collect_and_save(Raw(), "a", "2026-10-22", ["カーナベル"], db_guard=guard)
    assert guard.consecutive == 1


def test_buyback_abort_in_cleanup_reports_post(monkeypatch, harness):
    sb = _Sb2({"cleanup_target": FOREVER})
    names = [f"c{i}" for i in range(3)]
    monkeypatch.setattr(cb, "get_supabase", lambda: sb)
    monkeypatch.setattr(cb, "sync_latest_packs", lambda sb: (set(names), set()))
    monkeypatch.setattr(cb, "sync_meta_decks", lambda sb: set())
    monkeypatch.setattr(cb, "sync_regulation", lambda sb: set())
    monkeypatch.setattr(cb, "sync_searched_cards", lambda sb, **k: set())
    monkeypatch.setattr(cb, "fetch_tracked_cards_with_meta",
                        lambda sb: [{"card_name": n, "last_collected_at": None} for n in names])
    monkeypatch.setattr(cb, "BUYBACK_SHOPS", [("カードラッシュ", None)])
    monkeypatch.setattr(cb, "SKIP_BUYBACK_SHOPS_IN_CI", set())
    monkeypatch.setattr(cb, "check_shop_availability", lambda shops, urls=None: list(shops))

    def fake_buyback(card_name, shop_names=None, status_out=None):
        if status_out is not None:
            status_out["カードラッシュ"] = {"count": 1}
        return [{"shop": "カードラッシュ", "name": card_name, "rarity": "ウルトラ", "price": 500, "sold_out": False}]

    monkeypatch.setattr(cb, "compare_buyback", fake_buyback)
    monkeypatch.setattr(cb.time, "sleep", lambda s: None)
    monkeypatch.setattr(cb, "cleanup_old_buyback_data", lambda sb: sb.table("cleanup_target").delete().execute())
    monkeypatch.setattr(cb, "get_host_request_counts", lambda: {})

    code = _run(cb.main)
    assert code == 1
    assert sb.calls.count("cleanup_target") == LIMIT
    runs = sb.inserted["collection_runs"]
    assert len(runs) == 1 and runs[0]["finished_at"] is None and runs[0]["run_type"] == "buyback"
    assert runs[0]["shop_stats"]["_aborted"]["phase"] == "post" and runs[0]["success_count"] == 3
    assert len(harness) == 1 and "処理 3/3件・成功 3件" in harness[0]


def test_non_builder_postgrest_values_are_not_wrapped():
    sb = GuardedSupabase(_real_client(lambda req: httpx.Response(200, json=[])), ConsecutiveDbFailureGuard())
    q = sb.table("t").select("a")
    assert not isinstance(q.request, db_write_guard._GuardedBuilder)
