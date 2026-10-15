"""
tests/test_solitaire_og.py — 共有リプレイのリンク画像（OGP）

対象:
  - replay_board.validate_board（正常・不正・上限超え・カード1枚だけ不正・画像URLを受け取らないこと）
  - POST /api/solitaire/replay の board 受け取り（不正でも共有は成功・保存payloadに入るのは検証済みのみ）
  - GET /solitaire/og/<id>.png（board あり=1200x630のPNG・無し=汎用画像・不正id=404）
  - GET /solitaire?replay=<id> の og:image / og:title / og:url 出し分け
  - 画像はカード名からサーバーが解決する経路（利用者の送った img は取りに行かない）・トークン・
    描画の予算（プロセス全体）・描画枠の待ち時間切れ・不完全描画の短期キャッシュ・
    索引/辞書が使えないときの不完全扱い・名前解決は1回にまとめること
外部への画像取得（カードDL・card_display・DB）はすべてモックする。
"""

import importlib
import io
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import replay_board  # noqa: E402
from replay_board import validate_board  # noqa: E402


def _board(**over):
    b = {
        "v": 1, "moves": 12,
        "field": [
            {"slot": "custom-slot-5", "name": "閃刀姫-レイ", "pos": "atk", "face": "up"},
            {"slot": "custom-slot-6", "name": "未発売カード", "pos": "def", "face": "down", "n": 2},
            {"slot": "custom-slot-3", "name": "フィールド魔法", "pos": "atk", "face": "up"},
            {"slot": "custom-slot-7", "name": "トークン", "token": True, "pos": "atk", "face": "up"},
        ],
        "hand": [{"name": "手札A"}, {"name": "手札B"}],
        "grave": {"count": 7, "cards": [{"name": "墓地の奥"}, {"name": "墓地の上"}]},
        "banish": {"count": 1, "cards": [{"name": "裏の除外", "face": "down"}]},
        "ex": {"count": 15}, "deckCount": 30,
    }
    b.update(over)
    return b


# ── validate_board ──

