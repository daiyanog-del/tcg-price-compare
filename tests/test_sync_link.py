"""
tests/test_sync_link.py — 端末間同期(sync.py)のワンタイムリンク（P3）のテスト

設計文書: docs/design-sync-2026-08-09.md（第2版）§6.4〜§6.7・§13。
ネットワーク不使用、Supabaseクライアントはモックする（tests/test_sync_merge.py と同じ流儀）。

検証すること（司令塔の完了条件(G)に対応）:
  - トークンの二重使用が防げること
  - 期限切れ・使用済み・不明の区別（preview / redeem 双方）
  - redeem が既存のマージ規則（merge_wishlist / merge_decks）で和集合になること
  - old_sync_id の行が削除されない（redeem_link は old_sync_id を一切受け取らない設計に
    したことで保証する。app.py 側もそれを転送しないことをエンドポイントテストで確認する）
  - unlink が元の行を残したまま新しい sync_id を作ること
"""

import sys
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sync


# ── 汎用フェイクSupabaseクライアント（sync_accounts / sync_link_tokens の2テーブルを
#    メモリ上の dict-of-list で模倣する。eq / is_ / gt をチェーンできる） ──

class _FakeQuery:
    def __init__(self, rows, mode, table_name, payload=None):
        self._rows = rows  # テーブル本体（list of dict。参照を直接書き換える）
        self._mode = mode  # 'select' | 'update' | 'insert'
        self._table_name = table_name
        self._payload = payload
        self._filters = []

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self._filters.append(("is", col, val))
        return self

    def gt(self, col, val):
        self._filters.append(("gt", col, val))
        return self

    def _match(self, row):
        for op, col, val in self._filters:
            rv = row.get(col)
            if op == "eq":
                if rv != val:
                    return False
            elif op == "is":
                if val == "null":
                    if rv is not None:
                        return False
                elif rv != val:
                    return False
            elif op == "gt":
                if rv is None or not (rv > val):
                    return False
        return True

    def execute(self):
        if self._mode == "insert":
            row = dict(self._payload)
            if self._table_name == "sync_accounts" and "sync_id" not in row:
                row["sync_id"] = str(_uuid.uuid4())
            self._rows.append(row)
            return SimpleNamespace(data=[dict(row)])

        matched = [r for r in self._rows if self._match(r)]
        if self._mode == "select":
            return SimpleNamespace(data=[dict(r) for r in matched])
        if self._mode == "update":
            for r in matched:
                r.update(self._payload)
            return SimpleNamespace(data=[dict(r) for r in matched])
        raise AssertionError(f"unknown mode {self._mode}")


class _FakeTable:
    def __init__(self, rows, table_name):
        self._rows = rows
        self._table_name = table_name

    def select(self, *_a, **_k):
        return _FakeQuery(self._rows, "select", self._table_name)

    def update(self, payload):
        return _FakeQuery(self._rows, "update", self._table_name, payload)

    def insert(self, payload):
        return _FakeQuery(self._rows, "insert", self._table_name, payload)


class _FakeClient:
    """sync_accounts / sync_link_tokens の2テーブルを持つフェイクSupabaseクライアント。"""

    def __init__(self):
        self.accounts: list[dict] = []
        self.tokens: list[dict] = []

    def table(self, name):
        if name == "sync_accounts":
            return _FakeTable(self.accounts, "sync_accounts")
        if name == "sync_link_tokens":
            return _FakeTable(self.tokens, "sync_link_tokens")
        raise AssertionError(f"unexpected table {name}")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _make_account(client, sync_id, wishlist=None, wishlist_rev=1, decks=None, decks_rev=1):
    client.accounts.append({
        "sync_id": sync_id,
        "wishlist": wishlist or [],
        "wishlist_rev": wishlist_rev,
        "decks": decks or [],
        "decks_rev": decks_rev,
    })


def _make_token(client, token, sync_id, *, expires_in_sec=600, used=False):
    client.tokens.append({
        "token": token,
        "sync_id": sync_id,
        "expires_at": _iso(datetime.now(timezone.utc) + timedelta(seconds=expires_in_sec)),
        "used_at": _iso(datetime.now(timezone.utc)) if used else None,
    })


# ── create_link_token ──

