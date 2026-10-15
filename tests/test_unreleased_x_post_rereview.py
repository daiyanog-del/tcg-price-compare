"""再レビュー指摘（2026-10-15）の修正テスト: approved_at による投稿対象判定・回収枠・同名競合ほか。"""
import threading
import time
from datetime import datetime, timedelta, timezone

import admin_unreleased as admin_module
import unreleased_auto_approve as aa
import unreleased_x_post as xp
from tests.fake_supabase import FakeSB, Query
from tests.test_admin_unreleased_release_date_guard import HEADERS, _client
from tests.test_unreleased_x_post import (  # noqa: F401
    APPROVED_AT, SNAP, TODAY, Poster, _add_card, _env, _sb,
)
from tests.test_unreleased_x_post_review_fixes import (  # noqa: F401
    _SyncThread, _admin_with_sb, _approval_row, _ann_row, _rows, fresh_context_cache, notices,
)


# ══════════════════════════════════════════════
# 中3・中2: approved_at を承認 UPDATE と同時に入れる
# ══════════════════════════════════════════════

def test_single_approve_sets_approved_at_in_same_update(monkeypatch):
    sb = _admin_with_sb(monkeypatch, [_approval_row()])
    rows = sb.tables["unreleased_cards"]
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (False, "ng"))
    resp = _client().post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200 and rows[0]["status"] == "approved"
    assert xp._parse_ts(rows[0]["approved_at"]) is not None  # UTC ISO で入る
    assert resp.get_json()["card"]["approved_at"] == rows[0]["approved_at"]


def test_bulk_approve_sets_approved_at(monkeypatch):
    rows = _admin_with_sb(monkeypatch, [_approval_row(1), _approval_row(2)]).tables["unreleased_cards"]
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda card_id, **kw: (False, "x"))
    monkeypatch.setattr(admin_module.threading, "Thread", _SyncThread)
    resp = _client().post("/api/admin/unreleased/bulk-approve", headers=HEADERS, json={"ids": [1, 2]})
    assert resp.status_code == 200
    assert all(xp._parse_ts(r["approved_at"]) is not None for r in rows)


class _NoApprovedAtColumnQuery(Query):
    """approved_at 列が無い DB（SQL 適用前）の再現: approved_at を含む UPDATE は失敗する"""

    def execute(self):
        if self.op == "update" and "approved_at" in (self.payload or {}):
            err = RuntimeError("column unreleased_cards.approved_at does not exist")
            err.code = "42703"   # PostgreSQL の undefined_column（本物のエラーに付くコード）
            raise err
        return super().execute()


class _NoApprovedAtColumnSB(FakeSB):
    def table(self, name):
        return _NoApprovedAtColumnQuery(self, name)


def _install_no_column_sb(monkeypatch, rows):
    sb = _NoApprovedAtColumnSB({"unreleased_cards": rows})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date",
                        lambda product: {"status": "unverified", "release_date": None})
    return sb


def test_single_approve_survives_missing_approved_at_column(monkeypatch, caplog):
    """SQL より先にコードがデプロイされても承認は成立する（approved_at 抜きで再実行・警告ログ）"""
    rows = _install_no_column_sb(monkeypatch, [_approval_row()]).tables["unreleased_cards"]
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (False, "ng"))
    with caplog.at_level("WARNING"):
        resp = _client().post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200 and rows[0]["status"] == "approved" and "approved_at" not in rows[0]
    assert "approved_at 列が無い" in caplog.text


def test_bulk_approve_survives_missing_approved_at_column(monkeypatch):
    rows = _install_no_column_sb(monkeypatch, [_approval_row(1), _approval_row(2)]).tables["unreleased_cards"]
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda card_id, **kw: (False, "x"))
    monkeypatch.setattr(admin_module.threading, "Thread", _SyncThread)
    resp = _client().post("/api/admin/unreleased/bulk-approve", headers=HEADERS, json={"ids": [1, 2]})
    assert resp.status_code == 200 and resp.get_json()["approved"] == 2
    assert [r["status"] for r in rows] == ["approved", "approved"]