class TestValidateBoard:
    def test_valid_board_passes_and_normalizes(self):
        b = _board()
        b["field"][0]["evil"] = "<script>"   # 未知キーは落とす
        out = validate_board(b)
        assert out is not None
        assert "evil" not in out["field"][0]
        assert out["field"][1]["n"] == 2
        assert out["banish"]["cards"] == [{"name": "裏の除外", "face": "down"}]

    def test_none_returns_none(self):
        assert validate_board(None) is None

    @pytest.mark.parametrize("bad", [
        "str", [], {"v": 2}, {},
        _board(field="x"), _board(hand=None), _board(ex=None), _board(grave=None),
        _board(moves=-1), _board(moves=True), _board(deckCount="30"), _board(deckCount=10**6),
    ])
    def test_malformed_returns_none(self, bad):
        assert validate_board(bad) is None

    def test_field_over_limit(self):
        field = [{"slot": f"custom-slot-{i % 14 + 1}", "name": "a", "pos": "atk", "face": "up"}
                 for i in range(replay_board.BOARD_MAX_FIELD + 1)]
        assert validate_board(_board(field=field)) is None

    def test_hand_over_limit(self):
        hand = [{"name": "a"}] * (replay_board.BOARD_MAX_HAND + 1)
        assert validate_board(_board(hand=hand)) is None

    def test_hand_at_limit_ok(self):
        hand = [{"name": "a"}] * replay_board.BOARD_MAX_HAND
        assert validate_board(_board(hand=hand)) is not None

    @pytest.mark.parametrize("img", [
        "javascript:alert(1)", "http://example.com/a.png", "https://127.0.0.1:9\\@artworks-jp-n.ygoresources.com/x.png",
        "https://user@artworks-jp-n.ygoresources.com/x.png", "https://localhost:8080/x", "data:image/png;base64,AAAA",
        "file:///etc/passwd", "card://x",
    ])
    def test_img_is_never_accepted_or_stored(self, img):
        """img を送られても無視し、正規化後の board には一切残らない（サーバーは利用者のURLを取りに行かない）"""
        b = _board()
        b["hand"][0]["img"] = img
        b["field"][0]["img"] = img
        b["grave"]["cards"][0]["img"] = img
        out = validate_board(b)
        assert out is not None
        assert "img" not in str(out) and img not in str(out)

    def test_one_bad_card_is_dropped_but_board_is_kept(self):
        b = _board()
        b["hand"].insert(0, {"name": "あ" * 101})            # 長すぎる名前
        b["hand"].insert(0, "not-a-dict")
        b["hand"].insert(0, {"name": "x", "token": "yes"})   # token が真偽値でない
        b["field"].insert(0, {"slot": "custom-slot-99", "name": "x", "pos": "atk", "face": "up"})
        b["field"].insert(0, {"slot": "custom-slot-5", "name": "重複", "pos": "atk", "face": "up"})
        b["grave"]["cards"] = [{"name": 123}]
        out = validate_board(b)
        assert out is not None
        assert [c["name"] for c in out["hand"]] == ["手札A", "手札B"]
        assert [f["slot"] for f in out["field"]][0] == "custom-slot-5" and len(out["field"]) == 4
        assert out["field"][0]["name"] == "重複"   # 先勝ち（同じマスの2つ目は除外される）
        assert out["grave"]["cards"] == [] and out["grave"]["count"] == 7

    def test_token_and_pile_face_are_kept(self):
        out = validate_board(_board())
        assert out["field"][3]["token"] is True
        assert out["banish"]["cards"] == [{"name": "裏の除外", "face": "down"}]

    def test_pile_keeps_last_cards_only(self):
        """墓地・除外の cards は末尾（一番上）側の最大 PILE_MAX_CARDS 枚。count は元の枚数のまま"""
        cards = [{"name": f"c{i}"} for i in range(replay_board.PILE_MAX_CARDS + 3)]
        out = validate_board(_board(grave={"count": 30, "cards": cards}))
        assert [c["name"] for c in out["grave"]["cards"]] == [c["name"] for c in cards[-replay_board.PILE_MAX_CARDS:]]
        assert out["grave"]["count"] == 30

    def test_pile_cards_over_input_limit_is_malformed(self):
        cards = [{"name": "a"}] * (replay_board.PILE_MAX_INPUT + 1)
        assert validate_board(_board(grave={"count": 5, "cards": cards})) is None
        assert validate_board(_board(grave={"count": 5, "cards": "x"})) is None

    def test_pile_count_is_at_least_cards(self):
        out = validate_board(_board(grave={"count": 1, "cards": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}))
        assert out["grave"]["count"] == 3

    def test_legacy_top_only_pile_is_converted(self):
        """旧形式（top だけ）の board も受け取り、cards=[top] に直す（本番に保存済みの旧 board）"""
        out = validate_board(_board(grave={"count": 7, "top": {"name": "旧"}},
                                    banish={"count": 1, "top": {"name": "旧裏", "face": "down"}}))
        assert out["grave"] == {"count": 7, "cards": [{"name": "旧"}]}
        assert out["banish"]["cards"] == [{"name": "旧裏", "face": "down"}]
        assert "top" not in out["grave"]
        out = validate_board(_board(grave={"count": 0, "cards": []}))
        assert out["grave"] == {"count": 0, "cards": []}

    @pytest.mark.parametrize("slot", ["custom-slot-0", "custom-slot-15", "custom-slot-x", "side-slot-0", "poolRow", 5,
                                      "custom-slot-5\n"])
    def test_bad_slot_drops_only_that_card(self, slot):
        b = _board()
        b["field"][0]["slot"] = slot
        out = validate_board(b)
        assert out is not None and len(out["field"]) == 3

    def test_bad_state_drops_only_that_card(self):
        b = _board()
        b["field"][0]["pos"] = "sideways"
        out = validate_board(b)
        assert out is not None and len(out["field"]) == 3


# ── API / ルート ──

class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, store, name):
        self.store, self.name, self._eq = store, name, None

    def insert(self, payload):
        self.store["inserted"].append(payload)
        return self

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._eq = val
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self._eq is not None:
            row = self.store["rows"].get(self._eq)
            if self.store.get("raise"):
                raise RuntimeError("db down")
            return _Resp([row] if row else [])
        return None


class _FakeSupabase:
    def __init__(self):
        self.store = {"inserted": [], "rows": {}}

    def table(self, name):
        return _FakeTable(self.store, name)