class TestCreateLinkToken:
    def test_creates_token_for_existing_sync_id(self):
        client = _FakeClient()
        _make_account(client, "s1")
        result = sync.create_link_token(client, "s1")
        assert result["ok"] is True
        assert len(result["token"]) > 20
        assert result["expires_in"] == sync.LINK_TOKEN_TTL_SEC
        assert len(client.tokens) == 1
        assert client.tokens[0]["sync_id"] == "s1"
        # used_at はDB側のデフォルト(null)に委ねる。insert時のpayloadには含めない
        assert client.tokens[0].get("used_at") is None

    def test_not_found_for_unknown_sync_id(self):
        client = _FakeClient()
        result = sync.create_link_token(client, "ghost")
        assert result == {"reason": "not_found"}
        assert client.tokens == []  # 存在しないsync_idに対してトークン行を作らない


# ── preview_link（副作用なし） ──

class TestPreviewLink:
    def test_valid_token_returns_counts_and_remaining_seconds(self):
        client = _FakeClient()
        _make_account(client, "s1",
                       wishlist=[{"name": "A", "rarity": "", "qty": 1},
                                 {"name": "B", "rarity": "", "qty": 1}],
                       decks=[{"id": "d1", "name": "デッキ", "text": "", "main": [], "ex": [], "updated": 1}])
        _make_token(client, "tok1", "s1", expires_in_sec=480)
        result = sync.preview_link(client, "tok1")
        assert result["ok"] is True
        assert result["remote"] == {"wishlist_count": 2, "decks_count": 1}
        assert 0 < result["expires_in"] <= 480

    def test_not_found_reason(self):
        client = _FakeClient()
        result = sync.preview_link(client, "ghost-token")
        assert result == {"ok": False, "reason": "not_found"}

    def test_used_reason(self):
        client = _FakeClient()
        _make_account(client, "s1")
        _make_token(client, "tok1", "s1", used=True)
        result = sync.preview_link(client, "tok1")
        assert result == {"ok": False, "reason": "used"}

    def test_expired_reason(self):
        client = _FakeClient()
        _make_account(client, "s1")
        _make_token(client, "tok1", "s1", expires_in_sec=-10)
        result = sync.preview_link(client, "tok1")
        assert result == {"ok": False, "reason": "expired"}

    def test_preview_has_no_side_effects(self):
        # 下見を2回呼んでも used_at が変化しない（副作用なし。設計文書 §6.5）
        client = _FakeClient()
        _make_account(client, "s1")
        _make_token(client, "tok1", "s1")
        sync.preview_link(client, "tok1")
        sync.preview_link(client, "tok1")
        assert client.tokens[0]["used_at"] is None
        assert len(client.tokens) == 1


# ── redeem_link（トークン消費・和集合マージ） ──

