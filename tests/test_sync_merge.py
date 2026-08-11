"""
tests/test_sync_merge.py — 端末間同期(sync.py)の和集合マージ・バリデーション・
条件付き更新（競合→リトライ）のテスト

設計文書: docs/design-sync-2026-08-09.md（第2版）§13「テスト」節に挙がる項目を最低限カバーする。
P1スコープ（購入候補=wishlistのみ）。ネットワーク不使用、Supabaseクライアントはモックする。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync


# ── merge_wishlist ──

class TestMergeWishlist:
    def test_same_key_takes_larger_qty(self):
        server = [{"name": "青眼の白龍", "rarity": "", "qty": 2}]
        client = [{"name": "青眼の白龍", "rarity": "", "qty": 5}]
        merged = sync.merge_wishlist(server, client)
        assert merged == [{"name": "青眼の白龍", "rarity": "", "qty": 5}]

    def test_qty_capped_at_99(self):
        server = [{"name": "青眼の白龍", "rarity": "", "qty": 99}]
        client = [{"name": "青眼の白龍", "rarity": "", "qty": 500}]
        merged = sync.merge_wishlist(server, client)
        assert merged[0]["qty"] == 99

    def test_elements_only_on_one_side_survive(self):
        server = [{"name": "カードA", "rarity": "", "qty": 1}]
        client = [{"name": "カードB", "rarity": "", "qty": 1}]
        merged = sync.merge_wishlist(server, client)
        names = {m["name"] for m in merged}
        assert names == {"カードA", "カードB"}

    def test_key_is_tuple_name_with_space_does_not_collide(self):
        # 文字列連結キーだと "A" + " " + "B" と "A " + "" + "B" が衝突しうるが、
        # タプルキーなら (name, rarity) の組で区別されるため衝突しない
        server = [{"name": "A B", "rarity": "", "qty": 1}]
        client = [{"name": "A", "rarity": "B", "qty": 1}]
        merged = sync.merge_wishlist(server, client)
        assert len(merged) == 2
        names_rarities = {(m["name"], m["rarity"]) for m in merged}
        assert names_rarities == {("A B", ""), ("A", "B")}

    def test_rarity_differentiates_key(self):
        server = [{"name": "青眼の白龍", "rarity": "レア", "qty": 1}]
        client = [{"name": "青眼の白龍", "rarity": "ノーマル", "qty": 1}]
        merged = sync.merge_wishlist(server, client)
        assert len(merged) == 2


# ── validate_wishlist ──

class TestValidateWishlist:
    def test_over_limit_rejected_not_truncated(self):
        items = [{"name": f"カード{i}", "rarity": "", "qty": 1} for i in range(201)]
        cleaned, error = sync.validate_wishlist(items)
        assert cleaned is None
        assert error is not None

    def test_at_limit_accepted(self):
        items = [{"name": f"カード{i}", "rarity": "", "qty": 1} for i in range(200)]
        cleaned, error = sync.validate_wishlist(items)
        assert error is None
        assert len(cleaned) == 200

    def test_name_too_long_rejected(self):
        items = [{"name": "あ" * 51, "rarity": "", "qty": 1}]
        cleaned, error = sync.validate_wishlist(items)
        assert cleaned is None
        assert error is not None

    def test_name_at_limit_accepted(self):
        items = [{"name": "あ" * 50, "rarity": "", "qty": 1}]
        cleaned, error = sync.validate_wishlist(items)
        assert error is None
        assert cleaned[0]["name"] == "あ" * 50

    def test_qty_clamped_not_rejected(self):
        items = [{"name": "青眼の白龍", "rarity": "", "qty": 999}]
        cleaned, error = sync.validate_wishlist(items)
        assert error is None
        assert cleaned[0]["qty"] == 99


# ── push_wishlist（条件付き更新の競合→リトライ） ──
# supabase-py の .table().update({...}).eq().eq().execute() チェーンを
# 最小限たどるスタブ。execute() は「更新できた行」のリストを .data に返す
# （実物と同じく、条件不一致なら空リスト＝更新0行）。

class _FakeTable:
    def __init__(self, store):
        self._store = store

    def update(self, values):
        # eq() が呼ばれるまで sync_id は未確定。最初の eq 呼び出しで拾う簡易実装。
        return _FakeUpdateQueryBuilder(self._store, values)

    def select(self, *_a, **_k):
        return _FakeSelectQueryBuilder(self._store)

    def insert(self, payload):
        return _FakeInsertQueryBuilder(self._store, payload)


class _FakeInsertQueryBuilder:
    def __init__(self, store, payload):
        self._store = store
        self._payload = payload

    def execute(self):
        from types import SimpleNamespace
        import uuid
        sync_id = str(uuid.uuid4())
        row = {"sync_id": sync_id, "wishlist": self._payload.get("wishlist", []),
               "wishlist_rev": self._payload.get("wishlist_rev", 1)}
        self._store[sync_id] = row
        return SimpleNamespace(data=[dict(row)])


class _FakeUpdateQueryBuilder:
    """table().update(values) の直後、最初の eq(sync_id) が来るまで sync_id 不明のため中継する。"""

    def __init__(self, store, values):
        self._store = store
        self._values = values
        self._conds = {}

    def eq(self, col, val):
        self._conds[col] = val
        return self

    def execute(self):
        from types import SimpleNamespace
        sync_id = self._conds.get("sync_id")
        row = self._store.get(sync_id)
        if row is None:
            return SimpleNamespace(data=[])
        for col, val in self._conds.items():
            if row.get(col) != val:
                return SimpleNamespace(data=[])
        row.update(self._values)
        return SimpleNamespace(data=[dict(row)])


class _FakeSelectQueryBuilder:
    def __init__(self, store):
        self._store = store
        self._sync_id = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        if col == "sync_id":
            self._sync_id = val
        return self

    def execute(self):
        from types import SimpleNamespace
        row = self._store.get(self._sync_id)
        return SimpleNamespace(data=[dict(row)] if row is not None else [])


class _FakeClient:
    def __init__(self, store):
        self._store = store

    def table(self, _name):
        return _FakeTable(self._store)


class TestPushWishlistConflictRetry:
    def test_applied_when_rev_matches(self):
        store = {"s1": {"sync_id": "s1", "wishlist": [], "wishlist_rev": 3}}
        client = _FakeClient(store)
        result = sync.push_wishlist(client, "s1", 3, [{"name": "A", "rarity": "", "qty": 1}])
        assert result == {"ok": True, "status": "applied", "rev": 4}
        assert store["s1"]["wishlist_rev"] == 4

    def test_conflict_resolves_by_merge_and_retry(self):
        # base_rev=3 で送るが、実際のサーバー側リビジョンは既に4（他端末が先に更新済み）。
        # 通常経路が0行更新になり、競合経路（現在値を読み→マージ→再更新）で解決するはず。
        store = {"s1": {"sync_id": "s1",
                         "wishlist": [{"name": "既存カード", "rarity": "", "qty": 1}],
                         "wishlist_rev": 4}}
        client = _FakeClient(store)
        result = sync.push_wishlist(client, "s1", 3, [{"name": "新カード", "rarity": "", "qty": 1}])
        assert result["ok"] is True
        assert result["status"] == "merged"
        assert result["rev"] == 5
        names = {i["name"] for i in result["items"]}
        assert names == {"既存カード", "新カード"}
        assert store["s1"]["wishlist_rev"] == 5

    def test_not_found_when_row_missing(self):
        store = {}
        client = _FakeClient(store)
        result = sync.push_wishlist(client, "ghost", 0, [{"name": "A", "rarity": "", "qty": 1}])
        assert result == {"reason": "not_found"}


class TestPull:
    def test_unchanged_when_rev_matches(self):
        store = {"s1": {"sync_id": "s1", "wishlist": [{"name": "A", "rarity": "", "qty": 1}],
                         "wishlist_rev": 5}}
        client = _FakeClient(store)
        result = sync.pull(client, "s1", 5)
        assert result == {"ok": True, "wishlist": {"unchanged": True}}

    def test_returns_items_when_rev_differs(self):
        store = {"s1": {"sync_id": "s1", "wishlist": [{"name": "A", "rarity": "", "qty": 1}],
                         "wishlist_rev": 5}}
        client = _FakeClient(store)
        result = sync.pull(client, "s1", 4)
        assert result["ok"] is True
        assert result["wishlist"]["rev"] == 5
        assert result["wishlist"]["items"] == [{"name": "A", "rarity": "", "qty": 1}]

    def test_not_found_when_row_missing(self):
        store = {}
        client = _FakeClient(store)
        result = sync.pull(client, "ghost", 0)
        assert result == {"reason": "not_found"}


class TestInitAccount:
    def test_creates_row_with_rev_1(self):
        store = {}
        client = _FakeClient(store)
        result = sync.init_account(client, [{"name": "A", "rarity": "", "qty": 1}])
        assert result["ok"] is True
        assert result["wishlist_rev"] == 1
        assert result["sync_id"] in store