def _fake_png_bytes():
    # _download_card_image の「1024バイト超」ガードを通るサイズのPNG（ノイズ画像）
    import os as _os
    im = Image.frombytes("RGB", (60, 90), _os.urandom(60 * 90 * 3))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", "1")
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    sb = _FakeSupabase()
    app_module._supabase_client = sb

    import replay_og_image
    import deck_image
    # ディスクキャッシュ類はテスト専用ディレクトリへ。レート制限・不完全描画のメモリ状態は毎回空にする
    (tmp_path / "og").mkdir()
    (tmp_path / "cards").mkdir()
    monkeypatch.setattr(replay_og_image, "_OG_CACHE_DIR", str(tmp_path / "og"))
    monkeypatch.setattr(deck_image, "_CARD_CACHE_DIR", str(tmp_path / "cards"))
    replay_og_image._budget_log.clear()
    replay_og_image._degraded_cache.clear()
    warmed = []
    monkeypatch.setattr(replay_og_image, "warm_cache_async", lambda *a: warmed.append(a))
    real_resolve_urls = replay_og_image._resolve_urls
    # 既定では名前→URL の解決を偽物にする（外部・辞書に依存させない）。実経路を通すテストは net フィクスチャを使う
    monkeypatch.setattr(replay_og_image, "_resolve_urls",
                        lambda names: ({n: f"https://fake.invalid/{n}.png" for n in names}, False))
    return type("Env", (), {"client": app_module.app.test_client(), "sb": sb, "warmed": warmed,
                            "og": replay_og_image, "routes": solitaire_routes, "tmp": tmp_path,
                            "real_resolve_urls": staticmethod(real_resolve_urls)})


@pytest.fixture
def fake_fetch(monkeypatch, env):
    """カード画像取得そのものを偽物にする（名前だけが渡る）。"""
    fetched = []

    def _fetch(name, url):
        fetched.append(name)
        return Image.new("RGB", (100, 146), (80, 120, 200)), False

    monkeypatch.setattr(env.og, "_fetch_card_image", _fetch)
    return fetched


@pytest.fixture
def net(monkeypatch, env):
    """実際の取得経路（名前→card_display.resolve_card_displays→deck_image._download_card_image→requests.get）を通し、
    外部通信だけを偽物にする。取りに行った URL を記録する。"""
    import card_display
    import requests
    monkeypatch.setattr(env.og, "_resolve_urls", env.real_resolve_urls)
    monkeypatch.setattr(card_display, "released_index_available", lambda: True)
    monkeypatch.setattr(card_display, "unreleased_data_available", lambda: True)
    batches, urls = [], []

    def fake_resolve_many(names):
        batches.append(list(names))
        return {n: {"kind": "image", "url": f"https://artworks-jp-n.ygoresources.com/by-name/{n}.png",
                    "source": "released"} for n in names}

    class _R:
        status_code = 200
        content = _fake_png_bytes()

    def fake_get(url, **kw):
        urls.append(url)
        return _R()

    def no_single(name):
        raise AssertionError("取得スレッドから resolve_card_display を個別に呼んではいけない")

    monkeypatch.setattr(card_display, "resolve_card_displays", fake_resolve_many)
    monkeypatch.setattr(card_display, "resolve_card_display", no_single)
    monkeypatch.setattr(requests, "get", fake_get)
    return type("Net", (), {"batches": batches, "urls": urls})


def _png_size(data):
    return Image.open(io.BytesIO(data)).size


def _generic():
    return Path(replay_og_image_generic_path()).read_bytes()


def replay_og_image_generic_path():
    import replay_og_image
    return replay_og_image.GENERIC_IMAGE_PATH


def _seed(env, rid="abcDEF123_-", board=None, title="閃刀姫 展開"):
    env.sb.store["rows"][rid] = {"title": title, "board": board if board is not None else _board()}
    return rid


def test_save_with_valid_board_stores_board_and_warms_cache(env):
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1], "title": "テスト", "board": _board()})
    assert resp.status_code == 200
    saved = env.sb.store["inserted"][-1]
    assert saved["board"]["field"][0]["slot"] == "custom-slot-5"
    assert len(env.warmed) == 1 and env.warmed[0][0] == resp.get_json()["id"]


def test_save_never_stores_img_even_if_sent(env):
    board = _board()
    board["hand"][0]["img"] = "https://127.0.0.1:9\\@artworks-jp-n.ygoresources.com/x.png"
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1], "board": board})
    assert resp.status_code == 200
    assert "img" not in str(env.sb.store["inserted"][-1]["board"])


def test_save_with_structurally_invalid_board_still_succeeds_without_board(env):
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1], "board": {"v": 9}})
    assert resp.status_code == 200
    assert "board" not in env.sb.store["inserted"][-1]
    assert env.warmed == []


def test_save_with_one_bad_card_keeps_board(env):
    board = _board()
    board["hand"].append({"name": "あ" * 500})
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1], "board": board})
    assert resp.status_code == 200
    assert len(env.sb.store["inserted"][-1]["board"]["hand"]) == 2