def test_approve_update_other_errors_still_propagate(monkeypatch):
    """approved_at と無関係のエラーは握りつぶさない（承認失敗として 500）"""
    sb = _admin_with_sb(monkeypatch, [_approval_row()])

    class Boom(Query):
        def execute(self):
            if self.op == "update":
                raise RuntimeError("connection reset")
            return super().execute()

    monkeypatch.setattr(sb, "table", lambda name: Boom(sb, name))
    resp = _client().post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 500


def test_backfill_candidates_require_approved_at():
    sb = _sb()
    _add_card(sb, 2, approved=False)   # 導入前の承認（approved_at なし）
    assert xp.find_backfill_candidates(sb, TODAY) == [1]


def test_backfill_not_picked_up_without_approved_at_even_with_snapshot():
    sb = _sb(card={"approved_at": None})  # approval_snapshot はあるが approved_at がない
    assert xp.find_backfill_candidates(sb, TODAY) == []


# ══════════════════════════════════════════════
# 中1: 回収枠の食い潰し
# ══════════════════════════════════════════════

def test_backfill_not_starved_by_released_name_cards(monkeypatch):
    """発売済み名のカード10件が先頭にあっても、通常カードが回収で投稿される"""
    monkeypatch.setattr(xp, "_is_released_name", lambda name: name.startswith("R"))
    sb = _sb(card={"id": 99, "name": "通常カード"})
    for cid in range(2, 12):
        _add_card(sb, cid)
        sb.tables["unreleased_cards"][-1]["name"] = f"R{cid}"
    assert xp.find_backfill_candidates(sb, TODAY) == [99]
    poster = Poster()
    assert xp.run_backfill(sb, post_fn=poster, sleep=lambda s: None) == {"posted": 1}
    assert len(poster.calls) == 1


def test_sequential_not_starved_even_if_candidates_are_released_names(monkeypatch):
    """候補選定をすり抜けた発売済み名（not_postable）が10件並んでも、max_cards の枠は消費されない"""
    monkeypatch.setattr(xp, "_is_released_name", lambda name: name.startswith("R"))
    sb = _sb(card={"id": 99, "name": "通常カード"})
    ids = []
    for cid in range(2, 12):
        _add_card(sb, cid)
        sb.tables["unreleased_cards"][-1]["name"] = f"R{cid}"
        ids.append(cid)
    poster = Poster()
    counts = xp.post_cards_sequential(sb, ids + [99], post_fn=poster, max_cards=xp.MAX_BACKFILL_PER_RUN)
    assert counts == {"not_postable": 10, "posted": 1}


def test_backfill_excludes_same_name_already_posted():
    sb = _sb(card={"name": "同名"}, posts=[{"card_id": 50, "status": "posted", "attempts": 0}])
    sb.tables["unreleased_cards"].append({"id": 50, "name": "同名", "status": "approved", "hidden": False,
                                          "approved_at": APPROVED_AT, "release_date": None})
    assert xp.find_backfill_candidates(sb, TODAY) == []


def test_sequential_max_cards_counts_only_attempts():
    """行を作らない結果（missing / not_postable）では max_cards の枠を消費しない"""
    sb = _sb()
    _add_card(sb, 2, approved=False)          # not_postable
    _add_card(sb, 3)
    poster = Poster()
    counts = xp.post_cards_sequential(sb, [500, 2, 1, 3], post_fn=poster, max_cards=1)
    assert counts == {"missing": 1, "not_postable": 1, "posted": 1}
    assert len(poster.calls) == 1 and [r["card_id"] for r in _rows(sb)] == [1]


def test_sequential_skipped_duplicate_does_not_consume_slot():
    sb = _sb(card={"name": "同名"}, posts=[{"card_id": 50, "status": "posted", "attempts": 0}])
    sb.tables["unreleased_cards"].append({"id": 50, "name": "同名", "status": "approved", "hidden": False,
                                          "approved_at": APPROVED_AT, "release_date": None})
    _add_card(sb, 3)
    counts = xp.post_cards_sequential(sb, [1, 3], post_fn=Poster(), max_cards=1)
    assert counts == {"skipped_duplicate": 1, "posted": 1}