class TestRedeemLink:
    def test_merges_using_union_rules_and_advances_both_revs(self):
        client = _FakeClient()
        _make_account(client, "s1",
                       wishlist=[{"name": "A", "rarity": "", "qty": 1}], wishlist_rev=3,
                       decks=[{"id": "d1", "name": "サーバーデッキ", "text": "", "main": [], "ex": [], "updated": 1}],
                       decks_rev=2)
        _make_token(client, "tok1", "s1")

        result = sync.redeem_link(
            client, "tok1",
            wishlist=[{"name": "B", "rarity": "", "qty": 1}],
            decks=[{"id": "d2", "name": "端末Bのデッキ", "text": "", "main": [], "ex": [], "updated": 2}],
        )

        assert result["ok"] is True
        assert result["sync_id"] == "s1"
        assert result["wishlist"]["rev"] == 4
        names = {i["name"] for i in result["wishlist"]["items"]}
        assert names == {"A", "B"}
        assert result["decks"]["rev"] == 3
        deck_ids = {d["id"] for d in result["decks"]["items"]}
        assert deck_ids == {"d1", "d2"}

        # ストア側にも反映されている
        row = client.accounts[0]
        assert row["wishlist_rev"] == 4
        assert row["decks_rev"] == 3

    def test_token_consumed_only_once(self):
        client = _FakeClient()
        _make_account(client, "s1")
        _make_token(client, "tok1", "s1")

        first = sync.redeem_link(client, "tok1", wishlist=[], decks=[])
        assert first["ok"] is True

        second = sync.redeem_link(client, "tok1", wishlist=[], decks=[])
        assert second == {"ok": False, "reason": "used"}

    def test_two_simultaneous_redeems_only_one_succeeds(self):
        # 「2端末が同時に踏んでも二重処理しない」の直接検証。
        # 条件付き更新は1行しか更新できないため、同じトークンで連続してredeemした場合
        # 成功するのは1回だけになる（実際の同時アクセスはDBのCASで解決されるが、
        # ここでは逐次呼び出しで「2回目は必ず失敗する」ことを確認する）
        client = _FakeClient()
        _make_account(client, "s1")
        _make_token(client, "tok1", "s1")
        results = [sync.redeem_link(client, "tok1", wishlist=[], decks=[]) for _ in range(2)]
        oks = [r for r in results if r.get("ok")]
        assert len(oks) == 1

    def test_expired_token_cannot_be_redeemed(self):
        client = _FakeClient()
        _make_account(client, "s1", wishlist=[{"name": "既存", "rarity": "", "qty": 1}], wishlist_rev=1)
        _make_token(client, "tok1", "s1", expires_in_sec=-10)

        result = sync.redeem_link(client, "tok1", wishlist=[{"name": "新規", "rarity": "", "qty": 1}], decks=[])
        assert result == {"ok": False, "reason": "expired"}
        # マージが実行されず、サーバー側のデータは変化しない
        assert client.accounts[0]["wishlist_rev"] == 1
        assert client.accounts[0]["wishlist"] == [{"name": "既存", "rarity": "", "qty": 1}]

    def test_unknown_token_not_found(self):
        client = _FakeClient()
        result = sync.redeem_link(client, "ghost", wishlist=[], decks=[])
        assert result == {"ok": False, "reason": "not_found"}

    def test_redeem_link_signature_has_no_old_sync_id_param(self):
        # 設計文書 §6.6「old_sync_id の行は削除しない」をコード構造で保証する。
        # sync.redeem_link は old_sync_id を一切受け取らない（呼びようがない＝触りようがない）
        import inspect
        params = list(inspect.signature(sync.redeem_link).parameters)
        assert "old_sync_id" not in params
        assert params == ["client", "token", "wishlist", "decks"]


# ── unlink ──

class TestUnlink:
    def test_creates_new_row_preserving_data_original_row_intact(self):
        client = _FakeClient()
        _make_account(client, "s1",
                       wishlist=[{"name": "A", "rarity": "", "qty": 2}], wishlist_rev=5,
                       decks=[{"id": "d1", "name": "デッキ", "text": "", "main": [], "ex": [], "updated": 9}],
                       decks_rev=3)

        result = sync.unlink(client, "s1")

        assert result["ok"] is True
        assert result["sync_id"] != "s1"
        assert result["wishlist_rev"] == 1
        assert result["decks_rev"] == 1

        # 元の行(s1)はそのまま残っている（他端末は影響を受けない）
        old_row = next(r for r in client.accounts if r["sync_id"] == "s1")
        assert old_row["wishlist_rev"] == 5
        assert old_row["wishlist"] == [{"name": "A", "rarity": "", "qty": 2}]
        assert old_row["decks_rev"] == 3

        # 新しい行にデータが複製されている
        new_row = next(r for r in client.accounts if r["sync_id"] == result["sync_id"])
        assert new_row["wishlist"] == [{"name": "A", "rarity": "", "qty": 2}]
        assert new_row["decks"] == [{"id": "d1", "name": "デッキ", "text": "", "main": [], "ex": [], "updated": 9}]

        assert len(client.accounts) == 2  # 複製先が増えただけで元は消えていない

    def test_not_found_for_unknown_sync_id(self):
        client = _FakeClient()
        result = sync.unlink(client, "ghost")
        assert result == {"reason": "not_found"}


# ── /api/sync/link/* エンドポイントの配線（app.py。Flask test_client経由） ──

@pytest.fixture
def link_app_client(monkeypatch):
    import app as app_module

    client = _FakeClient()
    monkeypatch.setattr(app_module, "_supabase_client", client)
    app_module._last_sync.clear()
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client(), client


