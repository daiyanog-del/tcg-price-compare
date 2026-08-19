"""
tests/test_sync_merge.py — 端末間同期(sync.py)の和集合マージ・バリデーション・
条件付き更新（競合→リトライ）のテスト

設計文書: docs/design-sync-2026-08-09.md（第2版）§13「テスト」節に挙がる項目を最低限カバーする。
P1スコープ（購入候補=wishlistのみ）。ネットワーク不使用、Supabaseクライアントはモックする。
"""

import sys
from pathlib import Path

import pytest

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
        # payloadの全フィールド（wishlist/wishlist_rev/decks/decks_rev等）をそのまま保持する
        row = {"sync_id": sync_id, **self._payload}
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
    """2026-08-18追記: sync.pull() が is_linked()（sync_link_tokens への問い合わせ）を
    内部で呼ぶようになったため、.not_ / .is_ / .limit も最小限サポートする。
    このスタブは sync_accounts 形状の行しか持たないため、"used_at" 列は常に無く
    （= NULL 扱い）、.not_.is_("used_at","null") は常に「除外」に働く＝is_linked()は
    常に False を返す。このファイルのテストは linked フィールドの中身を検証しないため
    実害はない（linked の値そのものは tests/test_sync_link.py で検証する）。"""

    def __init__(self, store):
        self._store = store
        self._sync_id = None
        self._is_filters = []  # [(col, val, negate), ...]
        self._negate_next = False
        self._limit_n = None

    def select(self, *_a, **_k):
        return self

    @property
    def not_(self):
        self._negate_next = True
        return self

    def eq(self, col, val):
        if col == "sync_id":
            self._sync_id = val
        return self

    def is_(self, col, val):
        self._is_filters.append((col, val, self._negate_next))
        self._negate_next = False
        return self

    def limit(self, n):
        self._limit_n = n
        return self

    def execute(self):
        from types import SimpleNamespace
        row = self._store.get(self._sync_id)
        if row is None:
            return SimpleNamespace(data=[])
        for col, val, negate in self._is_filters:
            rv = row.get(col)
            ok = (rv is None) if val == "null" else (rv == val)
            if negate:
                ok = not ok
            if not ok:
                return SimpleNamespace(data=[])
        data = [dict(row)]
        if self._limit_n is not None:
            data = data[: self._limit_n]
        return SimpleNamespace(data=data)


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
                         "wishlist_rev": 5, "decks": [], "decks_rev": 2}}
        client = _FakeClient(store)
        result = sync.pull(client, "s1", 5, 2)
        # 2026-08-18: pull() は §7.3 の連携判定用に linked も返すようになった
        # （このスタブでは is_linked() が常に False。tests/test_sync_link.py 参照）
        assert result == {"ok": True, "linked": False,
                           "wishlist": {"unchanged": True}, "decks": {"unchanged": True}}

    def test_returns_items_when_rev_differs(self):
        store = {"s1": {"sync_id": "s1", "wishlist": [{"name": "A", "rarity": "", "qty": 1}],
                         "wishlist_rev": 5, "decks": [], "decks_rev": 1}}
        client = _FakeClient(store)
        result = sync.pull(client, "s1", 4, 1)
        assert result["ok"] is True
        assert result["wishlist"]["rev"] == 5
        assert result["wishlist"]["items"] == [{"name": "A", "rarity": "", "qty": 1}]
        assert result["decks"] == {"unchanged": True}

    def test_not_found_when_row_missing(self):
        store = {}
        client = _FakeClient(store)
        result = sync.pull(client, "ghost", 0, 0)
        assert result == {"reason": "not_found"}

    def test_wishlist_and_decks_revisions_are_independent(self):
        # 購入候補だけが変わっていてデッキは変わっていない場合、
        # wishlistは中身を返しdecksはunchangedになる（逆も同様）
        store = {"s1": {"sync_id": "s1",
                         "wishlist": [{"name": "A", "rarity": "", "qty": 1}], "wishlist_rev": 9,
                         "decks": [{"id": "d1", "name": "デッキA", "text": "", "main": [], "ex": [],
                                    "updated": 100}], "decks_rev": 3}}
        client = _FakeClient(store)
        result = sync.pull(client, "s1", 1, 3)  # wishlist_rev不一致・decks_rev一致
        assert result["wishlist"]["rev"] == 9
        assert result["decks"] == {"unchanged": True}


