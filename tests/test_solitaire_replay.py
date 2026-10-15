"""
tests/test_solitaire_replay.py — POST /api/solitaire/replay の保存ガードの検証（2026-08-20）

検証方針:
  test_solo_flag.py と同じ手法（env経由でモジュールreload）でアプリを構築し、
  Supabaseクライアントをフェイクに差し替えて正常系・異常系を確認する。
  - 正常系: 60枚相当のimages・300手のlogsで200・idが返る
  - 異常系: logsが配列でない=400 / logsが2000件超=400 / 同一IPからの11回目=429
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeTable:
    def insert(self, payload):
        return self

    def execute(self):
        return None


class _FakeSupabase:
    def table(self, name):
        return _FakeTable()


def _load_app(monkeypatch):
    """一人回しON・フェイクSupabase接続済みの状態でアプリを再構築する。
    レートリミットのメモリ状態もモジュールreloadでテストごとにリセットされる。"""
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", "1")
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    app_module._supabase_client = _FakeSupabase()
    return app_module, app_module.app.test_client()


def test_replay_save_success_with_large_payload(monkeypatch):
    """60枚相当のimages・300手のlogsを持つ現実的なサイズのリプレイが保存できる"""
    _, client = _load_app(monkeypatch)
    logs = [{"type": "action", "detail": f"step-{i}"} for i in range(300)]
    images = {str(i): f"https://example.com/{i}.png" for i in range(60)}
    names = {str(i): f"カード{i}" for i in range(60)}

    resp = client.post("/api/solitaire/replay", json={
        "logs": logs, "images": images, "names": names,
        "exCardIds": ["1", "2"], "title": "テストリプレイ",
    })

    assert resp.status_code == 200
    assert "id" in resp.get_json()


def test_replay_save_logs_not_list_returns_400(monkeypatch):
    _, client = _load_app(monkeypatch)
    resp = client.post("/api/solitaire/replay", json={"logs": "not-a-list"})
    assert resp.status_code == 400


def test_replay_save_too_many_logs_returns_400(monkeypatch):
    _, client = _load_app(monkeypatch)
    resp = client.post("/api/solitaire/replay", json={"logs": list(range(2001))})
    assert resp.status_code == 400


def test_replay_save_rate_limit_blocks_eleventh_request(monkeypatch):
    """60秒10回の枠を使い切った次（11回目）は429になる"""
    _, client = _load_app(monkeypatch)
    for i in range(10):
        resp = client.post("/api/solitaire/replay", json={"logs": [1]})
        assert resp.status_code == 200, f"{i+1}回目で失敗: {resp.get_json()}"

    resp = client.post("/api/solitaire/replay", json={"logs": [1]})
    assert resp.status_code == 429


# ── カード種類（cardTypes）の保存・取得（省略可の項目） ──

class _RecordingTable:
    """insert の内容を残すフェイク"""

    def __init__(self, log, row=None):
        self.log, self.row = log, row
        self._select = None

    def insert(self, payload):
        self._payload = payload
        self._op = "insert"
        return self

    def select(self, cols):
        self._select = cols
        self._op = "select"
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        if self._op == "insert":
            self.log.append(self._payload)
            return None

        class _R:
            data = [self.row]
        return _R()


def _load_app_with(monkeypatch, table_factory):
    app_module, client = _load_app(monkeypatch)

    class _Sb:
        def table(self, name):
            return table_factory()
    app_module._supabase_client = _Sb()
    return client


def test_replay_save_keeps_valid_card_types_only(monkeypatch):
    """cardTypes は images にあるカードの monster/spell/trap だけ保存し、それ以外は捨てる"""
    log = []
    client = _load_app_with(monkeypatch, lambda: _RecordingTable(log))
    resp = client.post("/api/solitaire/replay", json={
        "logs": [1], "images": {"a": "x", "b": "y"},
        "cardTypes": {"a": "monster", "b": "bogus", "zzz": "spell"},
    })
    assert resp.status_code == 200
    assert log[0]["card_types"] == {"a": "monster"}


def test_replay_save_without_card_types_is_unchanged(monkeypatch):
    """cardTypes を送らない旧クライアントの保存は、card_types 列を含めない"""
    log = []
    client = _load_app_with(monkeypatch, lambda: _RecordingTable(log))
    resp = client.post("/api/solitaire/replay", json={"logs": [1], "images": {"a": "x"}})
    assert resp.status_code == 200
    assert "card_types" not in log[0]


def test_replay_save_card_types_not_dict_returns_400(monkeypatch):
    client = _load_app_with(monkeypatch, lambda: _RecordingTable([]))
    resp = client.post("/api/solitaire/replay", json={"logs": [1], "cardTypes": ["monster"]})
    assert resp.status_code == 400


def test_replay_get_returns_card_types(monkeypatch):
    row = {"images": {"a": "x"}, "names": {}, "ex_card_ids": [], "logs": [{}], "title": "t",
           "card_types": {"a": "spell"}}
    client = _load_app_with(monkeypatch, lambda: _RecordingTable([], row=row))
    body = client.get("/api/solitaire/replay/abc").get_json()
    assert body["cardTypes"] == {"a": "spell"}


# ── リプレイ一覧ページ（GET /solitaire/replays） ──

def test_replays_page_enabled_returns_200(monkeypatch):
    """一人回しが有効なら一覧ページは200で、検索対象外(noindex)・専用JSを読む"""
    _, client = _load_app(monkeypatch)
    resp = client.get("/solitaire/replays")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'content="noindex"' in html
    assert "replays-page.js" in html
    assert 'href="/solitaire"' in html  # シミュレーターに戻るリンク


def test_replays_page_disabled_returns_503(monkeypatch):
    """kill-switch(ENABLE_VISUAL_SOLO_PLAY=0)では /solitaire と同じく案内ページを503で返す"""
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", "0")
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    try:
        resp = app_module.app.test_client().get("/solitaire/replays")
        assert resp.status_code == 503
        assert "ご利用いただけません" in resp.get_data(as_text=True)
    finally:
        # 後続のテストに無効状態を持ち越さない（フラグは import 時評価のため ON に戻して reload）
        monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", "1")
        importlib.reload(solitaire_routes)
        importlib.reload(app_module)
