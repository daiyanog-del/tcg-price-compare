"""
tests/test_reconcile_near_match.py -- 未発売カード照合の「1字違い」判定と
管理API POST /api/admin/unreleased/<id>/link のテスト。

背景:
  画像AIが珍しい漢字を1字誤読して登録したカードは、発売後もファジーキー完全一致
  しないため永久に approved のまま残っていた。release_date が過去のカードに限り
  1字違いの正式名を候補として needs_review に回し、管理画面から人が紐付ける。

テスト方針:
  実 Supabase / ygores は使わず fake を monkeypatch で差し込む（ネットワーク不使用）。
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import admin_unreleased as admin_module
import reconcile_unreleased as target
from frozen_clock import freeze_now

# reconcile 側は JST 基準で「今日」を判定する。テストも同じ基準で相対日付を作る
# （UTC 環境の UTC15〜24時に date.today() とずれて境界テストが落ちるのを防ぐ）。
# 収集と実行のあいだに JST の0時をまたいでもずれないよう、本体の時計をこの時刻に固定する
_NOW = datetime.now(target.JST)
_TODAY = _NOW.date()
FUTURE = (_TODAY + timedelta(days=30)).isoformat()
PAST = (_TODAY - timedelta(days=30)).isoformat()
DAYS_6_AGO = (_TODAY - timedelta(days=6)).isoformat()
DAYS_7_AGO = (_TODAY - timedelta(days=7)).isoformat()


@pytest.fixture(autouse=True)
def _freeze_reconcile_clock(monkeypatch):
    freeze_now(monkeypatch, target, _NOW)


# ──────────────────────────────────────────────
# fake Supabase client（unreleased_cards のみ）
# ──────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._op = "select"
        self._data = None
        self._filters = []

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def update(self, data):
        self._op = "update"
        self._data = data
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def _match(self, row):
        for typ, col, val in self._filters:
            if typ == "eq" and row.get(col) != val:
                return False
            if typ == "in" and row.get(col) not in val:
                return False
            if typ == "is" and val == "null" and row.get(col) is not None:
                return False
        return True

    def execute(self):
        matched = [r for r in self._rows if self._match(r)]
        if self._op == "update":
            for r in matched:
                r.update(self._data)
        return SimpleNamespace(data=matched)


class _FakeSupabase:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        assert name == "unreleased_cards"
        return _FakeQuery(self.rows)


# ──────────────────────────────────────────────
# 1字違い判定
# ──────────────────────────────────────────────

def test_one_char_diff_match():
    assert target._is_one_char_diff("屠欺龍", "蜃欺龍") is True


def test_one_char_diff_two_chars_is_false():
    assert target._is_one_char_diff("屠欺龍", "蜃欺竜") is False


def test_one_char_diff_length_differs_is_false():
    assert target._is_one_char_diff("屠欺龍", "屠欺龍の") is False


def test_one_char_diff_identical_is_false():
    assert target._is_one_char_diff("屠欺龍", "屠欺龍") is False


def test_find_near_candidates_dedup_and_same_length_only():
    name_index = {
        "蜃欺龍": [100],
        "蜃欺竜": [101],      # 2字違い
        "蜃欺龍の": [102],    # 長さ違い
        "別名カード": [103],
    }
    names_idx = target._build_fuzzy_names_index(name_index)
    length_idx = target._build_length_index(names_idx)
    found = target._find_near_candidates(target.fuzzy_key("屠欺龍"), names_idx, length_idx)
    assert found == [{"konami_id": 100, "name": "蜃欺龍"}]


def test_make_review_truncates_display_but_keeps_all_ids():
    name_index = {chr(0x4E00 + i) + "欺龍": [1000 + i] for i in range(15)}
    names_idx = target._build_fuzzy_names_index(name_index)
    length_idx = target._build_length_index(names_idx)
    found = target._find_near_candidates(target.fuzzy_key("屠欺龍"), names_idx, length_idx)
    assert len(found) == 15
    rc = target._make_review("near_match", found)
    assert len(rc["candidates"]) == target.MAX_NEAR_CANDIDATES == 10
    assert rc["total_candidates"] == 15
    assert rc["all_candidate_ids"] == [1000 + i for i in range(15)]


def test_build_fuzzy_names_index_skips_non_integer_id(monkeypatch):
    idx = target._build_fuzzy_names_index({"正常": [1], "壊れ": ["abc"]})
    assert idx[target.fuzzy_key("正常")] == [("正常", 1)]
    assert idx[target.fuzzy_key("壊れ")] == []


# ──────────────────────────────────────────────
# reconcile() の分岐（status は approved のまま・印だけ付ける）
# ──────────────────────────────────────────────

def _run(monkeypatch, cards, name_index):
    monkeypatch.setattr(target._ygores_repo, "get_name_index", lambda: name_index)
    sb = _FakeSupabase(cards)
    summary = target.reconcile(sb)
    return summary, cards


def _card(cid, name, release_date, status="approved", review=None):
    return {"id": cid, "name": name, "release_date": release_date,
            "status": status, "konami_id": None, "review_candidates": review}


def test_past_release_one_char_diff_marks_review_and_keeps_approved(monkeypatch):
    cards = [_card(1, "屠欺龍", PAST)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["status"] == "approved"  # 公開を維持
    assert rows[0]["konami_id"] is None     # 自動 linked しない
    rc = rows[0]["review_candidates"]
    assert rc["reason"] == "near_match"
    assert rc["candidates"] == [{"konami_id": 100, "name": "蜃欺龍"}]
    assert rc["total_candidates"] == 1
    assert rc["checked_at"]
    assert rc["dismissed_at"] is None
    assert summary["near_match"] == 1
    assert summary["review_marked"] == 1
    assert summary["linked"] == 0


def test_past_release_two_char_diff_is_no_match(monkeypatch):
    cards = [_card(1, "屠欺龍", PAST)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺竜": [100]})
    assert rows[0]["status"] == "approved"
    rc = rows[0]["review_candidates"]
    assert rc["reason"] == "release_passed_no_match"
    assert rc["candidates"] == []
    assert rc["total_candidates"] == 0
    assert summary["release_passed_no_match"] == 1
    assert summary["near_match"] == 0


def test_future_release_is_skipped_even_if_similar(monkeypatch):
    cards = [_card(1, "屠欺龍", FUTURE)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["status"] == "approved"
    assert rows[0]["review_candidates"] is None
    assert summary["skipped"] == 1
    assert summary["review_marked"] == 0


def test_null_release_is_skipped(monkeypatch):
    cards = [_card(1, "屠欺龍", None)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["review_candidates"] is None
    assert summary["skipped"] == 1


def test_grace_period_boundary_6_days_ago_is_skipped(monkeypatch):
    cards = [_card(1, "屠欺龍", DAYS_6_AGO)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["review_candidates"] is None
    assert summary["skipped"] == 1
    assert summary["review_marked"] == 0


def test_grace_period_boundary_7_days_ago_is_judged(monkeypatch):
    cards = [_card(1, "屠欺龍", DAYS_7_AGO)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["status"] == "approved"
    assert rows[0]["review_candidates"]["reason"] == "near_match"
    assert summary["near_match"] == 1


def test_multiple_exact_saves_candidates_and_keeps_approved(monkeypatch):
    cards = [_card(1, "同名カード", FUTURE)]
    summary, rows = _run(monkeypatch, cards, {"同名カード": [200, 201]})
    assert rows[0]["status"] == "approved"
    rc = rows[0]["review_candidates"]
    assert rc["reason"] == "multiple_exact"
    assert rc["candidates"] == [
        {"konami_id": 200, "name": "同名カード"},
        {"konami_id": 201, "name": "同名カード"},
    ]
    assert rc["total_candidates"] == 2
    assert summary["multiple_exact"] == 1
    assert summary["review_marked"] == 1


def _dismissed(reason, ids):
    return {"reason": reason, "candidates": [{"konami_id": i, "name": "x"} for i in sorted(ids)[:10]],
            "total_candidates": len(ids), "all_candidate_ids": sorted(ids), "checked_at": "x",
            "dismissed_at": "2026-10-01T00:00:00+00:00",
            "dismissed_signature": {"reason": reason, "candidate_ids": sorted(ids)}}


def test_dismissed_card_with_same_signature_is_not_overwritten(monkeypatch):
    dismissed = _dismissed("near_match", [100])
    cards = [_card(1, "屠欺龍", PAST, review=dict(dismissed))]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["review_candidates"] == dismissed
    assert summary["dismissed_skipped"] == 1
    assert summary["review_marked"] == 0


def test_dismissed_card_is_remarked_when_new_candidate_appears(monkeypatch):
    cards = [_card(1, "屠欺龍", PAST, review=_dismissed("near_match", [100]))]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100], "蛋欺龍": [101]})
    rc = rows[0]["review_candidates"]
    assert rc["dismissed_at"] is None
    assert "dismissed_signature" not in rc
    assert {c["konami_id"] for c in rc["candidates"]} == {100, 101}
    assert summary["review_marked"] == 1
    assert summary["dismissed_skipped"] == 0


def test_dismissed_with_over_10_candidates_remarked_when_larger_id_added(monkeypatch):
    """候補が10件を超えても、切り詰めで見えない大きい konami_id の新規候補を見逃さない。"""
    base = {chr(0x4E00 + i) + "欺龍": [1000 + i] for i in range(12)}
    ids = [1000 + i for i in range(12)]
    cards = [_card(1, "屠欺龍", PAST, review=_dismissed("near_match", ids))]
    # 同じ候補のまま → 確認済みを維持
    summary, rows = _run(monkeypatch, cards, dict(base))
    assert summary["dismissed_skipped"] == 1
    # konami_id が最大の1字違いカードが追加された（上位10件の外側）→ 再付与
    extra = dict(base)
    extra[chr(0x4E00 + 20) + "欺龍"] = [9999]
    cards = [_card(1, "屠欺龍", PAST, review=_dismissed("near_match", ids))]
    summary, rows = _run(monkeypatch, cards, extra)
    rc = rows[0]["review_candidates"]
    assert rc["dismissed_at"] is None
    assert 9999 in rc["all_candidate_ids"]
    assert 9999 not in [c["konami_id"] for c in rc["candidates"]]  # 表示は上位10件のまま
    assert summary["review_marked"] == 1


def test_dismissed_card_is_remarked_when_reason_changes(monkeypatch):
    cards = [_card(1, "屠欺龍", PAST, review=_dismissed("near_match", [100]))]
    # 近い名前が消えた → release_passed_no_match（理由が変わる）
    summary, rows = _run(monkeypatch, cards, {"全然違う": [100]})
    rc = rows[0]["review_candidates"]
    assert rc["reason"] == "release_passed_no_match"
    assert rc["dismissed_at"] is None
    assert summary["release_passed_no_match"] == 1


def test_dismissed_without_signature_is_remarked(monkeypatch):
    old = {"reason": "near_match", "candidates": [], "dismissed_at": "2026-10-01T00:00:00+00:00"}
    cards = [_card(1, "屠欺龍", PAST, review=old)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["review_candidates"]["dismissed_at"] is None
    assert summary["review_marked"] == 1


def test_stale_review_cleared_when_skipped(monkeypatch):
    stale = {"reason": "near_match", "candidates": [], "dismissed_at": None}
    cards = [_card(1, "屠欺龍", FUTURE, review=dict(stale)),      # 未来日
             _card(2, "屠欺龍B", DAYS_6_AGO, review=dict(stale)),  # 猶予期間内
             _card(3, "屠欺龍C", None, review=dict(stale))]       # NULL
    summary, rows = _run(monkeypatch, cards, {"無関係": [1]})
    assert all(r["review_candidates"] is None for r in rows)
    assert summary["review_cleared"] == 3
    assert summary["skipped"] == 3


def test_dismissed_review_kept_when_skipped(monkeypatch):
    d = _dismissed("near_match", [100])
    cards = [_card(1, "屠欺龍", FUTURE, review=dict(d))]
    summary, rows = _run(monkeypatch, cards, {"無関係": [1]})
    assert rows[0]["review_candidates"] == d
    assert summary["review_cleared"] == 0


def test_fetch_failure_counts_as_error(monkeypatch):
    monkeypatch.setattr(target._ygores_repo, "get_name_index", lambda: {"x": [1]})

    class _Boom:
        def table(self, _n):
            raise RuntimeError("down")

    summary = target.reconcile(_Boom())
    assert summary["error"] == 1


def test_main_exits_1_on_fetch_failure(monkeypatch):
    import pytest
    monkeypatch.setattr(target, "_get_supabase", lambda: object())
    monkeypatch.setattr(target, "reconcile", lambda sb: {
        "linked": 0, "review_marked": 0, "multiple_exact": 0, "near_match": 0,
        "release_passed_no_match": 0, "dismissed_skipped": 0, "review_cleared": 0,
        "skipped": 0, "conflict": 0, "error": 1})
    monkeypatch.setattr(target, "_cleanup_stale_cards",
                        lambda sb: {"linked_deleted": 0, "rejected_deleted": 0, "error": 0})
    with pytest.raises(SystemExit) as ei:
        target.main()
    assert ei.value.code == 1


def test_dismissed_card_still_linked_on_exact_single_match(monkeypatch):
    dismissed = {"reason": "near_match", "dismissed_at": "2026-10-01T00:00:00+00:00"}
    cards = [_card(1, "正式カード", PAST, review=dismissed)]
    summary, rows = _run(monkeypatch, cards, {"正式カード": [300]})
    assert rows[0]["status"] == "linked"
    assert rows[0]["konami_id"] == 300
    assert rows[0]["review_candidates"] is None
    assert summary["linked"] == 1


def test_exact_single_links_and_clears_review_candidates(monkeypatch):
    c = _card(1, "正式カード", PAST, review={"reason": "near_match", "candidates": []})
    summary, rows = _run(monkeypatch, [c], {"正式カード": [300]})
    assert rows[0]["status"] == "linked"
    assert rows[0]["konami_id"] == 300
    assert rows[0]["review_candidates"] is None
    assert rows[0]["linked_at"]
    assert summary["linked"] == 1


def test_update_has_status_approved_condition(monkeypatch):
    """照合の取得後に管理者が状態を変えた行（却下など）は、UPDATE されずに conflict になる。"""
    monkeypatch.setattr(target._ygores_repo, "get_name_index",
                        lambda: {"正式カード": [300], "蜃欺龍": [100]})
    rows = [_card(1, "正式カード", PAST), _card(2, "屠欺龍", PAST)]
    sb = _FakeSupabase(rows)
    # 取得後・更新前に status が変わった状況を再現する
    monkeypatch.setattr(target, "_fetch_approved_cards", lambda _sb: [dict(r) for r in rows])
    rows[0]["status"] = "rejected"
    rows[1]["status"] = "rejected"
    summary = target.reconcile(sb)
    assert summary["conflict"] == 2
    assert summary["linked"] == 0 and summary["review_marked"] == 0
    assert rows[0]["status"] == "rejected" and rows[0]["konami_id"] is None
    assert rows[1]["review_candidates"] is None


# ──────────────────────────────────────────────
# link API
# ──────────────────────────────────────────────

HEADERS = {"X-Admin-Key": "test-key"}


def _install(monkeypatch, rows, name_index=None):
    fake = _FakeSupabase(rows)
    monkeypatch.setattr(admin_module, "_supabase", fake, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    monkeypatch.setattr(admin_module._ygores_repo, "get_name_index",
                        lambda: name_index if name_index is not None else {"蜃欺龍": [100], "同名": [200, 201]})
    monkeypatch.setattr(admin_module._card_display, "invalidate_cache", lambda: None)
    return fake


def _client():
    import app as app_module
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _row(cid, status):
    return {"id": cid, "name": "屠欺龍", "status": status, "konami_id": None,
            "linked_at": None,
            "review_candidates": {"reason": "near_match", "candidates": [], "dismissed_at": None}}


def test_link_success(monkeypatch):
    rows = [_row(1, "approved")]
    _install(monkeypatch, rows)
    resp = _client().post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 100})
    assert resp.status_code == 200
    assert rows[0]["status"] == "linked"
    assert rows[0]["konami_id"] == 100
    assert rows[0]["linked_at"]
    assert rows[0]["review_candidates"] is None


def test_link_accepts_needs_review(monkeypatch):
    rows = [_row(1, "needs_review")]
    _install(monkeypatch, rows)
    assert _client().post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 200}).status_code == 200
    assert rows[0]["status"] == "linked"


def test_link_rejects_pending(monkeypatch):
    rows = [_row(1, "pending")]
    _install(monkeypatch, rows)
    resp = _client().post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 100})
    assert resp.status_code == 400
    assert rows[0]["status"] == "pending"
    assert rows[0]["konami_id"] is None


def test_link_unknown_konami_id_400(monkeypatch):
    rows = [_row(1, "approved")]
    _install(monkeypatch, rows)
    resp = _client().post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 999})
    assert resp.status_code == 400
    assert rows[0]["status"] == "approved"


def test_link_non_integer_konami_id_400(monkeypatch):
    rows = [_row(1, "approved")]
    _install(monkeypatch, rows)
    client = _client()
    for bad in ("100", 100.5, None, True, [100]):
        resp = client.post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": bad})
        assert resp.status_code == 400, bad
    assert rows[0]["status"] == "approved"


def test_link_rejected_or_linked_status_400(monkeypatch):
    rows = [_row(1, "rejected"), _row(2, "linked")]
    _install(monkeypatch, rows)
    client = _client()
    assert client.post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 100}).status_code == 400
    assert client.post("/api/admin/unreleased/2/link", headers=HEADERS, json={"konami_id": 100}).status_code == 400
    assert rows[0]["status"] == "rejected"


def test_link_not_found_404(monkeypatch):
    _install(monkeypatch, [])
    resp = _client().post("/api/admin/unreleased/9/link", headers=HEADERS, json={"konami_id": 100})
    assert resp.status_code == 404


def test_link_requires_admin_key(monkeypatch):
    _install(monkeypatch, [_row(1, "approved")])
    resp = _client().post("/api/admin/unreleased/1/link", json={"konami_id": 100})
    assert resp.status_code == 401


# ──────────────────────────────────────────────
# review-dismiss API
# ──────────────────────────────────────────────

def test_review_dismiss_sets_dismissed_at_and_keeps_status(monkeypatch):
    rows = [_row(1, "approved")]
    _install(monkeypatch, rows)
    resp = _client().post("/api/admin/unreleased/1/review-dismiss", headers=HEADERS)
    assert resp.status_code == 200
    assert rows[0]["status"] == "approved"
    assert rows[0]["review_candidates"]["dismissed_at"]
    assert rows[0]["review_candidates"]["reason"] == "near_match"  # 他の中身は保持


def test_review_dismiss_without_review_400(monkeypatch):
    rows = [_row(1, "approved")]
    rows[0]["review_candidates"] = None
    _install(monkeypatch, rows)
    assert _client().post("/api/admin/unreleased/1/review-dismiss", headers=HEADERS).status_code == 400


def test_review_dismiss_non_approved_400(monkeypatch):
    rows = [_row(1, "needs_review"), _row(2, "linked")]
    _install(monkeypatch, rows)
    client = _client()
    assert client.post("/api/admin/unreleased/1/review-dismiss", headers=HEADERS).status_code == 400
    assert client.post("/api/admin/unreleased/2/review-dismiss", headers=HEADERS).status_code == 400


def test_review_dismiss_not_found_and_auth(monkeypatch):
    _install(monkeypatch, [_row(1, "approved")])
    client = _client()
    assert client.post("/api/admin/unreleased/9/review-dismiss", headers=HEADERS).status_code == 404
    assert client.post("/api/admin/unreleased/1/review-dismiss").status_code == 401


def test_review_dismiss_records_signature(monkeypatch):
    rows = [_row(1, "approved")]
    rows[0]["review_candidates"]["candidates"] = [
        {"konami_id": 300, "name": "a"}, {"konami_id": 100, "name": "b"}]
    _install(monkeypatch, rows)
    assert _client().post("/api/admin/unreleased/1/review-dismiss", headers=HEADERS).status_code == 200
    assert rows[0]["review_candidates"]["dismissed_signature"] == {
        "reason": "near_match", "candidate_ids": [100, 300]}


def test_review_dismiss_signature_uses_all_candidate_ids(monkeypatch):
    rows = [_row(1, "approved")]
    rows[0]["review_candidates"].update(
        candidates=[{"konami_id": 100, "name": "a"}], all_candidate_ids=[100, 200, 300])
    _install(monkeypatch, rows)
    assert _client().post("/api/admin/unreleased/1/review-dismiss", headers=HEADERS).status_code == 200
    assert rows[0]["review_candidates"]["dismissed_signature"]["candidate_ids"] == [100, 200, 300]


def test_review_dismiss_rejects_multiple_exact(monkeypatch):
    rows = [_row(1, "approved")]
    rows[0]["review_candidates"]["reason"] = "multiple_exact"
    _install(monkeypatch, rows)
    assert _client().post("/api/admin/unreleased/1/review-dismiss", headers=HEADERS).status_code == 400
    assert rows[0]["review_candidates"]["dismissed_at"] is None


# ──────────────────────────────────────────────
# 編集API（PUT）で name / release_date が変わったら印を消す
# ──────────────────────────────────────────────

def _put_row():
    r = _row(1, "approved")
    r["release_date"] = "2026-09-01"
    return r


def test_put_name_change_clears_review(monkeypatch):
    rows = [_put_row()]
    _install(monkeypatch, rows)
    resp = _client().put("/api/admin/unreleased/1", headers=HEADERS, json={"name": "蜃欺龍"})
    assert resp.status_code == 200
    assert rows[0]["name"] == "蜃欺龍"
    assert rows[0]["review_candidates"] is None


def test_put_release_date_change_clears_review(monkeypatch):
    rows = [_put_row()]
    _install(monkeypatch, rows)
    resp = _client().put("/api/admin/unreleased/1", headers=HEADERS, json={"release_date": "2027-01-01"})
    assert resp.status_code == 200
    assert rows[0]["review_candidates"] is None


def test_put_unchanged_name_or_other_field_keeps_review(monkeypatch):
    rows = [_put_row()]
    _install(monkeypatch, rows)
    client = _client()
    # 同じ name・同じ release_date の再保存、無関係フィールドの変更では消さない
    assert client.put("/api/admin/unreleased/1", headers=HEADERS,
                      json={"name": "屠欺龍", "release_date": "2026-09-01", "reading": "とぎりゅう"}).status_code == 200
    assert rows[0]["review_candidates"] is not None