class TestInitAccount:
    def test_creates_row_with_rev_1(self):
        store = {}
        client = _FakeClient(store)
        result = sync.init_account(client, [{"name": "A", "rarity": "", "qty": 1}])
        assert result["ok"] is True
        assert result["wishlist_rev"] == 1
        assert result["decks_rev"] == 1
        assert result["sync_id"] in store

    def test_creates_row_with_decks(self):
        store = {}
        client = _FakeClient(store)
        decks = [{"id": "d1", "name": "デッキA", "text": "", "main": [], "ex": [], "updated": 1}]
        result = sync.init_account(client, [], decks)
        assert result["ok"] is True
        assert result["decks_rev"] == 1
        row = store[result["sync_id"]]
        assert row["decks"] == decks


# ── merge_decks（設計文書 §5.2） ──

class TestMergeDecks:
    def test_same_id_takes_newer_updated(self):
        server = [{"id": "d1", "name": "旧", "text": "旧テキスト", "main": [], "ex": [], "updated": 100}]
        client = [{"id": "d1", "name": "新", "text": "新テキスト", "main": [], "ex": [], "updated": 200}]
        merged = sync.merge_decks(server, client)
        assert len(merged) == 1
        assert merged[0]["name"] == "新"
        assert merged[0]["updated"] == 200

    def test_same_id_older_updated_loses(self):
        server = [{"id": "d1", "name": "サーバー版", "text": "", "main": [], "ex": [], "updated": 500}]
        client = [{"id": "d1", "name": "端末版", "text": "", "main": [], "ex": [], "updated": 300}]
        merged = sync.merge_decks(server, client)
        assert len(merged) == 1
        assert merged[0]["name"] == "サーバー版"

    def test_different_id_both_survive(self):
        server = [{"id": "d1", "name": "デッキA", "text": "", "main": [], "ex": [], "updated": 1}]
        client = [{"id": "d2", "name": "デッキB", "text": "", "main": [], "ex": [], "updated": 1}]
        merged = sync.merge_decks(server, client)
        ids = {d["id"] for d in merged}
        assert ids == {"d1", "d2"}

    def test_same_name_different_id_renamed_to_2(self):
        server = [{"id": "d1", "name": "青眼", "text": "", "main": [], "ex": [], "updated": 1}]
        client = [{"id": "d2", "name": "青眼", "text": "", "main": [], "ex": [], "updated": 2}]
        merged = sync.merge_decks(server, client)
        names = sorted(d["name"] for d in merged)
        assert names == ["青眼", "青眼 (2)"]
        # 後から来た(client側)がリネームされる。元のidは維持される
        renamed = next(d for d in merged if d["name"] == "青眼 (2)")
        assert renamed["id"] == "d2"

    def test_name_2_already_taken_becomes_3(self):
        # クライアント側が「青眼」と「青眼 (2)」という異なるidのデッキを2つ持っている状態で、
        # サーバーにも別idの「青眼」が既にある場合、衝突は (2)→(3) と繰り上がる
        server = [{"id": "d1", "name": "青眼", "text": "", "main": [], "ex": [], "updated": 1}]
        client = [
            {"id": "d2", "name": "青眼", "text": "", "main": [], "ex": [], "updated": 2},
            {"id": "d3", "name": "青眼 (2)", "text": "", "main": [], "ex": [], "updated": 3},
        ]
        merged = sync.merge_decks(server, client)
        names = sorted(d["name"] for d in merged)
        assert names == ["青眼", "青眼 (2)", "青眼 (3)"]
        # d2が(2)を得て、後から来たd3が(3)に繰り上がる
        by_id = {d["id"]: d["name"] for d in merged}
        assert by_id["d1"] == "青眼"
        assert by_id["d2"] == "青眼 (2)"
        assert by_id["d3"] == "青眼 (3)"

    def test_deck_without_main_ex_survives_merge(self):
        # 一人回しページ由来のデッキ（main/exを持たない）
        server = []
        client = [{"id": "d1", "name": "一人回しデッキ", "text": "3 灰流うらら", "updated": 1}]
        merged = sync.merge_decks(server, client)
        assert len(merged) == 1
        assert merged[0]["name"] == "一人回しデッキ"


# ── validate_decks（設計文書 §9・§11） ──

