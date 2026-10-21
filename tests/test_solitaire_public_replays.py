"""
tests/test_solitaire_public_replays.py — みんなのリプレイ（公開リプレイ。公開ライブラリ化の段階2）の検証

検証方針:
  test_solitaire_replay.py と同じ手法（env 経由でモジュール reload）でアプリを構築し、
  Supabase クライアントを「solitaire_replays を模したメモリ上の表」に差し替える。
  eq / contains / lt / order / limit / update / delete を実際に評価するので、
  一覧に hidden が出ないこと・card 絞り込み・before での続き読みは、フィルタの呼び出しではなく結果で確かめる。

  - 公開の受付: タイトル必須・禁止語・card_names / steps の作り方・削除鍵（ハッシュだけ保存）
  - 公開でない保存は新しい列を含めない（従来どおり）
  - 一覧: hidden と非公開が出ない・card 完全一致・before・limit 上限・機能停止スイッチ・レート制限
  - 取り下げ: 鍵が合う／合わない・行は消えない
  - 掃除（collect_prices.cleanup_old_data）が公開分を消さない
  - 管理画面 API の認証・非表示の切り替え・削除
"""

import hashlib
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ──────────────────────────────────────────────
# solitaire_replays を模したメモリ上の表
# ──────────────────────────────────────────────

class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self._rows = rows
        self._op = "select"
        self._payload = None
        self._filters = []
        self._order = None
        self._limit = None

    # ビルダ
    def insert(self, payload):
        self._op, self._payload = "insert", payload
        return self

    def select(self, cols="*"):
        return self

    def update(self, payload):
        self._op, self._payload = "update", payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, val):
        self._filters.append(lambda r: r.get(col) == val)
        return self

    def lt(self, col, val):
        self._filters.append(lambda r: r.get(col) is not None and r.get(col) < val)
        return self

    def contains(self, col, vals):
        # 本番は配列リテラルの文字列 '{"…"}' で渡す（引用符・バックスラッシュのエスケープも解く）
        if isinstance(vals, str):
            assert vals.startswith('{"') and vals.endswith('"}'), vals
            body, out, i = vals[2:-2], [], 0
            while i < len(body):
                if body[i] == chr(92):
                    i += 1
                out.append(body[i])
                i += 1
            vals = ["".join(out)]
        self._filters.append(lambda r: all(v in (r.get(col) or []) for v in vals))
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._op == "insert":
            self._rows.append(dict(self._payload))
            return _Resp([dict(self._payload)])
        hit = [r for r in self._rows if all(f(r) for f in self._filters)]
        if self._op == "update":
            for r in hit:
                r.update(self._payload)
            return _Resp([dict(r) for r in hit])
        if self._op == "delete":
            for r in hit:
                self._rows.remove(r)
            return _Resp([dict(r) for r in hit])
        if self._order:
            col, desc = self._order
            hit.sort(key=lambda r: r.get(col) or "", reverse=desc)
        if self._limit is not None:
            hit = hit[: self._limit]
        return _Resp([dict(r) for r in hit])


class FakeSupabase:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []

    def table(self, name):
        assert name == "solitaire_replays"
        return _Query(self.rows)


def _load_app(monkeypatch, flag="1", rows=None):
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", flag)
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    sb = FakeSupabase(rows)
    app_module._supabase_client = sb
    return app_module, app_module.app.test_client(), sb


def _row(id_, published_at, *, title="t", cards=("A",), hidden=False, public=True, token_hash=None):
    return {
        "id": id_, "title": title, "is_public": public, "hidden": hidden,
        "published_at": published_at if public else None,
        "card_names": list(cards), "steps": 3, "delete_token_hash": token_hash,
        "created_at": "2026-10-01T00:00:00+00:00",
    }


def _body(**kw):
    body = {"logs": [{"type": "x"}], "images": {"a": "https://e/a.png"}, "names": {"a": "灰流うらら"}, "title": "テスト"}
    body.update(kw)
    return body


# ──────────────────────────────────────────────
# 公開の受付
# ──────────────────────────────────────────────