def test_save_without_board_is_backward_compatible(env):
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1]})
    assert resp.status_code == 200
    assert "board" not in env.sb.store["inserted"][-1]


# ── OG ルート ──

def test_og_route_renders_board_png_and_caches(env, fake_fetch):
    _seed(env)
    resp = env.client.get("/solitaire/og/abcDEF123_-.png")
    assert resp.status_code == 200 and resp.mimetype == "image/png"
    assert _png_size(resp.data) == (1200, 630)
    assert "max-age=86400" in resp.headers["Cache-Control"]
    n = len(fake_fetch)
    assert n > 0
    env.sb.store["rows"].clear()   # 2回目はディスクキャッシュ（DB・画像取得なし）
    resp2 = env.client.get("/solitaire/og/abcDEF123_-.png")
    assert resp2.data == resp.data and len(fake_fetch) == n


def test_images_are_resolved_from_card_names_only(env, net):
    """実際の取得経路: 取りに行く URL は card_display が名前から返したものだけ。トークン・裏向きは取りに行かない。"""
    _seed(env)
    resp = env.client.get("/solitaire/og/abcDEF123_-.png")
    assert resp.status_code == 200 and _png_size(resp.data) == (1200, 630)
    # 裏向きのフィールド・裏向きの除外・トークンは対象外。守備表示（表）は対象
    assert len(net.batches) == 1   # 名前の解決は取得を投げる前に1回だけ（未発売辞書の同時読み込みを避ける）
    assert set(net.batches[0]) == {"閃刀姫-レイ", "フィールド魔法", "手札A", "手札B", "墓地の上"}
    assert net.urls and all(u.startswith("https://artworks-jp-n.ygoresources.com/by-name/") for u in net.urls)


@pytest.mark.parametrize("evil", [
    "https://127.0.0.1:9\\@artworks-jp-n.ygoresources.com/x.png",
    "https://user@artworks-jp-n.ygoresources.com/x.png",
    "https://artworks-jp-n.ygoresources.com:8443/x.png",
    "https://169.254.169.254/latest/meta-data/",
    "http://localhost/",
    "card://閃刀姫-レイ",
])
def test_dangerous_img_is_never_fetched(env, net, evil):
    """保存API→DB→OG ルートの全経路で、利用者の送った img を一切取りに行かない。"""
    board = _board()
    for c in (board["hand"][0], board["field"][0], board["grave"]["cards"][0]):
        c["img"] = evil
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1], "board": board})
    saved = env.sb.store["inserted"][-1]
    rid = resp.get_json()["id"]
    env.sb.store["rows"][rid] = {"title": "t", "board": saved["board"]}
    # DB に直接 img 入りの board が入っていた場合（旧クライアント・手書き）も同じく無視される
    env.sb.store["rows"]["directInsert1"] = {"title": "t", "board": board}
    for r in (rid, "directInsert1"):
        assert env.client.get(f"/solitaire/og/{r}.png").status_code == 200
    assert net.urls
    assert not any(bad in u for u in net.urls for bad in ("127.0.0.1", "user@", ":8443", "169.254", "localhost"))
    assert all(u.startswith("https://artworks-jp-n.ygoresources.com/by-name/") for u in net.urls)


def test_token_board_renders_without_fetching_tokens(env, fake_fetch):
    board = _board(field=[{"slot": "custom-slot-5", "name": "トークン", "token": True, "pos": "atk", "face": "up"}],
                   hand=[{"name": "手札トークン", "token": True}], grave={"count": 1, "cards": [{"name": "墓", "token": True}]})
    _seed(env, board=board)
    resp = env.client.get("/solitaire/og/abcDEF123_-.png")
    assert resp.status_code == 200 and _png_size(resp.data) == (1200, 630)
    assert fake_fetch == []


def test_token_among_other_cards_keeps_whole_board(env, fake_fetch):
    """トークンが1枚あっても board 全体は捨てられず、他のカードは描かれる（旧実装は全体を捨てていた）。"""
    resp = env.client.post("/api/solitaire/replay", json={"logs": [1], "board": _board()})
    saved = env.sb.store["inserted"][-1]["board"]
    assert any(f.get("token") for f in saved["field"]) and len(saved["field"]) == 4


