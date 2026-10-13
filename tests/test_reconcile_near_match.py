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
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_unreleased as admin_module
import reconcile_unreleased as target

FUTURE = (date.today() + timedelta(days=30)).isoformat()
PAST = (date.today() - timedelta(days=30)).isoformat()


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


# ──────────────────────────────────────────────
# reconcile() の分岐
# ──────────────────────────────────────────────

def _run(monkeypatch, cards, name_index):
    monkeypatch.setattr(target._ygores_repo, "get_name_index", lambda: name_index)
    sb = _FakeSupabase(cards)
    summary = target.reconcile(sb)
    return summary, cards


def _card(cid, name, release_date, status="approved"):
    return {"id": cid, "name": name, "release_date": release_date,
            "status": status, "konami_id": None, "review_candidates": None}


def test_past_release_one_char_diff_goes_needs_review_not_linked(monkeypatch):
    cards = [_card(1, "屠欺龍", PAST)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["status"] == "needs_review"
    assert rows[0]["konami_id"] is None  # 自動 linked しない
    rc = rows[0]["review_candidates"]
    assert rc["reason"] == "near_match"
    assert rc["candidates"] == [{"konami_id": 100, "name": "蜃欺龍"}]
    assert rc["checked_at"]
    assert summary["near_match"] == 1
    assert summary["needs_review"] == 1
    assert summary["linked"] == 0


def test_past_release_two_char_diff_is_no_match(monkeypatch):
    cards = [_card(1, "屠欺龍", PAST)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺竜": [100]})
    assert rows[0]["status"] == "needs_review"
    assert rows[0]["review_candidates"] == {
        "reason": "release_passed_no_match",
        "candidates": [],
        "checked_at": rows[0]["review_candidates"]["checked_at"],
    }
    assert summary["release_passed_no_match"] == 1
    assert summary["near_match"] == 0


def test_future_release_is_skipped_even_if_similar(monkeypatch):
    cards = [_card(1, "屠欺龍", FUTURE)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["status"] == "approved"
    assert rows[0]["review_candidates"] is None
    assert summary["skipped"] == 1
    assert summary["needs_review"] == 0


def test_null_release_is_skipped(monkeypatch):
    cards = [_card(1, "屠欺龍", None)]
    summary, rows = _run(monkeypatch, cards, {"蜃欺龍": [100]})
    assert rows[0]["status"] == "approved"
    assert summary["skipped"] == 1


def test_multiple_exact_saves_candidates(monkeypatch):
    cards = [_card(1, "同名カード", FUTURE)]
    summary, rows = _run(monkeypatch, cards, {"同名カード": [200, 201]})
    assert rows[0]["status"] == "needs_review"
    rc = rows[0]["review_candidates"]
    assert rc["reason"] == "multiple_exact"
    assert rc["candidates"] == [
        {"konami_id": 200, "name": "同名カード"},
        {"konami_id": 201, "name": "同名カード"},
    ]
    assert summary["needs_review"] == 1
    assert summary["near_match"] == 0


def test_exact_single_links_and_clears_review_candidates(monkeypatch):
    c = _card(1, "正式カード", PAST)
    c["review_candidates"] = {"reason": "near_match", "candidates": []}
    summary, rows = _run(monkeypatch, [c], {"正式カード": [300]})
    assert rows[0]["status"] == "linked"
    assert rows[0]["konami_id"] == 300
    assert rows[0]["review_candidates"] is None
    assert rows[0]["linked_at"]
    assert summary["linked"] == 1


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
            "linked_at": None, "review_candidates": {"reason": "near_match", "candidates": []}}


def test_link_success(monkeypatch):
    rows = [_row(1, "needs_review")]
    _install(monkeypatch, rows)
    resp = _client().post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 100})
    assert resp.status_code == 200
    assert rows[0]["status"] == "linked"
    assert rows[0]["konami_id"] == 100
    assert rows[0]["linked_at"]
    assert rows[0]["review_candidates"] is None


def test_link_accepts_pending_and_approved(monkeypatch):
    rows = [_row(1, "pending"), _row(2, "approved")]
    _install(monkeypatch, rows)
    client = _client()
    assert client.post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 200}).status_code == 200
    assert client.post("/api/admin/unreleased/2/link", headers=HEADERS, json={"konami_id": 201}).status_code == 200


def test_link_unknown_konami_id_400(monkeypatch):
    rows = [_row(1, "needs_review")]
    _install(monkeypatch, rows)
    resp = _client().post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": 999})
    assert resp.status_code == 400
    assert rows[0]["status"] == "needs_review"


def test_link_non_integer_konami_id_400(monkeypatch):
    rows = [_row(1, "needs_review")]
    _install(monkeypatch, rows)
    client = _client()
    for bad in ("100", 100.5, None, True, [100]):
        resp = client.post("/api/admin/unreleased/1/link", headers=HEADERS, json={"konami_id": bad})
        assert resp.status_code == 400, bad
    assert rows[0]["status"] == "needs_review"


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
    _install(monkeypatch, [_row(1, "needs_review")])
    resp = _client().post("/api/admin/unreleased/1/link", json={"konami_id": 100})
    assert resp.status_code == 401
