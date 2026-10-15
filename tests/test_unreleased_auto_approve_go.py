"""自動承認の開始（unreleased_approve.py）と、非公開時の X 投稿削除のテスト。

- approve_card（Flask 非依存の承認処理）
- maybe_auto_approve（有効化スイッチ・判定・1日の上限・通知・失敗時に止めない）
- 取込3経路の配線（画像保存の後に呼ぶ・失敗しても取込は止めない）
- x_poster.delete_tweet_ex / unreleased_x_post.delete_posts / 管理画面の4つの非公開操作
"""
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_unreleased as admin_module
import app as app_module
import unreleased_approve as ap
import unreleased_x_post as xp
import watch_x_unreleased as watcher
import x_poster
from constants import JST
from tests.fake_supabase import FakeSB

FUTURE = (datetime.now(JST).date() + timedelta(days=30)).isoformat()


def _card(cid=1, status="pending", eligible=True, **over):
    row = {"id": cid, "name": f"カード{cid}", "source_url": "", "status": status, "release_date": FUTURE,
           "product_name": "商品X", "card_type": "効果モンスター", "attribute": "闇", "race": "ドラゴン族",
           "level": 4, "rank": None, "link_val": None, "atk": 1800, "def": "1000", "effect_text": "効果",
           "pendulum_scale": None, "pendulum_effect": "", "hidden": False,
           "extraction_raw": {"auto_approve_verdict": {"eligible": eligible, "reasons": []}}}
    row.update(over)
    return row


def _sb(cards=None, **tables):
    t = {"unreleased_cards": cards if cards is not None else [], "unreleased_x_posts": [], "app_settings": [],
         "official_card_images": []}
    t.update(tables)
    return FakeSB(t, pks={"unreleased_cards": "id", "unreleased_x_posts": "card_id", "app_settings": "key"})


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(ap, "_cap_notified_dates", set())
    monkeypatch.delenv("AUTO_APPROVE_ENABLED", raising=False)
    monkeypatch.delenv("UNRELEASED_X_POST_ENABLED", raising=False)
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.delenv(k, raising=False)
    notes = []
    monkeypatch.setattr(ap, "_notify", notes.append)
    return notes


@pytest.fixture()
def notes(_isolate):
    return _isolate


# ── approve_card ──

def test_approve_card_ok_records_auto_and_posts_when_image_ok(monkeypatch):
    sb = _sb([_card(1)])
    started, inval = [], []
    monkeypatch.setattr(xp, "start_post_thread", lambda s, ids: started.append(list(ids)))
    res = ap.approve_card(sb, 1, "auto", fetch_image=lambda **kw: (True, "ok"), invalidate_cache=lambda: inval.append(1))
    assert res["status"] == "ok" and res["image_fetched"] is True
    row = sb.tables["unreleased_cards"][0]
    assert row["status"] == "approved" and row["approved_at"]
    assert row["extraction_raw"]["approval_snapshot"]["via"] == "auto"
    assert row["extraction_raw"]["auto_approve_verdict"]["eligible"] is True   # 既存キーを消さない
    assert started == [[1]] and inval


def test_approve_card_no_post_when_image_failed(monkeypatch):
    sb = _sb([_card(1)])
    started = []
    monkeypatch.setattr(xp, "start_post_thread", lambda s, ids: started.append(list(ids)))
    res = ap.approve_card(sb, 1, "auto", fetch_image=lambda **kw: (False, "ng"), invalidate_cache=lambda: None)
    assert res["status"] == "ok" and res["image_fetched"] is False and started == []


def test_approve_card_states():
    assert ap.approve_card(_sb([]), 9, "auto")["status"] == "not_found"
    assert ap.approve_card(_sb([_card(1, status="approved")]), 1, "auto")["status"] == "not_approvable"
    assert ap.approve_card(_sb([_card(1, status="linked")]), 1, "auto")["status"] == "not_approvable"


def test_approve_card_warns_on_past_release_date(monkeypatch):
    sb = _sb([_card(1, release_date="2000-01-01")])
    res = ap.approve_card(sb, 1, "single", fetch_image=lambda **kw: (False, "x"), invalidate_cache=lambda: None)
    assert "過去日" in res["release_date_warning"]