def test_hand_is_capped_and_shows_plus_n(env, fake_fetch):
    _seed(env, board=_board(hand=[{"name": f"手札{i}"} for i in range(60)], field=[],
                            grave={"count": 0, "cards": []}, banish={"count": 0, "cards": []}))
    resp = env.client.get("/solitaire/og/abcDEF123_-.png")
    assert resp.status_code == 200
    assert len(fake_fetch) == env.og.HAND_DRAW_MAX   # 手札60枚でも取りに行くのは先頭10枚だけ


def test_og_route_without_board_returns_generic_image(env, fake_fetch):
    env.sb.store["rows"]["oldReplay01"] = {"title": "旧", "board": None}
    resp = env.client.get("/solitaire/og/oldReplay01.png")
    assert resp.status_code == 200
    assert resp.data == _generic() and _png_size(resp.data) == (1200, 630)
    assert fake_fetch == []


def test_og_route_corrupt_board_returns_generic_image(env):
    _seed(env, "corrupt0001", board={"v": 9})
    resp = env.client.get("/solitaire/og/corrupt0001.png")
    assert resp.data == _generic()


def test_og_route_missing_replay_returns_generic_with_short_cache(env):
    resp = env.client.get("/solitaire/og/nothingHere1.png")
    assert resp.status_code == 200
    assert resp.data == _generic()
    assert "max-age=300" in resp.headers["Cache-Control"]


@pytest.mark.parametrize("bad_id", ["a", "../etc/passwd", "ab cd efg", "x" * 40, "id.with.dots1", "あいうえおかきく",
                                    "abcDEF123_-%0A"])
def test_og_route_invalid_id_is_404(env, bad_id):
    assert env.client.get(f"/solitaire/og/{bad_id}.png").status_code == 404


def test_replay_id_check_rejects_trailing_newline(env):
    assert env.og.is_valid_replay_id("abcDEF123_-")
    assert not env.og.is_valid_replay_id("abcDEF123_-\n")


def test_og_route_disabled_returns_404(monkeypatch, env):
    monkeypatch.setattr(env.routes, "ENABLE_VISUAL_SOLO_PLAY", False)
    assert env.client.get("/solitaire/og/abcDEF123_-.png").status_code == 404


# ── 不完全な描画・負荷対策 ──

def test_degraded_render_is_cached_in_memory_not_on_disk(env, monkeypatch):
    calls = []

    def failing(name, url):
        calls.append(name)
        return None, True

    monkeypatch.setattr(env.og, "_fetch_card_image", failing)
    _seed(env, "degraded001")
    r1 = env.client.get("/solitaire/og/degraded001.png")
    assert r1.status_code == 200 and _png_size(r1.data) == (1200, 630)   # 取得失敗でも枠で代用して描画できる
    assert "max-age=60" in r1.headers["Cache-Control"]
    assert env.og.read_cached("degraded001") is None                      # ディスクには固定しない
    n = len(calls)
    assert n > 0
    env.sb.store["rows"].clear()
    r2 = env.client.get("/solitaire/og/degraded001.png")                  # 数分はメモリから返る（再取得・DB参照なし）
    assert r2.data == r1.data and len(calls) == n and "max-age=60" in r2.headers["Cache-Control"]


def test_degraded_memory_cache_expires(env, monkeypatch):
    monkeypatch.setattr(env.og, "_fetch_card_image", lambda name, url: (None, True))
    _seed(env, "degraded002")
    env.client.get("/solitaire/og/degraded002.png")
    assert env.og.read_any_cached("degraded002") is not None
    env.og._degraded_cache["degraded002"] = (0.0, b"x")   # 期限切れの状態
    assert env.og.read_any_cached("degraded002") is None


def test_render_budget_is_process_wide_not_per_ip(env, fake_fetch, monkeypatch):
    """描画の予算はプロセス全体。IP（X-Forwarded-For）を変えても同じ予算を消費し、超えたら汎用画像・60秒。"""
    monkeypatch.setattr(env.og, "RENDER_BUDGET_MAX", 2)
    for i in range(4):
        _seed(env, f"rate000000{i}")
    results = []
    for i in range(4):
        r = env.client.get(f"/solitaire/og/rate000000{i}.png", headers={"X-Forwarded-For": f"10.0.0.{i}"})
        results.append((r.status_code, r.data == _generic(), "max-age=60" in r.headers["Cache-Control"]))
    assert results == [(200, False, False), (200, False, False), (200, True, True), (200, True, True)]
    # キャッシュ済みの id は予算を消費せず返る
    assert env.client.get("/solitaire/og/rate0000000.png").data != _generic()