# ══════════════════════════════════════════════
# 低1: 同名重複判定は _post_lock の内側
# ══════════════════════════════════════════════

def test_same_name_concurrent_posts_only_one():
    sb = _sb(card={"name": "同名カード"})
    _add_card(sb, 2)
    sb.tables["unreleased_cards"][1]["name"] = "同名カード"

    def slow_poster(text, *args, **kw):
        time.sleep(0.2)   # 1枚目の送信中に2枚目が重複判定へ来る
        return ("t1", None)

    results = {}

    def run(cid):
        results[cid] = xp.post_card(sb, cid, post_fn=slow_poster, today=TODAY, interval=0)

    threads = [threading.Thread(target=run, args=(cid,)) for cid in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results.values()) == ["posted", "skipped_duplicate"]


# ══════════════════════════════════════════════
# 低2: 編集保存時の再判定は書き込み直前の最新 extraction_raw に対して行う
# ══════════════════════════════════════════════

def test_put_recompute_does_not_clobber_concurrent_extraction_raw_write(monkeypatch, fresh_context_cache):
    sb = FakeSB({"unreleased_cards": [_ann_row(1, "カードA", "旧")]})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)

    def fake_check(card, others, ctx, **kw):
        # 判定の計算中に別の書き込み（クロップ状態・承認記録）が入った状況
        sb.tables["unreleased_cards"][0]["extraction_raw"]["crop_state"] = {"mode": "manual"}
        sb.tables["unreleased_cards"][0]["extraction_raw"]["approval_snapshot"] = {"via": "single"}
        return {"version": 1, "status": "checked", "reason": None, "strong": 0, "weak": 0,
                "suspects": [], "unverified_fields": []}

    monkeypatch.setattr(aa, "compute_text_check", fake_check)
    resp = _client().put("/api/admin/unreleased/1", headers=HEADERS, json={"effect_text": "直した。"})
    assert resp.status_code == 200
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw["crop_state"] == {"mode": "manual"} and raw["approval_snapshot"] == {"via": "single"}
    assert raw["text_check"]["strong"] == 0 and "auto_approve_verdict" in raw


# ══════════════════════════════════════════════
# 低3: rate_limited の再挑戦に上限
# ══════════════════════════════════════════════

def _old(hours):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_rate_limited_gives_up_after_max_age(notices):
    sb = _sb(posts=[{"card_id": 1, "status": "rate_limited", "attempts": 0, "created_at": _old(25),
                     "updated_at": _old(3)}])
    assert xp.post_card(sb, 1, post_fn=Poster((None, "rate_limited")), today=TODAY) == "rate_limited"
    row = _rows(sb)[0]
    assert row["status"] == "failed" and row["attempts"] == xp.MAX_ATTEMPTS
    assert xp.find_backfill_candidates(sb, TODAY) == []     # もう拾わない
    assert len(notices) == 1 and "打ち切り" in notices[0]    # 通知は1回


def test_rate_limited_gives_up_after_max_count(notices):
    sb = _sb(posts=[{"card_id": 1, "status": "rate_limited", "attempts": 0, "created_at": _old(1),
                     "rate_limit_count": xp.RATE_LIMIT_MAX_COUNT - 1, "updated_at": _old(1)}])
    xp.post_card(sb, 1, post_fn=Poster((None, "rate_limited")), today=TODAY)
    assert _rows(sb)[0]["status"] == "failed" and "打ち切り" in notices[0]


def test_rate_limited_under_limits_keeps_retrying_and_counts(notices):
    sb = _sb()
    xp.post_card(sb, 1, post_fn=Poster((None, "rate_limited")), today=TODAY)
    xp.post_card(sb, 1, post_fn=Poster((None, "rate_limited")), today=TODAY)
    row = _rows(sb)[0]
    assert row["status"] == "rate_limited" and row["rate_limit_count"] == 2
    assert xp.find_backfill_candidates(sb, TODAY) == [1]


# ══════════════════════════════════════════════
# 低4: 取り残し通知は同じ行について1日1回
# ══════════════════════════════════════════════