def test_approve_card_backfills_release_date(monkeypatch):
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date",
                        lambda p: {"status": "verified", "release_date": FUTURE})
    sb = _sb([_card(1, release_date=None)])
    res = ap.approve_card(sb, 1, "single", fetch_image=lambda **kw: (False, "x"), invalidate_cache=lambda: None)
    assert FUTURE in res["release_date_note"] and sb.tables["unreleased_cards"][0]["release_date"] == FUTURE


# ── 自動承認 ──

def test_auto_approve_disabled_by_default_touches_nothing():
    class Boom:
        def table(self, name):
            raise AssertionError("無効のときは DB を触らない")
    assert ap.maybe_auto_approve(Boom(), [_card(1)]) == {}


def test_auto_approve_approves_eligible_only_and_notifies(monkeypatch, notes):
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    sb = _sb([_card(1), _card(2, eligible=False)])
    called = []

    def fake_approve(sb_, cid, via):
        called.append((cid, via))
        return {"status": "ok"}

    out = ap.maybe_auto_approve(sb, sb.tables["unreleased_cards"], approve=fake_approve)
    assert called == [(1, "auto")] and out == {"approved": 1, "not_eligible": 1}
    assert len(notes) == 1 and "カード1" in notes[0] and "商品X" in notes[0] and ap.ADMIN_URL in notes[0]


def test_auto_approve_end_to_end_with_real_approve(monkeypatch):
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    sb = _sb([_card(1)])
    out = ap.maybe_auto_approve(sb, sb.tables["unreleased_cards"])
    row = sb.tables["unreleased_cards"][0]
    assert out == {"approved": 1} and row["status"] == "approved"
    assert row["extraction_raw"]["approval_snapshot"]["via"] == "auto"


def test_auto_approve_daily_limit_leaves_pending_and_notifies_once(monkeypatch, notes):
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setattr(ap, "AUTO_APPROVE_DAILY_LIMIT", 2)
    sb = _sb([_card(1), _card(2), _card(3)])
    out = ap.maybe_auto_approve(sb, sb.tables["unreleased_cards"])
    assert out == {"approved": 2, "limit": 1}
    assert [r["status"] for r in sb.tables["unreleased_cards"]] == ["approved", "approved", "pending"]
    cap_notes = [n for n in notes if "上限" in n]
    assert len(cap_notes) == 1
    # 同じ日に別の取込（別 process を想定して process 内の記憶は消す）が来ても、上限通知は繰り返さない
    ap._cap_notified_dates.clear()
    sb.tables["unreleased_cards"].append(_card(4))
    out2 = ap.maybe_auto_approve(sb, [sb.tables["unreleased_cards"][3]])
    assert out2 == {"limit": 1} and len([n for n in notes if "上限" in n]) == 1
    assert sb.tables["unreleased_cards"][3]["status"] == "pending"


def test_count_auto_approved_today_uses_jst_day_and_via():
    now = datetime(2026, 10, 15, 10, 0, tzinfo=JST)

    def r(i, at, via):
        return {"id": i, "approved_at": at, "extraction_raw": {"approval_snapshot": {"via": via}}}

    sb = _sb([
        r(1, "2026-10-14T14:59:59+00:00", "auto"),   # JST 14日 23:59:59 → 前日
        r(2, "2026-10-14T15:00:00+00:00", "auto"),   # JST 15日 00:00:00 → 当日
        r(3, "2026-10-15T01:00:00+00:00", "single"),  # 手動は数えない
        r(4, "2026-10-15T02:00:00+00:00", "auto"),
        {"id": 5, "approved_at": None, "extraction_raw": {}},
    ])
    assert ap.count_auto_approved_today(sb, now) == 2


def test_auto_approve_failure_does_not_raise_and_notifies(monkeypatch, notes):
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    sb = _sb([_card(1), _card(2)])

    def boom(sb_, cid, via):
        if cid == 1:
            raise RuntimeError("DB down")
        return {"status": "not_approvable"}

    out = ap.maybe_auto_approve(sb, sb.tables["unreleased_cards"], approve=boom)
    assert out == {"failed": 2}
    assert any("失敗" in n and "DB down" in n for n in notes) and any("承認できませんでした" in n for n in notes)