def test_budget_is_consumed_only_when_actually_rendering(env, fake_fetch):
    """board 無し・存在しない id・キャッシュ命中では予算を消費しない。"""
    _seed(env, "hasBoard001")
    env.sb.store["rows"]["oldReplay01"] = {"title": "旧", "board": None}
    for _ in range(5):
        env.client.get("/solitaire/og/oldReplay01.png")
        env.client.get("/solitaire/og/nothingHere1.png")
    assert env.og._budget_log == []
    env.client.get("/solitaire/og/hasBoard001.png")
    assert len(env.og._budget_log) == 1
    env.client.get("/solitaire/og/hasBoard001.png")   # キャッシュ命中
    assert len(env.og._budget_log) == 1


def test_render_semaphore_timeout_returns_generic_image(env, fake_fetch, monkeypatch):
    monkeypatch.setattr(env.og, "RENDER_WAIT_SEC", 0.05)
    _seed(env, "busy0000001")
    held = [env.og._render_sem.acquire(), env.og._render_sem.acquire()]   # 描画枠2つを塞ぐ
    try:
        r = env.client.get("/solitaire/og/busy0000001.png")
    finally:
        for _ in held:
            env.og._render_sem.release()
    assert r.status_code == 200 and r.data == _generic()
    assert "max-age=60" in r.headers["Cache-Control"]
    assert fake_fetch == []   # 枠が取れない間は画像取得もしない
    assert env.og._budget_log == []   # 描画していないので予算も消費しない


def test_cache_write_failure_removes_temp_file(env, fake_fetch, monkeypatch):
    import os as _os

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(_os, "replace", boom)
    png, degraded = env.og.get_or_render("tmpcheck0001", validate_board(_board()), "t")
    assert _png_size(png) == (1200, 630) and not degraded
    assert [f for f in _os.listdir(env.og._OG_CACHE_DIR) if f.endswith(".tmp")] == []


def test_render_handles_empty_and_extreme_boards(env, fake_fetch):
    full = _board(field=[{"slot": f"custom-slot-{i}", "name": "あ" * 100, "pos": "def", "face": "up", "n": 3}
                         for i in range(1, 15)],
                  hand=[{"name": "手"}] * 60, moves=0)
    png, _ = env.og.render_replay_og(validate_board(full), "長" * 100)
    assert _png_size(png) == (1200, 630)
    empty = _board(field=[], hand=[], grave={"count": 0, "cards": []})
    png, _ = env.og.render_replay_og(validate_board(empty), "")
    assert _png_size(png) == (1200, 630)


# ── ページの og:image 出し分け ──

def _og(html, prop):
    import re
    m = re.search(rf'<meta (?:property|name)="{prop}" content="([^"]*)"', html)
    return m.group(1) if m else None


def test_page_default_uses_generic_page_image(env):
    html = env.client.get("/solitaire").get_data(as_text=True)
    assert "/static/og-solitaire.png" in _og(html, "og:image")
    assert "/static/og-solitaire.png" in _og(html, "twitter:image")
    assert _og(html, "og:title") == "一人回しシミュレーター | TCGYM"
    assert _og(html, "og:url").endswith("/solitaire")


def test_page_with_replay_uses_replay_image_title_and_url(env):
    _seed(env)
    html = env.client.get("/solitaire?replay=abcDEF123_-").get_data(as_text=True)
    img = _og(html, "og:image")
    assert img.endswith("/solitaire/og/abcDEF123_-.png") and img.startswith("http")
    assert _og(html, "twitter:image") == img
    assert _og(html, "og:title") == "閃刀姫 展開 | TCGYM シミュレーター"
    assert _og(html, "twitter:title") == "閃刀姫 展開 | TCGYM シミュレーター"
    assert _og(html, "og:url").endswith("/solitaire?replay=abcDEF123_-")


def test_page_title_is_html_escaped(env):
    _seed(env, title='"><script>alert(1)</script>')
    html = env.client.get("/solitaire?replay=abcDEF123_-").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html


def test_page_replay_without_board_uses_generic_image_but_keeps_title(env):
    env.sb.store["rows"]["oldReplay01"] = {"title": "", "board": None}
    html = env.client.get("/solitaire?replay=oldReplay01").get_data(as_text=True)
    assert "/static/og-solitaire.png" in _og(html, "og:image")
    assert _og(html, "og:title") == "共有リプレイ | TCGYM シミュレーター"


def test_page_replay_missing_or_invalid_id_uses_generic(env):
    html = env.client.get("/solitaire?replay=nothingHere1").get_data(as_text=True)
    assert "/static/og-solitaire.png" in _og(html, "og:image")
    html = env.client.get("/solitaire?replay=../../x").get_data(as_text=True)
    assert "/static/og-solitaire.png" in _og(html, "og:image")
    assert _og(html, "og:url").endswith("/solitaire")