def test_public_requires_title(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    for title in ("", "   ", "　"):
        resp = client.post("/api/solitaire/replay", json=_body(public=True, title=title))
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "公開するにはタイトルを入れてください"
    assert sb.rows == []


def test_public_title_too_long_is_rejected(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    ok = client.post("/api/solitaire/replay", json=_body(public=True, title="あ" * 40))
    assert ok.status_code == 200
    ng = client.post("/api/solitaire/replay", json=_body(public=True, title="あ" * 41))
    assert ng.status_code == 400
    assert len(sb.rows) == 1


def test_public_rejects_ng_word_even_when_obfuscated(monkeypatch):
    """全角・大文字・空白で崩した禁止語も、正規化して断る"""
    _, client, sb = _load_app(monkeypatch)
    for title in ("Ｆｕｃｋ", "FUCK　you", "死 ね"):
        resp = client.post("/api/solitaire/replay", json=_body(public=True, title=title))
        assert resp.status_code == 400, title
        assert resp.get_json()["error"] == "タイトルに使えない言葉が含まれています"
    assert sb.rows == []


def test_ng_word_is_not_checked_for_non_public(monkeypatch):
    """公開でない共有は従来どおり（タイトルは検査しない）"""
    _, client, sb = _load_app(monkeypatch)
    resp = client.post("/api/solitaire/replay", json=_body(title="fuck"))
    assert resp.status_code == 200
    assert "deleteToken" not in resp.get_json()


def test_public_saves_columns_and_returns_token_with_hash_only(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    resp = client.post("/api/solitaire/replay", json=_body(public=True, title="  先攻展開  "))
    assert resp.status_code == 200
    data = resp.get_json()
    token = data["deleteToken"]
    assert len(token) >= 32
    row = sb.rows[0]
    assert row["id"] == data["id"]
    assert row["title"] == "先攻展開"          # 前後の空白は除く
    assert row["is_public"] is True
    assert row["published_at"] and row["published_at"] == row["created_at"]
    assert row["delete_token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(row)                # 鍵そのものは保存しない
    assert "hidden" not in row                  # 既定値（false）に任せる


def test_public_card_names_exclude_tokens_dedupe_and_truncate(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    images = {
        "a": "https://e/a.png", "b": "https://e/b.png", "c": "https://e/c.png",
        "tok": "data:image/png;base64,AAAA", "d": "https://e/d.png",
    }
    names = {
        "a": "灰流うらら", "b": "灰流うらら",       # 重複
        "tok": "トークン",                          # 画像が data: → 除く
        "c": "あ" * 80,                            # 60 文字で切る
        "d": "  ",                                  # 空は除く
        "n": 123,                                   # 文字列でない値は除く
    }
    resp = client.post("/api/solitaire/replay", json=_body(public=True, images=images, names=names))
    assert resp.status_code == 200
    assert sb.rows[0]["card_names"] == ["灰流うらら", "あ" * 60]


def test_public_card_names_capped_at_80(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    names = {str(i): f"カード{i}" for i in range(100)}
    images = {str(i): f"https://e/{i}.png" for i in range(100)}
    resp = client.post("/api/solitaire/replay", json=_body(public=True, images=images, names=names))
    assert resp.status_code == 200
    assert len(sb.rows[0]["card_names"]) == 80


def test_public_steps_excludes_setup_logs(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    logs = [{"type": "s", "setup": True}, {"type": "s", "setup": True},
            {"type": "m"}, {"type": "m"}, {"type": "m", "setup": False}]
    resp = client.post("/api/solitaire/replay", json=_body(public=True, logs=logs))
    assert resp.status_code == 200
    assert sb.rows[0]["steps"] == 3


def test_non_public_save_leaves_new_columns_untouched(monkeypatch):
    """公開でない保存は、新しい列を payload に含めない（既定値のまま）。public が真偽値 true 以外でも同じ"""
    _, client, sb = _load_app(monkeypatch)
    for extra in ({}, {"public": False}, {"public": "true"}, {"public": 1}):
        resp = client.post("/api/solitaire/replay", json=_body(**extra))
        assert resp.status_code == 200
        assert "deleteToken" not in resp.get_json()
    for row in sb.rows:
        for col in ("is_public", "published_at", "card_names", "steps", "delete_token_hash", "hidden"):
            assert col not in row


# ──────────────────────────────────────────────
# 公開一覧
# ──────────────────────────────────────────────

def _sample_rows():
    return [
        _row("r1", "2026-10-01T00:00:01+00:00", cards=("A", "B")),
        _row("r2", "2026-10-02T00:00:00+00:00", cards=("B",)),
        _row("r3", "2026-10-03T00:00:00+00:00", cards=("A",), hidden=True),   # 非表示
        _row("r4", "2026-10-04T00:00:00+00:00", cards=("A",), public=False),  # 公開でない
        _row("r5", "2026-10-05T00:00:00+00:00", cards=("C",)),
    ]


def test_list_excludes_hidden_and_non_public_and_orders_newest_first(monkeypatch):
    _, client, _ = _load_app(monkeypatch, rows=_sample_rows())
    resp = client.get("/api/solitaire/public")
    assert resp.status_code == 200
    ids = [i["id"] for i in resp.get_json()["items"]]
    assert ids == ["r5", "r2", "r1"]
    assert "r3" not in ids and "r4" not in ids


def test_list_item_shape_and_card_names_capped_at_six(monkeypatch):
    rows = [_row("r1", "2026-10-01T00:00:00+00:00", cards=tuple(f"c{i}" for i in range(9)))]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    item = client.get("/api/solitaire/public").get_json()["items"][0]
    assert set(item) == {"id", "title", "card_names", "card_count", "steps", "published_at"}
    assert item["card_names"] == [f"c{i}" for i in range(6)]
    assert item["card_count"] == 9
    assert item["steps"] == 3
    # 削除鍵や hidden は返さない
    assert "delete_token_hash" not in item and "hidden" not in item


def test_list_card_filter_is_exact_match(monkeypatch):
    rows = _sample_rows() + [_row("r6", "2026-10-06T00:00:00+00:00", cards=("AB",))]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    ids = [i["id"] for i in client.get("/api/solitaire/public?card=A").get_json()["items"]]
    assert ids == ["r1"]          # r3(非表示)・r4(非公開)・r6("AB"は部分一致なので対象外)を除く
    ids_b = [i["id"] for i in client.get("/api/solitaire/public?card=B").get_json()["items"]]
    assert ids_b == ["r2", "r1"]


def test_list_before_reads_continuation(monkeypatch):
    rows = [_row(f"r{i}", f"2026-10-{i:02d}T00:00:00+00:00") for i in range(1, 6)]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    first = client.get("/api/solitaire/public?limit=2").get_json()
    assert [i["id"] for i in first["items"]] == ["r5", "r4"]
    assert first["next"] == "2026-10-04T00:00:00+00:00"
    second = client.get("/api/solitaire/public", query_string={"limit": 2, "before": first["next"]}).get_json()
    assert [i["id"] for i in second["items"]] == ["r3", "r2"]
    third = client.get("/api/solitaire/public", query_string={"limit": 2, "before": second["next"]}).get_json()
    assert [i["id"] for i in third["items"]] == ["r1"]
    assert third["next"] is None       # 最後まで読んだら続きなし


def test_list_next_is_null_when_exactly_limit_remaining(monkeypatch):
    rows = [_row(f"r{i}", f"2026-10-{i:02d}T00:00:00+00:00") for i in range(1, 3)]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    data = client.get("/api/solitaire/public?limit=2").get_json()
    assert len(data["items"]) == 2 and data["next"] is None


def test_list_before_accepts_plus_turned_into_space(monkeypatch):
    """クエリ中の + が空白に化けた before も受け付ける"""
    rows = [_row("r1", "2026-10-01T00:00:00+00:00"), _row("r2", "2026-10-02T00:00:00+00:00")]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    resp = client.get("/api/solitaire/public?before=2026-10-02T00:00:00 00:00")
    assert resp.status_code == 200
    assert [i["id"] for i in resp.get_json()["items"]] == ["r1"]


def test_list_rejects_bad_before_and_limit(monkeypatch):
    _, client, _ = _load_app(monkeypatch, rows=_sample_rows())
    assert client.get("/api/solitaire/public?before=drop table").status_code == 400
    assert client.get("/api/solitaire/public?limit=abc").status_code == 400


def test_list_limit_is_capped_at_48(monkeypatch):
    rows = [_row(f"r{i:03d}", f"2026-10-01T00:{i // 60:02d}:{i % 60:02d}+00:00") for i in range(60)]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    data = client.get("/api/solitaire/public?limit=500").get_json()
    assert len(data["items"]) == 48
    assert data["next"] is not None
    assert len(client.get("/api/solitaire/public").get_json()["items"]) == 24   # 既定は 24


def test_list_has_its_own_rate_limit_slot(monkeypatch):
    _, client, _ = _load_app(monkeypatch, rows=_sample_rows())
    import solitaire_routes
    for _ in range(solitaire_routes.PUBLIC_LIST_RATE_LIMIT_MAX_REQUESTS):
        assert client.get("/api/solitaire/public").status_code == 200
    assert client.get("/api/solitaire/public").status_code == 429
    # 保存の枠は別なので、一覧で使い切っても保存は通る
    assert client.post("/api/solitaire/replay", json=_body()).status_code == 200


def test_kill_switch_stops_public_endpoints(monkeypatch):
    _, client, _ = _load_app(monkeypatch, flag="0", rows=_sample_rows())
    assert client.get("/api/solitaire/public").status_code == 404
    assert client.post("/api/solitaire/replay/r1/unpublish", json={"token": "x"}).status_code == 404


# ──────────────────────────────────────────────
# 取り下げ
# ──────────────────────────────────────────────

def _publish(client):
    data = client.post("/api/solitaire/replay", json=_body(public=True)).get_json()
    return data["id"], data["deleteToken"]


def test_unpublish_with_correct_token(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    rid, token = _publish(client)
    resp = client.post(f"/api/solitaire/replay/{rid}/unpublish", json={"token": token})
    assert resp.status_code == 200
    row = sb.rows[0]                      # 行は消えない
    assert row["id"] == rid
    assert row["is_public"] is False
    assert row["published_at"] is None
    assert row["delete_token_hash"] is None
    assert client.get("/api/solitaire/public").get_json()["items"] == []
    # 取り下げ後に同じ鍵で再度呼んでも通らない（鍵のハッシュは消えている）
    assert client.post(f"/api/solitaire/replay/{rid}/unpublish", json={"token": token}).status_code == 403


def test_unpublish_with_wrong_token_is_403_and_changes_nothing(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    rid, token = _publish(client)
    before = dict(sb.rows[0])
    for bad in ({"token": "wrong"}, {"token": ""}, {"token": 123}, {}, None):
        resp = client.post(f"/api/solitaire/replay/{rid}/unpublish", json=bad)
        assert resp.status_code == 403, bad
        # クライアントが鍵を捨てる目印（別の 403 と見分ける）
        assert resp.get_json()["code"] == "not_unpublishable", bad
    assert sb.rows[0] == before


def test_unpublish_unknown_id_and_non_public_row_are_403(monkeypatch):
    _, client, _ = _load_app(monkeypatch, rows=[_row("p1", None, public=False)])
    assert client.post("/api/solitaire/replay/nope/unpublish", json={"token": "x"}).status_code == 403
    assert client.post("/api/solitaire/replay/p1/unpublish", json={"token": "x"}).status_code == 403


def test_unpublish_rate_limit(monkeypatch):
    _, client, _ = _load_app(monkeypatch)
    import solitaire_routes
    for _ in range(solitaire_routes.UNPUBLISH_RATE_LIMIT_MAX_REQUESTS):
        assert client.post("/api/solitaire/replay/x/unpublish", json={"token": "t"}).status_code == 403
    assert client.post("/api/solitaire/replay/x/unpublish", json={"token": "t"}).status_code == 429


# ──────────────────────────────────────────────
# 90 日削除
# ──────────────────────────────────────────────

def test_cleanup_does_not_delete_public_rows(monkeypatch):
    import collect_prices

    class _Sb(FakeSupabase):
        def table(self, name):
            if name == "solitaire_replays":
                return _Query(self.rows)
            return _Noop()

    class _Noop:
        def __getattr__(self, _):
            return lambda *a, **k: self

        def execute(self):
            return _Resp([])

    old = "2020-01-01T00:00:00+00:00"
    rows = [
        {**_row("pub", "2026-10-01T00:00:00+00:00"), "created_at": old},
        {**_row("priv", None, public=False), "created_at": old},
        {**_row("new", None, public=False), "created_at": "2999-01-01T00:00:00+00:00"},
    ]
    sb = _Sb(rows)
    collect_prices.cleanup_old_data(sb)
    assert sorted(r["id"] for r in sb.rows) == ["new", "pub"]


# ──────────────────────────────────────────────
# 禁止語モジュール
# ──────────────────────────────────────────────

def test_moderation_normalizes_and_loads_word_list(tmp_path):
    import replay_moderation as m
    p = tmp_path / "w.txt"
    p.write_text("# コメント\n\nＢａｄ\n悪い　言葉\n", encoding="utf-8")
    words = m.load_ng_words(str(p))
    assert words == ("bad", "悪い言葉")
    assert m.contains_ng_word("これは BAD です", words)
    assert m.contains_ng_word("悪い 言葉", words)
    assert not m.contains_ng_word("灰流うらら展開", words)
    assert not m.contains_ng_word("", words)


def test_moderation_missing_list_warns_and_passes(tmp_path, caplog):
    import replay_moderation as m
    with caplog.at_level("WARNING"):
        assert m.load_ng_words(str(tmp_path / "none.txt")) == ()
    assert "禁止語リストを読み込めません" in caplog.text


def test_default_word_list_has_entries_and_passes_normal_titles():
    import replay_moderation as m
    assert len(m.load_ng_words()) > 0
    for ok in ("先攻ワンキル展開", "灰流うららを受けた場合", "Snake-Eye 先行 3妨害", "初心者向けデッキ回し"):
        assert not m.contains_ng_word(ok), ok


# ──────────────────────────────────────────────
# 管理画面 API
# ──────────────────────────────────────────────

def _admin_client(monkeypatch, rows):
    import admin_unreleased as admin_module
    sb = FakeSupabase(rows)
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    # 認証失敗の連続で他テストを巻き込まないよう、失敗ログを空にしておく
    admin_module._auth_fail_log.clear()
    admin_module._auth_fail_global_log.clear()
    import app as app_module
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client(), sb


ADMIN = {"X-Admin-Key": "test-key"}


def test_admin_endpoints_require_key(monkeypatch):
    client, sb = _admin_client(monkeypatch, _sample_rows())
    for method, url in (
        ("get", "/api/admin/public-replays"),
        ("post", "/api/admin/public-replays/r1/toggle-hidden"),
        ("delete", "/api/admin/public-replays/r1"),
    ):
        assert getattr(client, method)(url).status_code == 401, url
    # 誤ったキーも拒否（認証失敗は IP 単位で 5 回まで数えるので、ここでは 1 回だけ）
    assert client.get("/api/admin/public-replays", headers={"X-Admin-Key": "bad"}).status_code == 401
    assert len(sb.rows) == 5 and sb.rows[0]["hidden"] is False
    import admin_unreleased as admin_module
    admin_module._auth_fail_log.clear()
    admin_module._auth_fail_global_log.clear()


def test_admin_list_includes_hidden_flag_and_only_public(monkeypatch):
    client, _ = _admin_client(monkeypatch, _sample_rows())
    items = client.get("/api/admin/public-replays", headers=ADMIN).get_json()["items"]
    assert [i["id"] for i in items] == ["r5", "r3", "r2", "r1"]   # 非公開の r4 は出ない・新しい順
    assert {i["id"]: i["hidden"] for i in items}["r3"] is True


def test_admin_toggle_hidden_and_list_effect(monkeypatch):
    client, sb = _admin_client(monkeypatch, _sample_rows())
    resp = client.post("/api/admin/public-replays/r1/toggle-hidden", headers=ADMIN)
    assert resp.get_json()["hidden"] is True
    assert next(r for r in sb.rows if r["id"] == "r1")["hidden"] is True
    resp = client.post("/api/admin/public-replays/r1/toggle-hidden", headers=ADMIN)
    assert resp.get_json()["hidden"] is False
    # 非公開の行は対象外
    assert client.post("/api/admin/public-replays/r4/toggle-hidden", headers=ADMIN).status_code == 404


def test_admin_delete_removes_row_only_for_public(monkeypatch):
    client, sb = _admin_client(monkeypatch, _sample_rows())
    assert client.delete("/api/admin/public-replays/r2", headers=ADMIN).status_code == 200
    assert "r2" not in [r["id"] for r in sb.rows]
    assert client.delete("/api/admin/public-replays/r2", headers=ADMIN).status_code == 404
    assert client.delete("/api/admin/public-replays/r4", headers=ADMIN).status_code == 404   # 非公開は消さない
    assert "r4" in [r["id"] for r in sb.rows]


def test_admin_page_has_public_replays_tab():
    import app as app_module
    app_module.app.config.update(TESTING=True)
    html = app_module.app.test_client().get("/admin").get_data(as_text=True)
    assert 'data-tab="public-replays"' in html and 'id="tab-public-replays"' in html


def test_public_card_names_drop_ng_word_names_but_still_save(monkeypatch):
    """禁止語を含むカード名は card_names から外す（保存自体は断らない）"""
    _, client, sb = _load_app(monkeypatch)
    images = {"a": "https://e/a.png", "b": "https://e/b.png", "c": "https://e/c.png"}
    names = {"a": "灰流うらら", "b": "Ｆｕｃｋ カード", "c": "死 ね"}
    resp = client.post("/api/solitaire/replay", json=_body(public=True, images=images, names=names))
    assert resp.status_code == 200
    assert sb.rows[0]["card_names"] == ["灰流うらら"]


# ──────────────────────────────────────────────
# リプレイ一覧ページの仮の枠の数（2026-10-21 CLS 対策）
# ──────────────────────────────────────────────

def test_replays_page_skeleton_count_from_server(monkeypatch):
    """仮の枠の数は、サーバーが数えた最初のページの件数（非公開・非表示は数えない）。"""
    rows = [_row("a", "2026-10-03"), _row("b", "2026-10-02"),
            _row("c", "2026-10-01", hidden=True), _row("d", None, public=False)]
    app_module, client, _ = _load_app(monkeypatch, rows=rows)
    import solitaire_routes
    solitaire_routes._public_count_cache.update(at=0.0, value=None)
    html = client.get("/solitaire/replays").get_data(as_text=True)
    assert "var n=1;n=2;" in html
    assert "rp_public_first_count" not in html.split("var n=1;")[1].split("for(")[0]


def test_replays_page_skeleton_count_is_cached(monkeypatch):
    """ページを開くたびに DB を引かない（2分覚える）。"""
    app_module, client, sb = _load_app(monkeypatch, rows=[_row("a", "2026-10-03")])
    import solitaire_routes
    solitaire_routes._public_count_cache.update(at=0.0, value=None)
    assert "n=1;" in client.get("/solitaire/replays").get_data(as_text=True).split("var n=1;")[1][:6]
    sb.rows.append(_row("b", "2026-10-04"))
    assert "var n=1;n=1;" in client.get("/solitaire/replays").get_data(as_text=True)
    solitaire_routes._public_count_cache.update(at=0.0)  # 期限切れにする
    assert "var n=1;n=2;" in client.get("/solitaire/replays").get_data(as_text=True)


def test_replays_page_skeleton_falls_back_without_db(monkeypatch):
    """DB に繋がらないときは、前回の件数（localStorage）・初めては1枚のまま。"""
    app_module, client, _ = _load_app(monkeypatch)
    import solitaire_routes
    solitaire_routes._public_count_cache.update(at=0.0, value=None)
    app_module._supabase_client = None
    html = client.get("/solitaire/replays").get_data(as_text=True)
    assert "var n=1;try{var v=parseInt(localStorage.getItem('rp_public_first_count')" in html