def test_auto_approve_unknown_cap_count_fails_closed(monkeypatch, notes):
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")

    class Broken(FakeSB):
        def table(self, name):
            if name == "unreleased_cards":
                raise RuntimeError("approved_at 列なし")
            return super().table(name)

    called = []
    out = ap.maybe_auto_approve(Broken({}), [_card(1)], approve=lambda *a: called.append(a) or {"status": "ok"})
    assert called == [] and out == {"error": 1} and any("数えられなかった" in n for n in notes)


# ── 取込経路の配線 ──

def _x_watcher_setup(monkeypatch, order):
    monkeypatch.setattr(watcher, "_get_known_fuzzy_keys", lambda: set())
    monkeypatch.setattr(watcher, "load_active_names", lambda sb: [])
    monkeypatch.setattr(watcher, "load_text_context", lambda sb: None)
    ext = ModuleType("unreleased_extractor")
    ext.extract_cards_from_tweet = MagicMock(return_value=[{
        "name": "新カード", "product_name": "P", "card_type": "効果モンスター", "effect_text": "効果",
        "atk": 1000, "level": 4, "confidence": "high", "release_date": FUTURE,
        "extraction_raw": {"source": "x_tweet", "card_image_url": "https://example.test/x.jpg"},
    }])
    monkeypatch.setitem(sys.modules, "unreleased_extractor", ext)
    store = ModuleType("unreleased_image_store")
    store.ingest_x_card_image = lambda sb, cid, url, tweet: order.append("image") or (True, "ok")
    monkeypatch.setitem(sys.modules, "unreleased_image_store", store)
    monkeypatch.setattr(watcher, "_upsert_cards", lambda sb, rows: [dict(r, id=7) for r in rows])


TWEET = {"id": "1", "text": "◤新カード◢", "attachments": {"media_keys": ["m"]}}


def test_x_watcher_auto_approves_after_image_saved(monkeypatch):
    order = []
    _x_watcher_setup(monkeypatch, order)
    monkeypatch.setattr(watcher, "maybe_auto_approve", lambda sb, rows: order.append(("approve", [r["id"] for r in rows])))
    n = watcher.process_tweets([TWEET], {"m": "https://example.test/x.jpg"}, MagicMock(), set())
    assert n == 1 and order == ["image", ("approve", [7])]


def test_x_watcher_insert_survives_auto_approve_failure(monkeypatch, notes):
    """自動承認が内部で失敗しても、取込の件数（= since_id を進める根拠）は変わらない。"""
    order = []
    _x_watcher_setup(monkeypatch, order)
    monkeypatch.setenv("AUTO_APPROVE_ENABLED", "1")
    monkeypatch.setattr(ap, "count_auto_approved_today", lambda sb, now=None: (_ for _ in ()).throw(RuntimeError("x")))
    n = watcher.process_tweets([TWEET], {"m": "https://example.test/x.jpg"}, MagicMock(), set())
    assert n == 1 and order == ["image"]


def test_official_watcher_passes_only_newly_inserted_rows(monkeypatch):
    import watch_unreleased
    sb = _sb([{"id": 1, "name": "既存", "product_name": "P"}])
    # upsert(on_conflict=name,product_name) を fake に合わせて再現（既存は無視）
    seen = []
    monkeypatch.setattr(ap, "maybe_auto_approve", lambda sb_, rows: seen.append([r["name"] for r in rows]) or {})
    monkeypatch.setattr("unreleased_auto_approve.load_active_names", lambda s: [])
    monkeypatch.setattr("unreleased_auto_approve.load_text_context", lambda s: None)
    rows = [{"name": "既存", "product_name": "P", "extraction_raw": {}},
            {"name": "新規", "product_name": "P", "extraction_raw": {}}]
    # FakeSB の upsert は id を振らない。判定の付与後の行が渡ることだけを見る
    assert watch_unreleased._upsert_cards(sb, rows) == 2
    assert seen == [["新規"]]
    assert "auto_approve_verdict" in sb.tables["unreleased_cards"][-1]["extraction_raw"]