class TestValidateDecks:
    def test_deck_without_main_ex_not_rejected(self):
        # 一人回しページ由来のデッキはmain/exを持たない（必須にしない。§11）
        decks = [{"id": "d1", "name": "一人回しデッキ", "text": "3 灰流うらら", "updated": 1}]
        cleaned, error = sync.validate_decks(decks)
        assert error is None
        assert cleaned[0]["main"] == []
        assert cleaned[0]["ex"] == []

    def test_over_deck_count_limit_rejected_not_truncated(self):
        decks = [{"id": f"d{i}", "name": f"デッキ{i}", "text": "", "main": [], "ex": [], "updated": 1}
                 for i in range(51)]
        cleaned, error = sync.validate_decks(decks)
        assert cleaned is None
        assert error is not None

    def test_at_deck_count_limit_accepted(self):
        decks = [{"id": f"d{i}", "name": f"デッキ{i}", "text": "", "main": [], "ex": [], "updated": 1}
                 for i in range(50)]
        cleaned, error = sync.validate_decks(decks)
        assert error is None
        assert len(cleaned) == 50

    def test_text_over_limit_rejected_not_truncated(self):
        decks = [{"id": "d1", "name": "デッキ", "text": "あ" * 6001, "main": [], "ex": [], "updated": 1}]
        cleaned, error = sync.validate_decks(decks)
        assert cleaned is None
        assert error is not None

    def test_text_at_limit_accepted(self):
        decks = [{"id": "d1", "name": "デッキ", "text": "あ" * 6000, "main": [], "ex": [], "updated": 1}]
        cleaned, error = sync.validate_decks(decks)
        assert error is None
        assert len(cleaned[0]["text"]) == 6000

    def test_main_over_80_cards_rejected_not_truncated(self):
        main = [{"name": f"カード{i}", "qty": 1} for i in range(81)]
        decks = [{"id": "d1", "name": "デッキ", "text": "", "main": main, "ex": [], "updated": 1}]
        cleaned, error = sync.validate_decks(decks)
        assert cleaned is None
        assert error is not None

    def test_main_at_80_cards_accepted(self):
        main = [{"name": f"カード{i}", "qty": 1} for i in range(80)]
        decks = [{"id": "d1", "name": "デッキ", "text": "", "main": main, "ex": [], "updated": 1}]
        cleaned, error = sync.validate_decks(decks)
        assert error is None
        assert len(cleaned[0]["main"]) == 80

    def test_deck_without_id_excluded_not_erroring(self):
        decks = [
            {"name": "id無し", "text": "", "main": [], "ex": [], "updated": 1},
            {"id": "d1", "name": "id有り", "text": "", "main": [], "ex": [], "updated": 1},
        ]
        cleaned, error = sync.validate_decks(decks)
        assert error is None
        assert len(cleaned) == 1
        assert cleaned[0]["name"] == "id有り"


# ── push_decks（条件付き更新の競合→リトライ。push_wishlistと同じ流儀） ──

class TestPushDecksConflictRetry:
    def test_applied_when_rev_matches(self):
        store = {"s1": {"sync_id": "s1", "decks": [], "decks_rev": 3}}
        client = _FakeClient(store)
        result = sync.push_decks(client, "s1", 3, [{"id": "d1", "name": "A", "text": "",
                                                       "main": [], "ex": [], "updated": 1}])
        assert result == {"ok": True, "status": "applied", "rev": 4}
        assert store["s1"]["decks_rev"] == 4

    def test_conflict_resolves_by_merge_and_retry(self):
        store = {"s1": {"sync_id": "s1",
                         "decks": [{"id": "d1", "name": "既存デッキ", "text": "", "main": [], "ex": [],
                                    "updated": 1}],
                         "decks_rev": 4}}
        client = _FakeClient(store)
        result = sync.push_decks(client, "s1", 3,
                                  [{"id": "d2", "name": "新デッキ", "text": "", "main": [], "ex": [],
                                    "updated": 2}])
        assert result["ok"] is True
        assert result["status"] == "merged"
        assert result["rev"] == 5
        names = {d["name"] for d in result["items"]}
        assert names == {"既存デッキ", "新デッキ"}
        assert store["s1"]["decks_rev"] == 5

    def test_not_found_when_row_missing(self):
        store = {}
        client = _FakeClient(store)
        result = sync.push_decks(client, "ghost", 0, [{"id": "d1", "name": "A", "text": "",
                                                          "main": [], "ex": [], "updated": 1}])
        assert result == {"reason": "not_found"}

    def test_pushing_decks_does_not_touch_wishlist_revision(self):
        # 購入候補とデッキはリビジョンを分けているため、デッキのpushで
        # wishlist_revが変化しないことを確認する（設計文書 §3.1）
        store = {"s1": {"sync_id": "s1",
                         "wishlist": [{"name": "A", "rarity": "", "qty": 1}], "wishlist_rev": 7,
                         "decks": [], "decks_rev": 1}}
        client = _FakeClient(store)
        sync.push_decks(client, "s1", 1, [{"id": "d1", "name": "デッキ", "text": "",
                                             "main": [], "ex": [], "updated": 1}])
        assert store["s1"]["wishlist_rev"] == 7
        assert store["s1"]["wishlist"] == [{"name": "A", "rarity": "", "qty": 1}]

    def test_pushing_wishlist_does_not_touch_decks_revision(self):
        store = {"s1": {"sync_id": "s1",
                         "wishlist": [], "wishlist_rev": 1,
                         "decks": [{"id": "d1", "name": "デッキ", "text": "", "main": [], "ex": [],
                                    "updated": 1}], "decks_rev": 9}}
        client = _FakeClient(store)
        sync.push_wishlist(client, "s1", 1, [{"name": "A", "rarity": "", "qty": 1}])
        assert store["s1"]["decks_rev"] == 9
        assert store["s1"]["decks"] == [{"id": "d1", "name": "デッキ", "text": "", "main": [], "ex": [],
                                           "updated": 1}]


