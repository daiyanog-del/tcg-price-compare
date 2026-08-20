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