def test_page_db_failure_points_to_og_route(env):
    env.sb.store["raise"] = True
    _seed(env)
    html = env.client.get("/solitaire?replay=abcDEF123_-").get_data(as_text=True)
    assert _og(html, "og:image").endswith("/solitaire/og/abcDEF123_-.png")
    assert _og(html, "og:title") == "一人回しシミュレーター | TCGYM"


def _patch_displays(monkeypatch, env, kinds, index_ok=True, unreleased_ok=True):
    import card_display
    monkeypatch.setattr(env.og, "_resolve_urls", env.real_resolve_urls)
    monkeypatch.setattr(card_display, "resolve_card_displays",
                        lambda names: {n: ({"kind": kinds.get(n, "none")} if kinds.get(n, "none") != "proxy"
                                           else {"kind": "proxy", "proxy": {}}) for n in names})
    monkeypatch.setattr(card_display, "released_index_available", lambda: index_ok)
    monkeypatch.setattr(card_display, "unreleased_data_available", lambda: unreleased_ok)


def test_unresolved_names_do_not_hit_external_search(env, monkeypatch):
    """card_display で画像URLが求まらない名前（none / proxy）は、店舗の名前検索にも画像取得にも出ず、枠で描く。"""
    import requests
    import scraper
    calls = []
    _patch_displays(monkeypatch, env, {"見つからないN": "none", "未発売P": "proxy"})
    monkeypatch.setattr(scraper, "kanabell_card_image_url", lambda n: calls.append(("kanabell", n)) or "https://x/y.png")
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(("get", a)) or None)
    board = _board(field=[{"slot": "custom-slot-5", "name": "見つからないN", "pos": "atk", "face": "up"}],
                   hand=[{"name": "未発売P"}], grave={"count": 0, "cards": []}, banish={"count": 0, "cards": []})
    _seed(env, board=board)
    r = env.client.get("/solitaire/og/abcDEF123_-.png")
    assert r.status_code == 200 and _png_size(r.data) == (1200, 630)
    assert calls == []
    assert "max-age=86400" in r.headers["Cache-Control"]   # 索引・辞書が使えていて見つからないだけなら正常な描画


@pytest.mark.parametrize("index_ok,unreleased_ok", [(False, True), (True, False), (False, False)])
def test_unavailable_index_or_dictionary_is_degraded_not_cached_on_disk(env, monkeypatch, index_ok, unreleased_ok):
    """名前の索引・未発売辞書が使えないとき、全カードが枠でも正常な描画としてディスクに固定しない。"""
    _patch_displays(monkeypatch, env, {}, index_ok=index_ok, unreleased_ok=unreleased_ok)   # 全て none
    _seed(env, "unavail0001")
    r = env.client.get("/solitaire/og/unavail0001.png")
    assert r.status_code == 200 and _png_size(r.data) == (1200, 630)
    assert "max-age=60" in r.headers["Cache-Control"]
    assert env.og.read_cached("unavail0001") is None                  # ディスクに固定しない
    assert env.og.read_any_cached("unavail0001") is not None          # 短期のメモリキャッシュのみ


def test_resolve_failure_is_degraded(env, monkeypatch):
    import card_display

    def boom(names):
        raise RuntimeError("down")

    monkeypatch.setattr(env.og, "_resolve_urls", env.real_resolve_urls)
    monkeypatch.setattr(card_display, "resolve_card_displays", boom)
    urls, unavailable = env.og._resolve_urls(["a", "b"])
    assert urls == {"a": None, "b": None} and unavailable is True


def test_found_names_with_unavailable_dictionary_are_not_flagged(env, monkeypatch):
    """全て解決できたなら、辞書が使えなくても（none が無いので）不完全扱いにしない。"""
    import card_display
    monkeypatch.setattr(env.og, "_resolve_urls", env.real_resolve_urls)
    monkeypatch.setattr(card_display, "resolve_card_displays",
                        lambda names: {n: {"kind": "image", "url": "https://u/x.png", "source": "released"} for n in names})
    monkeypatch.setattr(card_display, "released_index_available", lambda: False)
    monkeypatch.setattr(card_display, "unreleased_data_available", lambda: False)
    urls, unavailable = env.og._resolve_urls(["a"])
    assert urls == {"a": "https://u/x.png"} and unavailable is False


