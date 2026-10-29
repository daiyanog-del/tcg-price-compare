"""
tests/test_replay_image_policy.py — 共有リプレイの画像辞書を絞る・無くなったリプレイの絵を返さない（2026-10-29 監査の指摘）

  - 保存時: 任意の外部URL・形の違う data: は card://カード名 に置き換え、card://・トークン・発売済みカードの URL は残す
  - サーバーとブラウザの「残してよい発行元」が同じ値
  - 管理画面: id で探すと公開していない共有リプレイも出て、非表示・削除できる
  - リンク画像・一覧の縮小版: キャッシュがあっても、無くなった（非表示・削除）リプレイの絵は返さない
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_solitaire_public_replays import (  # noqa: E402
    ADMIN, _admin_client, _body, _load_app, _row,
)

ROOT = Path(__file__).resolve().parent.parent
YGORES = "https://artworks-jp-n.ygoresources.com/1/23/45_1.png"
TOKEN = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD="


def test_js_and_python_allow_the_same_sources():
    from constants import REPLAY_IMAGE_SOURCES
    js = (ROOT / "static/solitaire/js/utils/replay-image-policy.js").read_text(encoding="utf-8")
    block = re.search(r"ALLOWED_IMAGE_SOURCES = \[(.*?)\];", js, re.S).group(1)
    pairs = re.findall(r"\['([^']+)', String\.raw`([^`]+)`\]", block)
    assert pairs == [tuple(p) for p in REPLAY_IMAGE_SOURCES]


def test_clean_images_keeps_known_values_and_replaces_the_rest():
    import solitaire_routes as sr
    images = {
        "1": "card://灰流うらら",
        "2": TOKEN,
        "3": YGORES,
        "4": "https://evil.example/x.png",
        "5": "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "6": "data:text/html,<script>",
        "7": 123,
        "8": "https://artworks-jp-n.ygoresources.com@evil.example/x.png",
        "9": "http://artworks-jp-n.ygoresources.com/1/2/3_1.png",
        "10": "data:image/png;base64," + "A" * sr.REPLAY_TOKEN_IMAGE_MAX_CHARS,
        "11": "javascript:alert(1)",
        "12": "https://www.ka-nabell.com/redirect?url=https://evil.example",
        "13": "https://www.ka-nabell.com/img/s/abc_123.jpg",
        "14": YGORES + "?x=1",
        "15": "https://artworks-jp-n.ygoresources.com/redirect",
    }
    names = {"4": "増殖するG", "5": "トークン"}
    out, replaced = sr._clean_images(images, names)
    assert out["1"] == "card://灰流うらら"
    assert out["2"] == TOKEN
    assert out["3"] == YGORES
    assert out["13"] == "https://www.ka-nabell.com/img/s/abc_123.jpg"
    assert out["4"] == "card://増殖するG"
    # 形の違う data: はトークンのまま（中身の無い印）にする。card:// にするとトークンの削除の手が効かなくなる
    for k in ("5", "6", "10"):
        assert out[k] == sr._TOKEN_PLACEHOLDER, k
    for k in ("7", "8", "9", "11", "12", "14", "15"):
        assert out[k] == "card://", k
    assert replaced == 11


def test_save_stores_cleaned_images(monkeypatch):
    _, client, sb = _load_app(monkeypatch)
    resp = client.post("/api/solitaire/replay", json=_body(
        images={"a": "https://evil.example/a.png", "b": YGORES, "c": TOKEN},
        names={"a": "灰流うらら", "b": "増殖するG", "c": "羊"},
        cardTypes={"a": "monster", "b": "monster"},
    ))
    assert resp.status_code == 200
    row = sb.rows[-1]
    assert row["images"] == {"a": "card://灰流うらら", "b": YGORES, "c": TOKEN}
    assert row["card_types"] == {"a": "monster", "b": "monster"}


def test_admin_finds_hides_and_deletes_unlisted_replay(monkeypatch):
    unlisted = _row("unlist01", None, public=False)
    client, sb = _admin_client(monkeypatch, [unlisted, _row("pub00001", "2026-10-02T00:00:00+00:00")])
    # 通常の一覧には出ない（公開中だけ）
    listed = client.get("/api/admin/public-replays", headers=ADMIN).get_json()["items"]
    assert [i["id"] for i in listed] == ["pub00001"]
    # id で探すと出る
    found = client.get("/api/admin/public-replays?id=unlist01", headers=ADMIN).get_json()["items"]
    assert [i["id"] for i in found] == ["unlist01"] and found[0]["is_public"] is False
    # 非表示・削除できる
    assert client.post("/api/admin/public-replays/unlist01/toggle-hidden", headers=ADMIN).get_json()["hidden"] is True
    assert sb.rows[0]["hidden"] is True
    assert client.delete("/api/admin/public-replays/unlist01", headers=ADMIN).status_code == 200
    assert [r["id"] for r in sb.rows] == ["pub00001"]


def test_cached_og_image_is_not_served_after_replay_is_gone(monkeypatch, tmp_path):
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    rows = [_row("gone0001", "2026-10-01T00:00:00+00:00"), _row("live0001", "2026-10-02T00:00:00+00:00")]
    _, client, sb = _load_app(monkeypatch, rows=rows)
    for rid in ("gone0001", "live0001"):
        (tmp_path / f"{rid}.png").write_bytes(b"CACHED-" + rid.encode())
    with open(og.GENERIC_IMAGE_PATH, "rb") as f:
        generic = f.read()
    # ある間はキャッシュを返す
    assert client.get("/solitaire/og/gone0001.png").get_data() == b"CACHED-gone0001"
    # 別のプロセス（90日の自動削除など）で行が消えても、ある・無いの記憶が切れたら汎用画像になりキャッシュも消える
    sb.rows.remove(next(r for r in sb.rows if r["id"] == "gone0001"))
    import solitaire_routes
    solitaire_routes._og_alive_cache.clear()
    assert client.get("/solitaire/og/gone0001.png").get_data() == generic
    assert not (tmp_path / "gone0001.png").exists()
    assert client.get("/solitaire/og/live0001.png").get_data() == b"CACHED-live0001"


def test_thumb_memory_cache_is_dropped_when_replay_is_hidden(monkeypatch, tmp_path):
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    rows = [_row("hide0001", "2026-10-01T00:00:00+00:00")]
    _, client, sb = _load_app(monkeypatch, rows=rows)
    import solitaire_routes
    solitaire_routes._og_thumb_cache["hide0001"] = b"THUMB"
    assert client.get("/solitaire/og/thumb/hide0001.webp").get_data() == b"THUMB"
    sb.rows[0]["hidden"] = True
    solitaire_routes._og_alive_cache.clear()
    resp = client.get("/solitaire/og/thumb/hide0001.webp")
    assert resp.get_data() != b"THUMB"
    assert "hide0001" not in solitaire_routes._og_thumb_cache


def test_og_alive_check_does_not_wait_on_db_again_while_db_is_down(monkeypatch, tmp_path):
    """DB に聞けないときはキャッシュの絵を返し、しばらく聞き直さない（一覧の24枚が DB 待ちで固まらない）"""
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    _, client, sb = _load_app(monkeypatch, rows=[_row("down0001", "2026-10-01T00:00:00+00:00")])
    import solitaire_routes
    (tmp_path / "down0001.png").write_bytes(b"CACHED")
    calls = []

    class _Down:
        def table(self, name):
            calls.append(name)
            raise RuntimeError("db down")
    monkeypatch.setattr(solitaire_routes, "_get_supabase", lambda: _Down())
    for _ in range(3):
        assert client.get("/solitaire/og/down0001.png").get_data() == b"CACHED"
    assert len(calls) == 1


def test_public_list_and_page_remember_alive_so_thumbs_need_no_query(monkeypatch, tmp_path):
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    rows = [_row("list0001", "2026-10-01T00:00:00+00:00"), _row("list0002", "2026-10-02T00:00:00+00:00")]
    _, client, sb = _load_app(monkeypatch, rows=rows)
    import solitaire_routes
    assert client.get("/api/solitaire/public").status_code == 200
    calls = []
    monkeypatch.setattr(solitaire_routes, "_lookup_replay_alive", lambda rid: calls.append(rid) or True)
    for rid in ("list0001", "list0002"):
        solitaire_routes._og_thumb_cache[rid] = b"THUMB"
        assert client.get(f"/solitaire/og/thumb/{rid}.webp").get_data() == b"THUMB"
    assert calls == []


def test_admin_hide_drops_thumb_and_alive_memory(monkeypatch, tmp_path):
    import replay_og_image as og
    monkeypatch.setattr(og, "_OG_CACHE_DIR", str(tmp_path))
    import admin_unreleased as admin_module
    # 管理画面が呼ぶ forget_replay_og が持つキャッシュ（テストでは solitaire_routes が読み直されていることがある）
    g = admin_module._forget_replay_og.__globals__
    client, _ = _admin_client(monkeypatch, [_row("admhide1", None, public=False)])
    g["_og_thumb_cache"]["admhide1"] = b"THUMB"
    g["_remember_og_alive"]("admhide1", True)
    assert client.post("/api/admin/public-replays/admhide1/toggle-hidden", headers=ADMIN).status_code == 200
    assert "admhide1" not in g["_og_thumb_cache"]
    assert "admhide1" not in g["_og_alive_cache"]