def test_stuck_notice_once_per_day_per_row(notices):
    sb = _sb(posts=[{"card_id": 1, "status": "unknown", "attempts": 0, "updated_at": _old(5)},
                    {"card_id": 2, "status": "posting", "attempts": 0, "updated_at": _old(5)}])
    now = datetime.now(timezone.utc)
    assert xp.notify_stuck_rows(sb, now=now) == 2 and len(notices) == 1
    assert xp.notify_stuck_rows(sb, now=now + timedelta(hours=3)) == 0 and len(notices) == 1   # 3時間後は出さない
    # 新しく取り残された行だけは通知される
    _rows(sb).append({"card_id": 3, "status": "unknown", "attempts": 0, "updated_at": _old(5)})
    assert xp.notify_stuck_rows(sb, now=now + timedelta(hours=3)) == 1 and len(notices) == 2
    assert "card_id=3" in notices[1] and "card_id=1 " not in notices[1]
    # 1日たてば再度出す（card_id=3 は +3h に通知済みなので、+25h 時点ではまだ22時間しか経っておらず対象外）
    assert xp.notify_stuck_rows(sb, now=now + timedelta(hours=25)) == 2 and len(notices) == 3


# ══════════════════════════════════════════════
# 低5: unknown 通知の注意書き
# ══════════════════════════════════════════════

def test_unknown_notice_mentions_skipped_duplicate(notices):
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster((None, "unknown")), today=TODAY) == "unknown"
    assert "skipped_duplicate" in notices[0] and "行も削除" in notices[0]


def test_stuck_notice_mentions_skipped_duplicate(notices):
    sb = _sb(posts=[{"card_id": 1, "status": "unknown", "attempts": 0, "updated_at": _old(5)}])
    xp.notify_stuck_rows(sb)
    assert "skipped_duplicate" in notices[0]


# ══════════════════════════════════════════════
# 7: 一括承認の承認記録の保存失敗をまとめて通知
# ══════════════════════════════════════════════

class _SnapshotWriteFailQuery(Query):
    """extraction_raw に boom 印があり approval_snapshot を書く UPDATE だけ失敗させる"""

    def execute(self):
        raw = (self.payload or {}).get("extraction_raw") if self.op == "update" else None
        if isinstance(raw, dict) and raw.get("boom") and "approval_snapshot" in raw:
            raise RuntimeError("write failed")
        return super().execute()


def _bulk_setup(monkeypatch, rows, query_cls=None):
    sb = FakeSB({"unreleased_cards": rows})
    if query_cls:
        monkeypatch.setattr(sb, "table", lambda name: query_cls(sb, name))
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date",
                        lambda product: {"status": "unverified", "release_date": None})
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda card_id, **kw: (False, "x"))
    monkeypatch.setattr(admin_module.threading, "Thread", _SyncThread)
    sent = []
    import discord_notify
    monkeypatch.setattr(discord_notify, "send_discord_message", sent.append)
    return sb, sent


def test_bulk_approve_notifies_snapshot_failures_once(monkeypatch):
    rows = [_approval_row(1), _approval_row(2, extraction_raw={"boom": True}),
            _approval_row(3, extraction_raw={"boom": True})]
    _sb_, sent = _bulk_setup(monkeypatch, rows, _SnapshotWriteFailQuery)
    resp = _client().post("/api/admin/unreleased/bulk-approve", headers=HEADERS, json={"ids": [1, 2, 3]})
    assert resp.status_code == 200
    assert len(sent) == 1 and "2, 3" in sent[0] and "2件" in sent[0]


def test_bulk_approve_no_notice_when_all_snapshots_saved(monkeypatch):
    _sb_, sent = _bulk_setup(monkeypatch, [_approval_row(1), _approval_row(2)])
    assert _client().post("/api/admin/unreleased/bulk-approve", headers=HEADERS,
                          json={"ids": [1, 2]}).status_code == 200
    assert sent == []


def test_save_approval_snapshots_reports_failed_ids():
    sb = FakeSB({"unreleased_cards": [{"id": 1, "extraction_raw": {}}]})
    failed = []
    n = aa.save_approval_snapshots(sb, [(1, {"via": "bulk"}), (9, {"via": "bulk"})], failed)  # 9 は行なし
    assert n == 1 and failed == [9]