def test_card_display_availability_helpers(monkeypatch):
    import card_display
    monkeypatch.setattr(card_display, "_released_index_probe", None)
    assert card_display.released_index_available() is True
    card_display.register_released_index_probe(lambda: False)
    try:
        assert card_display.released_index_available() is False
    finally:
        monkeypatch.setattr(card_display, "_released_index_probe", None)

    def raiser(*, strict=False):
        raise card_display.DisplayDataUnavailable("x")

    monkeypatch.setattr(card_display, "_get_unreleased", raiser)
    assert card_display.unreleased_data_available() is False
    monkeypatch.setattr(card_display, "_get_unreleased", lambda *, strict=False: {})
    assert card_display.unreleased_data_available() is True


def test_legacy_top_board_renders_directly(env, fake_fetch):
    """検証を通さない旧形式（top のみ）の board を直接渡しても描ける（墓地の1枚だけ取りに行く）"""
    board = _board(grave={"count": 7, "top": {"name": "旧墓地"}}, banish={"count": 0, "top": None},
                   field=[], hand=[])
    png, degraded = env.og.render_replay_og(board, "旧")
    assert _png_size(png) == (1200, 630)
    assert fake_fetch == ["旧墓地"]


def test_pile_fetches_only_the_top_card(env, fake_fetch):
    """墓地・除外は最後の PILE_DRAW_MAX 枚を描くが、画像を取りに行くのは一番上の1枚だけ（下は名前の帯）"""
    n = env.og.PILE_DRAW_MAX
    cards = [{"name": f"墓{i}"} for i in range(n + 3)]
    board = _board(grave={"count": 40, "cards": cards}, banish={"count": 0, "cards": []}, field=[], hand=[])
    png, _ = env.og.render_replay_og(board, "t")
    assert _png_size(png) == (1200, 630)
    assert fake_fetch == [cards[-1]["name"]]


def test_fetch_count_upper_bound_is_26(env, fake_fetch):
    """外部へ取りに行く画像は、フィールド14＋手札10＋墓地・除外の一番上2＝26枚以下"""
    field = [{"slot": f"custom-slot-{i}", "name": f"場{i}", "pos": "atk", "face": "up"} for i in range(1, 15)]
    pile = lambda t: {"count": 9, "cards": [{"name": f"{t}{i}"} for i in range(4)]}
    board = _board(field=field, hand=[{"name": f"手{i}"} for i in range(30)], grave=pile("墓"), banish=pile("除"))
    png, _ = env.og.render_replay_og(board, "t")
    assert _png_size(png) == (1200, 630)
    assert len(fake_fetch) == 26 and len(set(fake_fetch)) == 26


def test_cache_dir_is_versioned():
    """見た目を変えたら古い画像を使わないよう、ディスクキャッシュのディレクトリ名にバージョンを入れている"""
    import replay_og_image
    assert os.path.basename(replay_og_image._OG_CACHE_DIR) == "cardprice_replay_og_v2"


def test_zone_icons_exist():
    import replay_og_image as og
    for name in ("grave", "banish", "field", "pendulum"):
        assert (REPO_ROOT / "static" / "og" / f"zone-{name}.png").is_file()
        assert og._load_zone_icon(name, 32) is not None


def test_missing_icons_make_render_degraded_and_are_not_remembered(env, fake_fetch, monkeypatch, tmp_path):
    """アイコンを読めない描画は degraded（ディスクに固定しない）。読めなかった下地は覚えず、読めたら通常に戻る"""
    og = env.og
    monkeypatch.setattr(og, "_base_cache", None)
    monkeypatch.setattr(og, "_ICON_DIR", str(tmp_path / "none"))
    board = _board(field=[], hand=[])
    _, degraded = og.render_replay_og(board, "t")
    assert degraded is True and og._base_cache is None
    monkeypatch.setattr(og, "_ICON_DIR", os.path.join(os.path.dirname(og.__file__), "static", "og"))
    _, degraded = og.render_replay_og(board, "t")
    assert degraded is False and og._base_cache is not None


def test_base_layer_is_built_once(env, fake_fetch, monkeypatch):
    """盤面によらない下地は1回だけ作って使い回す（描画ごとに大きな画像を作らない）"""
    og = env.og
    monkeypatch.setattr(og, "_base_cache", None)
    calls = []
    real = og._build_base
    monkeypatch.setattr(og, "_build_base", lambda: calls.append(1) or real())
    for _ in range(3):
        og.render_replay_og(_board(), "t")
    assert len(calls) == 1