# ── /api/sync/init の穴ふさぎ（2026-08-13）。空・壊れた本文で行を作らないこと ──
# app.py の Flask ルートを test_client() 経由で叩く。Supabaseクライアントは
# insert() 呼び出し回数を記録するだけの最小トラッキングスタブに差し替える
# （tests/test_wish_prices.py の monkeypatch パターンに倣う）。

class _TrackingInsertBuilder:
    def __init__(self, row):
        self._row = row

    def execute(self):
        from types import SimpleNamespace
        return SimpleNamespace(data=[self._row])


class _TrackingClient:
    """/api/sync/init が insert を呼んだかどうか（＝行を作ったかどうか）だけを検証するスタブ。"""

    def __init__(self):
        self.insert_calls = 0

    def table(self, _name):
        return self

    def insert(self, payload):
        import uuid
        self.insert_calls += 1
        row = dict(payload)
        row["sync_id"] = str(uuid.uuid4())
        return _TrackingInsertBuilder(row)


@pytest.fixture
def sync_init_client(monkeypatch):
    import app as app_module

    tracking = _TrackingClient()
    monkeypatch.setattr(app_module, "_supabase_client", tracking)
    app_module._last_sync.clear()  # レートリミット回避（同一IPでの連続テスト実行のため）
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), tracking


class TestSyncInitEmptyRequestGuard:
    def test_both_empty_rejected_no_row_created(self, sync_init_client):
        client, tracking = sync_init_client
        res = client.post("/api/sync/init", json={"wishlist": [], "decks": []})
        assert res.status_code == 400
        assert tracking.insert_calls == 0

    def test_no_body_at_all_rejected_no_row_created(self, sync_init_client):
        # 実際に起きたケース: 本文なしでPOSTされ、get_json(silent=True) or {} が
        # 空dict扱いになって行だけが作られていた（穴ふさぎ対象）
        client, tracking = sync_init_client
        res = client.post("/api/sync/init")
        assert res.status_code == 400
        assert tracking.insert_calls == 0

    def test_malformed_json_body_rejected_no_row_created(self, sync_init_client):
        client, tracking = sync_init_client
        res = client.post("/api/sync/init", data="{not valid json",
                           content_type="application/json")
        assert res.status_code == 400
        assert tracking.insert_calls == 0

    def test_wrong_content_type_rejected_no_row_created(self, sync_init_client):
        client, tracking = sync_init_client
        res = client.post("/api/sync/init", data="wishlist=foo",
                           content_type="text/plain")
        assert res.status_code == 400
        assert tracking.insert_calls == 0

    def test_wishlist_only_creates_row(self, sync_init_client):
        # 既存クライアントの挙動（購入候補が1件以上あるときだけinitを呼ぶ）は壊れない
        client, tracking = sync_init_client
        res = client.post("/api/sync/init",
                           json={"wishlist": [{"name": "A", "rarity": "", "qty": 1}]})
        assert res.status_code == 200
        assert tracking.insert_calls == 1
        assert res.get_json()["ok"] is True

    def test_decks_only_creates_row(self, sync_init_client):
        client, tracking = sync_init_client
        res = client.post("/api/sync/init",
                           json={"decks": [{"id": "d1", "name": "A", "text": "",
                                             "main": [], "ex": [], "updated": 1}]})
        assert res.status_code == 200
        assert tracking.insert_calls == 1
