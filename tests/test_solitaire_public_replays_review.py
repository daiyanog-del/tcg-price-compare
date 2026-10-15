"""
tests/test_solitaire_public_replays_review.py — みんなのリプレイ段階2のレビュー指摘への対応の検証

  - card の絞り込み（text[] の配列リテラル。postgrest-py の実物で URL を組み立てて確かめる）
  - 非表示（hidden）は直リンクの API・OGメタ情報・OG画像でも「無いもの」
  - 管理画面の削除・非表示で OG 画像キャッシュを消す・一覧の続き読みと id 検索
  - 禁止語の誤検知と抜け道・リストが使えないときは公開だけ 503
  - タイトルの整形（文字列でなければ空・制御文字の除去）
"""

import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_solitaire_public_replays import (  # noqa: E402
    ADMIN, _admin_client, _body, _load_app, _row,
)

BS = chr(92)   # バックスラッシュ


def test_pg_array_literal_builds_real_postgrest_url():
    """postgrest-py の実物で URL を組み立て、カンマ・引用符・{}・バックスラッシュ・NULL を含む名前が
    1要素の配列リテラルとして渡ることを確かめる（通信はしない）"""
    from postgrest import SyncPostgrestClient
    import solitaire_routes as sr
    client = SyncPostgrestClient("http://x/rest/v1")
    cases = {
        "A,B": '{"A,B"}',
        'q"x': '{"q' + BS + '"x"}',
        "{y}": '{"{y}"}',
        "NULL": '{"NULL"}',
        "back" + BS + "slash": '{"back' + BS * 2 + 'slash"}',
    }
    for name, expected in cases.items():
        assert sr._pg_array_literal(name) == expected, name
        built = client.table("solitaire_replays").select("id").contains("card_names", sr._pg_array_literal(name))
        assert built.request.params.get("card_names") == "cs." + expected, name


def test_list_card_filter_with_special_characters(monkeypatch):
    rows = [
        _row("r1", "2026-10-01T00:00:00+00:00", cards=("A,B", 'q"x', "back" + BS + "slash", "{y}", "NULL")),
        _row("r2", "2026-10-02T00:00:00+00:00", cards=("A", "B")),
    ]
    _, client, _ = _load_app(monkeypatch, rows=rows)
    for name, ids in (("A,B", ["r1"]), ('q"x', ["r1"]), ("back" + BS + "slash", ["r1"]),
                      ("{y}", ["r1"]), ("NULL", ["r1"]), ("A", ["r2"])):
        resp = client.get("/api/solitaire/public?card=" + quote(name))
        assert resp.status_code == 200, name
        assert [i["id"] for i in resp.get_json()["items"]] == ids, name


def _hidden_row():
    r = _row("hidden01", "2026-10-03T00:00:00+00:00", title="秘密のタイトル", hidden=True)
    r.update({"images": {"a": "x"}, "names": {}, "ex_card_ids": [], "logs": [{"t": 1}], "card_types": None, "board": {"b": 1}})
    return r


def _shown_row():
    r = _hidden_row()
    r.update({"id": "shown001", "title": "見えるタイトル", "hidden": False})
    return r


def test_hidden_replay_is_treated_as_missing_everywhere(monkeypatch):
    """hidden=true は、直リンクの API・ページのOGメタ情報・OG画像のすべてで「無いもの」"""
    import replay_og_image
    _, client, _ = _load_app(monkeypatch, rows=[_hidden_row(), _shown_row()])
    import solitaire_routes
    monkeypatch.setattr(solitaire_routes, "validate_board", lambda b: {"ok": 1})
    monkeypatch.setattr(replay_og_image, "read_any_cached", lambda rid: None)
    monkeypatch.setattr(replay_og_image, "get_or_render", lambda rid, board, title="": (b"RENDERED", False))
    # API
    assert client.get("/api/solitaire/replay/hidden01").status_code == 404
    assert client.get("/api/solitaire/replay/shown001").status_code == 200
    # ページのOGメタ情報（題名・画像）
    hid_html = client.get("/solitaire?replay=hidden01").get_data(as_text=True)
    assert "秘密のタイトル" not in hid_html and "/solitaire/og/hidden01.png" not in hid_html
    shown_html = client.get("/solitaire?replay=shown001").get_data(as_text=True)
    assert "見えるタイトル" in shown_html and "/solitaire/og/shown001.png" in shown_html
    # OG画像
    with open(replay_og_image.GENERIC_IMAGE_PATH, "rb") as f:
        generic = f.read()
    assert client.get("/solitaire/og/hidden01.png").get_data() == generic
    assert client.get("/solitaire/og/shown001.png").get_data() == b"RENDERED"


def test_purge_cache_removes_disk_and_degraded(monkeypatch, tmp_path):
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    (tmp_path / "abcdef12.png").write_bytes(b"png")
    og._store_degraded("abcdef12", b"degraded")
    assert og.read_any_cached("abcdef12") is not None
    og.purge_cache("abcdef12")
    assert not (tmp_path / "abcdef12.png").exists()
    assert og._read_degraded("abcdef12") is None
    og.purge_cache("abcdef12")    # 無くても例外にしない
    og.purge_cache("../evil")     # 不正な id は何もしない


