"""
tests/test_reconcile_unreleased_cleanup.py -- reconcile_unreleased.py の
不要レコード自動削除ロジック（_cleanup_stale_cards）のテスト。

背景:
  unreleased_cards には linked_at 列があり、以下2種類の自動削除を行う:
    1) status='linked' かつ linked_at が7日以上前 -> 物理削除
    2) status='rejected' かつ (ygoresと一致 or release_dateが過去) -> 物理削除
  削除前に official_card_images の storage_path を Supabase Storage
  （バケット official-card-images）から削除する。

テスト方針:
  実 Supabase を使わず、unreleased_cards / official_card_images テーブルと
  Storage バケットを模した fake client を monkeypatch で差し込む
  （ネットワーク不使用）。ygores 名前インデックスも monkeypatch で固定する。
"""

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reconcile_unreleased as target


# ──────────────────────────────────────────────
# fake Supabase client
# ──────────────────────────────────────────────

class _FakeQuery:
    """unreleased_cards / official_card_images 共用の簡易クエリビルダ。
    select/update/delete と eq/lte/in_ フィルタ、execute() のみ模倣する。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._op = "select"
        self._update_data = None
        self._filters = []

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def update(self, data):
        self._op = "update"
        self._update_data = data
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def lte(self, col, val):
        self._filters.append(("lte", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def _matched(self):
        result = []
        for row in self._rows:
            ok = True
            for typ, col, val in self._filters:
                if typ == "eq" and row.get(col) != val:
                    ok = False
                elif typ == "lte":
                    rv = row.get(col)
                    if rv is None or rv > val:
                        ok = False
                elif typ == "in" and row.get(col) not in val:
                    ok = False
                elif typ == "is":
                    none_expected = val in ("null", None)
                    if none_expected and row.get(col) is not None:
                        ok = False
                    elif not none_expected and row.get(col) is None:
                        ok = False
            if ok:
                result.append(row)
        return result

    def execute(self):
        matched = self._matched()
        if self._op == "update":
            for row in matched:
                row.update(self._update_data)
        elif self._op == "delete":
            for row in matched:
                self._rows.remove(row)
        return SimpleNamespace(data=matched)


class _FakeStorageBucket:
    def __init__(self, removed_paths: list, fail_paths: set | None = None):
        self._removed_paths = removed_paths
        self._fail_paths = fail_paths or set()

    def remove(self, paths):
        if any(p in self._fail_paths for p in paths):
            raise RuntimeError("Storage remove failed (simulated)")
        self._removed_paths.extend(paths)


class _FakeStorage:
    def __init__(self, removed_paths: list, fail_paths: set | None = None):
        self._bucket = _FakeStorageBucket(removed_paths, fail_paths)

    def from_(self, _bucket_name):
        return self._bucket


class _FakeSupabase:
    def __init__(self, unreleased_rows, image_rows=None, fail_storage_paths=None):
        self.unreleased_rows = unreleased_rows
        self.image_rows = image_rows or []
        self.removed_storage_paths: list = []
        self.storage = _FakeStorage(self.removed_storage_paths, fail_storage_paths)

    def table(self, name):
        if name == "unreleased_cards":
            return _FakeQuery(self.unreleased_rows)
        if name == "official_card_images":
            return _FakeQuery(self.image_rows)
        raise AssertionError(f"unexpected table: {name}")


def _install_fake_ygores(monkeypatch, matched_names: list[str]):
    """ygores 名前インデックスを固定する（konami_id はダミー）。"""
    name_index = {name: 999 for name in matched_names}
    monkeypatch.setattr(
        target._ygores_repo, "get_name_index", lambda: name_index, raising=False
    )


# ──────────────────────────────────────────────
# 1) linked クリーンアップ
# ──────────────────────────────────────────────

def test_linked_7days_or_more_is_deleted(monkeypatch):
    old_linked_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    rows = [
        {"id": 1, "name": "カードA", "status": "linked", "linked_at": old_linked_at,
         "release_date": None},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    assert summary["linked_deleted"] == 1
    assert summary["error"] == 0
    assert sb.unreleased_rows == []


def test_linked_less_than_7days_is_not_deleted(monkeypatch):
    recent_linked_at = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    rows = [
        {"id": 1, "name": "カードA", "status": "linked", "linked_at": recent_linked_at,
         "release_date": None},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    assert summary["linked_deleted"] == 0
    assert len(sb.unreleased_rows) == 1


def test_approved_not_linked_is_not_targeted(monkeypatch):
    rows = [
        {"id": 1, "name": "カードA", "status": "approved", "linked_at": None,
         "release_date": None},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    assert summary["linked_deleted"] == 0
    assert len(sb.unreleased_rows) == 1


def test_linked_deletion_removes_storage_images(monkeypatch):
    old_linked_at = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    rows = [
        {"id": 1, "name": "カードA", "status": "linked", "linked_at": old_linked_at,
         "release_date": None},
    ]
    image_rows = [
        {"unreleased_card_id": 1, "storage_path": "unreleased/1/front.jpg"},
    ]
    sb = _FakeSupabase(rows, image_rows=image_rows)
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    assert summary["linked_deleted"] == 1
    assert sb.removed_storage_paths == ["unreleased/1/front.jpg"]


# ──────────────────────────────────────────────
# 2) rejected クリーンアップ
# ──────────────────────────────────────────────

def test_rejected_matching_ygores_is_deleted(monkeypatch):
    rows = [
        {"id": 1, "name": "カードX", "status": "rejected", "linked_at": None,
         "release_date": None},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, ["カードX"])

    summary = target._cleanup_stale_cards(sb)

    assert summary["rejected_deleted"] == 1
    assert sb.unreleased_rows == []


def test_rejected_no_ygores_match_but_past_release_date_is_deleted(monkeypatch):
    past = (date.today() - timedelta(days=1)).isoformat()
    rows = [
        {"id": 1, "name": "カードY", "status": "rejected", "linked_at": None,
         "release_date": past},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, [])  # 一致なし

    summary = target._cleanup_stale_cards(sb)

    assert summary["rejected_deleted"] == 1
    assert sb.unreleased_rows == []


def test_rejected_no_match_and_future_release_date_is_not_deleted(monkeypatch):
    future = (date.today() + timedelta(days=10)).isoformat()
    rows = [
        {"id": 1, "name": "カードZ", "status": "rejected", "linked_at": None,
         "release_date": future},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    assert summary["rejected_deleted"] == 0
    assert len(sb.unreleased_rows) == 1


def test_rejected_no_match_and_null_release_date_is_not_deleted(monkeypatch):
    rows = [
        {"id": 1, "name": "カードW", "status": "rejected", "linked_at": None,
         "release_date": None},
    ]
    sb = _FakeSupabase(rows)
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    assert summary["rejected_deleted"] == 0
    assert len(sb.unreleased_rows) == 1


# ──────────────────────────────────────────────
# Storage削除失敗時のフォールト分離
# ──────────────────────────────────────────────

def test_storage_delete_failure_skips_only_that_card(monkeypatch):
    old_linked_at = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
    rows = [
        {"id": 1, "name": "カードA", "status": "linked", "linked_at": old_linked_at,
         "release_date": None},
        {"id": 2, "name": "カードB", "status": "linked", "linked_at": old_linked_at,
         "release_date": None},
    ]
    image_rows = [
        {"unreleased_card_id": 1, "storage_path": "unreleased/1/front.jpg"},
        {"unreleased_card_id": 2, "storage_path": "unreleased/2/front.jpg"},
    ]
    sb = _FakeSupabase(
        rows, image_rows=image_rows,
        fail_storage_paths={"unreleased/1/front.jpg"},
    )
    _install_fake_ygores(monkeypatch, [])

    summary = target._cleanup_stale_cards(sb)

    # id=1 は Storage 削除失敗でスキップ（DB行も残る）、id=2 は正常削除される
    assert summary["linked_deleted"] == 1
    assert summary["error"] == 1
    remaining_ids = {r["id"] for r in sb.unreleased_rows}
    assert remaining_ids == {1}
    assert sb.removed_storage_paths == ["unreleased/2/front.jpg"]