def test_admin_fetch_x_post_wiring_calls_auto_approve_after_images(monkeypatch):
    """fetch-x-post は画像保存の後に自動承認を呼ぶ（呼び出し位置を静的に確認）。"""
    src = Path(admin_module.__file__).read_text(encoding="utf-8")
    body = src[src.index("def admin_fetch_x_post"):]
    assert body.index("ingest_x_card_image(") < body.index("maybe_auto_approve(_supabase, inserted_cards)")


# ── x_poster.delete_tweet_ex ──

class _Resp:
    def __init__(self, code):
        self.status_code = code


class _HttpErr(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.response = _Resp(code) if code else None


def _creds(monkeypatch):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(k, "x")


def _fake_client(monkeypatch, behavior):
    import tweepy

    class FakeClient:
        def __init__(self, **kw):
            self.session = None

        def delete_tweet(self, tid, user_auth=True):
            assert user_auth is True
            return behavior(tid)

    monkeypatch.setattr(tweepy, "Client", FakeClient)


def test_delete_tweet_ex_success_and_session_has_timeout(monkeypatch):
    _creds(monkeypatch)
    seen = {}

    class R:
        data = {"deleted": True}

    import tweepy

    class FakeClient:
        def __init__(self, **kw):
            pass

        def delete_tweet(self, tid, user_auth=True):
            seen["session"] = self.session
            seen["id"] = tid
            return R()

    monkeypatch.setattr(tweepy, "Client", FakeClient)
    assert x_poster.delete_tweet_ex("123") == (True, None)
    assert seen["id"] == "123" and isinstance(seen["session"], x_poster._TimeoutSession)


@pytest.mark.parametrize("code,expected", [
    (404, (True, None)),                         # 削除済みは目的達成
    (429, (False, x_poster.ERR_RATE_LIMITED)),
    (403, (False, x_poster.ERR_REJECTED)),
    (401, (False, x_poster.ERR_REJECTED)),
    (500, (False, x_poster.ERR_UNKNOWN)),
    (None, (False, x_poster.ERR_UNKNOWN)),       # 応答なし（接続切断・タイムアウト）
])
def test_delete_tweet_ex_errors(monkeypatch, code, expected):
    _creds(monkeypatch)

    def raise_it(tid):
        raise _HttpErr(code)

    _fake_client(monkeypatch, raise_it)
    assert x_poster.delete_tweet_ex("1") == expected


def test_delete_tweet_ex_deleted_false_is_not_success(monkeypatch):
    _creds(monkeypatch)

    class R:
        data = {"deleted": False}

    _fake_client(monkeypatch, lambda tid: R())
    assert x_poster.delete_tweet_ex("1") == (False, x_poster.ERR_UNKNOWN)


def test_delete_tweet_ex_not_configured_and_dry_run(monkeypatch):
    assert x_poster.delete_tweet_ex("1") == (False, x_poster.ERR_NOT_CONFIGURED)
    _creds(monkeypatch)
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    _fake_client(monkeypatch, lambda tid: (_ for _ in ()).throw(AssertionError("DRY_RUN では呼ばない")))
    assert x_poster.delete_tweet_ex("1") == (False, x_poster.ERR_DRY_RUN)


# ── unreleased_x_post の削除 ──

def _posted(sb, cid, tid, status="posted"):
    sb.tables["unreleased_x_posts"].append({"card_id": cid, "status": status, "tweet_id": tid, "attempts": 0})


def test_collect_posted_only_posted_with_tweet_id():
    sb = _sb([_card(1, status="approved"), _card(2, status="approved"), _card(3, status="approved")])
    _posted(sb, 1, "111")
    _posted(sb, 2, None)
    _posted(sb, 3, "333", status="failed")
    rows = xp.collect_posted(sb, [1, 2, 3])
    assert [(r["card_id"], r["tweet_id"], r["name"]) for r in rows] == [(1, "111", "カード1")]
    _posted(sb, 4, "444", status="delete_failed")
    assert [r["card_id"] for r in xp.collect_posted(sb, [4])] == [4]   # 失敗分は再度の非公開で再試行できる


def test_delete_posts_success_removes_row_so_reapproval_can_repost(notes):
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    out = xp.delete_posts(sb, xp.collect_posted(sb, [1]), delete_fn=lambda tid: (True, None))
    assert out == {"deleted": 1} and sb.tables["unreleased_x_posts"] == []


def test_delete_posts_failure_keeps_row_as_delete_failed_and_notifies(monkeypatch):
    sent = []
    monkeypatch.setattr(xp, "_notify", sent.append)
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    out = xp.delete_posts(sb, xp.collect_posted(sb, [1]), delete_fn=lambda tid: (False, "rate_limited"))
    assert out == {"failed": 1}
    row = sb.tables["unreleased_x_posts"][0]
    assert row["status"] == "delete_failed" and "rate_limited" in row["last_error"] and row["tweet_id"] == "111"
    assert len(sent) == 1 and "111" in sent[0] and "カード1" in sent[0]
    # delete_failed の行は同名の重複判定に使われる（X 上に投稿が残っているため）
    assert list(xp._posted_name_keys(sb).values()) == [1]


def test_delete_posts_dry_run_does_not_delete(monkeypatch):
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    called = []
    out = xp.delete_posts(sb, xp.collect_posted(sb, [1]), delete_fn=lambda tid: called.append(tid) or (True, None))
    assert out == {"dry_run": 1} and called == [] and sb.tables["unreleased_x_posts"][0]["status"] == "posted"


def test_delete_posts_without_credentials_keeps_row_and_warns(monkeypatch):
    sent = []
    monkeypatch.setattr(xp, "_notify", sent.append)
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    out = xp.delete_posts(sb, xp.collect_posted(sb, [1]))
    assert out == {"not_configured": 1} and sb.tables["unreleased_x_posts"][0]["status"] == "posted"
    assert sent and "111" in sent[0]


def test_delete_posts_uses_delete_tweet_ex_by_default(monkeypatch):
    _creds(monkeypatch)
    calls = []
    monkeypatch.setattr(x_poster, "delete_tweet_ex", lambda tid: calls.append(tid) or (True, None))
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    assert xp.delete_posts(sb, xp.collect_posted(sb, [1])) == {"deleted": 1} and calls == ["111"]


def test_start_delete_thread_runs_in_background_and_empty_is_noop(monkeypatch):
    assert xp.start_delete_thread(_sb(), []) is None
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    started = []
    monkeypatch.setattr(xp, "delete_posts", lambda sb_, rows, **kw: started.append(threading.current_thread().name) or {})
    th = xp.start_delete_thread(sb, xp.collect_posted(sb, [1]))
    th.join(timeout=5)
    assert started == ["unreleased-x-delete"]


def test_collect_posted_safe_swallows_errors():
    class Boom:
        def table(self, name):
            raise RuntimeError("no table")
    assert xp.collect_posted_safe(Boom(), [1]) == []


def test_start_post_thread_skips_without_credentials_but_keeps_dry_run(monkeypatch):
    monkeypatch.setenv("UNRELEASED_X_POST_ENABLED", "1")
    monkeypatch.setattr(xp, "post_cards_sequential", lambda sb, ids, **kw: {})
    assert xp.start_post_thread(_sb(), [1]) is None                      # 認証なし → 何もしない（回収ジョブが拾う）
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    th = xp.start_post_thread(_sb(), [1])
    assert th is not None
    th.join(timeout=5)
    monkeypatch.delenv("X_POST_DRY_RUN")
    _creds(monkeypatch)
    th = xp.start_post_thread(_sb(), [1])
    assert th is not None
    th.join(timeout=5)


# ── 管理画面の4つの非公開操作 ──

@pytest.fixture()
def admin(monkeypatch):
    sb = _sb([_card(1, status="approved")])
    _posted(sb, 1, "111")
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    admin_module._auth_fail_log.clear()
    admin_module._auth_fail_global_log.clear()
    monkeypatch.setattr(admin_module, "_auth_fail_global_blocked_until", 0.0)
    app_module.app.config.update(TESTING=True)
    captured = []
    monkeypatch.setattr(admin_module._x_post, "start_delete_thread", lambda sb_, rows: captured.append(list(rows)))
    c = app_module.app.test_client()
    c.sb, c.captured = sb, captured
    c.h = {"X-Admin-Key": "test-key"}
    return c


def _tweets(captured):
    return [[(r["card_id"], r["tweet_id"]) for r in rows] for rows in captured]


def test_unpublish_deletes_x_post(admin):
    resp = admin.post("/api/admin/unreleased/1/unpublish", headers=admin.h)
    assert resp.status_code == 200 and admin.sb.tables["unreleased_cards"][0]["status"] == "pending"
    assert _tweets(admin.captured) == [[(1, "111")]]


def test_reject_deletes_x_post(admin):
    assert admin.post("/api/admin/unreleased/1/reject", headers=admin.h).status_code == 200
    assert admin.sb.tables["unreleased_cards"][0]["status"] == "rejected"
    assert _tweets(admin.captured) == [[(1, "111")]]


def test_reject_unknown_card_deletes_nothing(admin):
    assert admin.post("/api/admin/unreleased/99/reject", headers=admin.h).status_code == 404
    assert admin.captured == []


def test_toggle_hidden_deletes_only_when_hiding(admin):
    assert admin.post("/api/admin/unreleased/1/toggle-hidden", headers=admin.h).get_json()["hidden"] is True
    assert _tweets(admin.captured) == [[(1, "111")]]
    admin.captured.clear()
    assert admin.post("/api/admin/unreleased/1/toggle-hidden", headers=admin.h).get_json()["hidden"] is False
    assert _tweets(admin.captured) == [[]]   # 再表示では削除しない


def test_delete_reads_tweet_id_before_row_is_deleted(admin, monkeypatch):
    seen = {}
    orig = admin_module._x_post.collect_posted_safe

    def spy(sb, ids):
        seen["card_existed"] = any(r["id"] == 1 for r in sb.tables["unreleased_cards"])
        return orig(sb, ids)

    monkeypatch.setattr(admin_module._x_post, "collect_posted_safe", spy)
    resp = admin.delete("/api/admin/unreleased/1", headers=admin.h)
    assert resp.status_code == 200 and seen["card_existed"] is True
    assert admin.sb.tables["unreleased_cards"] == [] and _tweets(admin.captured) == [[(1, "111")]]


def test_unpublish_continues_when_x_state_unreadable(admin, monkeypatch):
    def boom(sb, ids):
        raise RuntimeError("unreleased_x_posts なし")

    monkeypatch.setattr(admin_module._x_post, "collect_posted", boom)
    assert admin.post("/api/admin/unreleased/1/unpublish", headers=admin.h).status_code == 200
    assert admin.sb.tables["unreleased_cards"][0]["status"] == "pending" and admin.captured == [[]]


def test_unpublish_without_post_row_deletes_nothing(admin):
    admin.sb.tables["unreleased_x_posts"].clear()
    assert admin.post("/api/admin/unreleased/1/unpublish", headers=admin.h).status_code == 200
    assert admin.captured == [[]]


def test_single_approve_route_still_delegates(admin, monkeypatch):
    """単体承認ルートは unreleased_approve.approve_card を via='single' で呼ぶだけ。"""
    admin.sb.tables["unreleased_cards"][0]["status"] = "pending"
    seen = {}
    real = ap.approve_card

    def spy(sb, cid, via, **kw):
        seen["via"] = via
        return real(sb, cid, via, **kw)

    monkeypatch.setattr(ap, "approve_card", spy)
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (False, "x"))
    resp = admin.post("/api/admin/unreleased/1/approve", headers=admin.h)
    assert resp.status_code == 200 and seen["via"] == "single"
    assert admin.sb.tables["unreleased_cards"][0]["extraction_raw"]["approval_snapshot"]["via"] == "single"


def test_admin_list_exposes_approval_via(admin):
    row = admin.sb.tables["unreleased_cards"][0]
    row["extraction_raw"]["approval_snapshot"] = {"via": "auto"}
    row["extracted_at"] = "2026-10-15T00:00:00+00:00"
    resp = admin.get("/api/admin/unreleased?status=approved", headers=admin.h)
    assert resp.status_code == 200 and resp.get_json()["cards"][0]["approval_via"] == "auto"