def test_admin_hide_and_delete_purge_og_cache(monkeypatch, tmp_path):
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    client, _ = _admin_client(monkeypatch, [_row("abcdef12", "2026-10-01T00:00:00+00:00"),
                                            _row("zyxwvu34", "2026-10-02T00:00:00+00:00")])
    for rid in ("abcdef12", "zyxwvu34"):
        (tmp_path / f"{rid}.png").write_bytes(b"png")
    client.post("/api/admin/public-replays/abcdef12/toggle-hidden", headers=ADMIN)
    assert not (tmp_path / "abcdef12.png").exists()
    assert (tmp_path / "zyxwvu34.png").exists()
    client.delete("/api/admin/public-replays/zyxwvu34", headers=ADMIN)
    assert not (tmp_path / "zyxwvu34.png").exists()


def test_admin_list_paginates_and_finds_by_id(monkeypatch):
    import admin_unreleased as admin_module
    rows = [_row(f"r{i}", f"2026-10-{i:02d}T00:00:00+00:00") for i in range(1, 6)]
    client, _ = _admin_client(monkeypatch, rows)
    monkeypatch.setattr(admin_module, "_PUBLIC_REPLAY_ADMIN_LIMIT", 2)
    first = client.get("/api/admin/public-replays", headers=ADMIN).get_json()
    assert [i["id"] for i in first["items"]] == ["r5", "r4"] and first["next"]
    second = client.get("/api/admin/public-replays", query_string={"before": first["next"]}, headers=ADMIN).get_json()
    assert [i["id"] for i in second["items"]] == ["r3", "r2"]
    third = client.get("/api/admin/public-replays", query_string={"before": second["next"]}, headers=ADMIN).get_json()
    assert [i["id"] for i in third["items"]] == ["r1"] and third["next"] is None
    one = client.get("/api/admin/public-replays?id=r2", headers=ADMIN).get_json()
    assert [i["id"] for i in one["items"]] == ["r2"] and one["next"] is None
    assert client.get("/api/admin/public-replays?id=none", headers=ADMIN).get_json()["items"] == []
    assert client.get("/api/admin/public-replays?before=drop table", headers=ADMIN).status_code == 400


def test_moderation_no_false_positive_on_yugioh_style_titles():
    import replay_moderation as m
    titles = ["瞬殺する先攻展開", "相殺と封殺のループ", "灰流うらら×増殖するG 受け", "ブラマジ素引き3妨害",
              "斬機サイクロン初動", "純エンディミオン 先行", "ホルス・ブラックフレイム・ドラゴン",
              "Ｓ：Ｐリトルナイト 展開", "ドラゴンメイド 連続ターン", "初心者向け 手札誘発 入門"]
    for t in titles:
        assert not m.contains_ng_word(t), t


def test_moderation_closes_bypasses():
    import replay_moderation as m
    for t in ["シネ", "ﾁﾝｺ", "ちんこ", "死_ね", "死ーね", "死­ね", "死️ね", "死​ね",
              "死・ね", "死-ね", "死.ね", "Ｆ.ｕ.ｃ.ｋ"]:
        assert m.contains_ng_word(t), repr(t)
    assert not m.contains_ng_word("殺陣の練習")   # 「殺す」はリストに入れない


def test_public_save_refuses_when_ng_list_unavailable_but_normal_share_passes(monkeypatch):
    import replay_moderation as m
    monkeypatch.setattr(m, "load_ng_words", lambda *a, **k: ())
    monkeypatch.setattr(m, "_ng_words_cache", None)
    _, client, sb = _load_app(monkeypatch)
    resp = client.post("/api/solitaire/replay", json=_body(public=True, title="普通のタイトル"))
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "現在公開できません"
    assert sb.rows == []
    # 公開でない共有は通す
    assert client.post("/api/solitaire/replay", json=_body(title="普通のタイトル")).status_code == 200


def test_title_non_string_is_empty_and_control_chars_are_removed(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    r = client.post("/api/solitaire/replay", json=_body(public=True, title=None))
    assert r.status_code == 400 and r.get_json()["error"] == "公開するにはタイトルを入れてください"
    assert client.post("/api/solitaire/replay", json=_body(public=True, title=123)).status_code == 400
    ok = client.post("/api/solitaire/replay",
                     json=_body(public=True, title="一行目\n二行目‮evil⁦x⁩​"))
    assert ok.status_code == 200
    assert sb.rows[0]["title"] == "一行目 二行目evilx"
    # 公開でない保存で title が None でも「None」にならない
    client.post("/api/solitaire/replay", json=_body(title=None))
    assert sb.rows[1]["title"] == ""


def test_card_names_with_ng_word_are_dropped_after_normalization(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    images = {"a": "https://e/a.png", "b": "https://e/b.png"}
    resp = client.post("/api/solitaire/replay",
                       json=_body(public=True, images=images, names={"a": "灰流うらら", "b": "チ_ン_コ"}))
    assert resp.status_code == 200
    assert sb.rows[0]["card_names"] == ["灰流うらら"]