class TestLinkCreateEndpoint:
    def test_missing_sync_id_rejected(self, link_app_client):
        flask_client, _ = link_app_client
        res = flask_client.post("/api/sync/link/create", json={})
        assert res.status_code == 400

    def test_creates_token_and_returns_url(self, link_app_client):
        flask_client, store = link_app_client
        _make_account(store, "s1")
        res = flask_client.post("/api/sync/link/create", json={"sync_id": "s1"})
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is True
        assert body["token"]
        assert body["url"].endswith("/sync?t=" + body["token"])

    def test_unknown_sync_id_returns_not_found(self, link_app_client):
        flask_client, _ = link_app_client
        res = flask_client.post("/api/sync/link/create", json={"sync_id": "ghost"})
        body = res.get_json()
        assert body["ok"] is False
        assert body["reason"] == "not_found"


class TestLinkPreviewEndpoint:
    def test_missing_token_rejected(self, link_app_client):
        flask_client, _ = link_app_client
        res = flask_client.post("/api/sync/link/preview", json={})
        assert res.status_code == 400

    def test_valid_token_previewed(self, link_app_client):
        flask_client, store = link_app_client
        _make_account(store, "s1", wishlist=[{"name": "A", "rarity": "", "qty": 1}])
        _make_token(store, "tok1", "s1")
        res = flask_client.post("/api/sync/link/preview", json={"token": "tok1"})
        body = res.get_json()
        assert body["ok"] is True
        assert body["remote"]["wishlist_count"] == 1


class TestLinkRedeemEndpoint:
    def test_missing_token_rejected(self, link_app_client):
        flask_client, _ = link_app_client
        res = flask_client.post("/api/sync/link/redeem", json={})
        assert res.status_code == 400

    def test_invalid_wishlist_rejected(self, link_app_client):
        flask_client, store = link_app_client
        _make_account(store, "s1")
        _make_token(store, "tok1", "s1")
        res = flask_client.post("/api/sync/link/redeem", json={
            "token": "tok1", "wishlist": [{"name": "あ" * 51, "rarity": "", "qty": 1}], "decks": [],
        })
        assert res.status_code == 400

    def test_redeem_success_merges_and_ignores_old_sync_id_row(self, link_app_client):
        # old_sync_id は別の既存アカウント行を指しているが、app.py はそれを sync.py に渡さない
        # ため、redeem 後もその行は一切変更されない（設計文書 §6.6「old_sync_id の行は削除しない」）
        flask_client, store = link_app_client
        _make_account(store, "s1", wishlist=[{"name": "A", "rarity": "", "qty": 1}], wishlist_rev=1)
        _make_account(store, "old-device", wishlist=[{"name": "旧端末専用", "rarity": "", "qty": 1}], wishlist_rev=7)
        _make_token(store, "tok1", "s1")

        res = flask_client.post("/api/sync/link/redeem", json={
            "token": "tok1",
            "wishlist": [{"name": "B", "rarity": "", "qty": 1}],
            "decks": [],
            "old_sync_id": "old-device",
        })
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is True
        names = {i["name"] for i in body["wishlist"]["items"]}
        assert names == {"A", "B"}

        # old_sync_id で指定した行は一切触られていない
        old_row = next(r for r in store.accounts if r["sync_id"] == "old-device")
        assert old_row["wishlist_rev"] == 7
        assert old_row["wishlist"] == [{"name": "旧端末専用", "rarity": "", "qty": 1}]


class TestUnlinkEndpoint:
    def test_missing_sync_id_rejected(self, link_app_client):
        flask_client, _ = link_app_client
        res = flask_client.post("/api/sync/unlink", json={})
        assert res.status_code == 400

    def test_unlink_creates_new_row_keeps_old(self, link_app_client):
        flask_client, store = link_app_client
        _make_account(store, "s1", wishlist=[{"name": "A", "rarity": "", "qty": 1}], wishlist_rev=4)
        res = flask_client.post("/api/sync/unlink", json={"sync_id": "s1"})
        assert res.status_code == 200
        body = res.get_json()
        assert body["ok"] is True
        assert body["sync_id"] != "s1"
        assert body["wishlist_rev"] == 1
        old_row = next(r for r in store.accounts if r["sync_id"] == "s1")
        assert old_row["wishlist_rev"] == 4  # 元の行は変わらない

    def test_unknown_sync_id_returns_not_found(self, link_app_client):
        flask_client, _ = link_app_client
        res = flask_client.post("/api/sync/unlink", json={"sync_id": "ghost"})
        body = res.get_json()
        assert body["ok"] is False
        assert body["reason"] == "not_found"
