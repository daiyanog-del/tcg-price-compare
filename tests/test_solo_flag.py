"""
tests/test_solo_flag.py — 一人回し(solo play) kill-switch の担保テスト

検証方針:
  feature flag ENABLE_VISUAL_SOLO_PLAY は import 時に os.environ から評価されるため、
  monkeypatch で env をセットしてから solitaire_routes → app を importlib.reload して
  ON/OFF 両状態の Flask アプリを作り、test_client で挙動を検証する。

  - OFF: 一人回しの導線・ルートだけが止まり、相場等のコア機能は無傷であること
         （コア route が flag によって 404 化されていないこと）を担保する。
  - ON : 従来通り一人回しが表示されること。
"""

import importlib
import sys
from pathlib import Path

import pytest

# リポジトリルートを import パスに追加（conftest.py 不在のため各テストで挿入）
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_app(monkeypatch, flag_value: str):
    """env に ENABLE_VISUAL_SOLO_PLAY をセットし、関連モジュールを再ロードして
    Flask test_client を返す。flag は import 時評価のため reload が必須。"""
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", flag_value)
    # 依存順に reload（app は solitaire_routes を import するため先に solitaire_routes）
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    return app_module, app_module.app.test_client()


# OFF でも url_map に残っているべきコア機能の route 前方一致（= flag で除去されていない）
CORE_RULE_PREFIXES = ["/api/search", "/api/deck", "/card/", "/api/price-history"]

# OFF でも正常応答すべきコア route（外部依存なしで叩けるもの）。
# 注: /api/search は DB 非存在カードに対し正当に 404 を返すためライブ検証から除外。
CORE_LIVE_ROUTES = [
    ("GET", "/"),
    ("GET", "/card/test"),
    ("GET", "/api/price-history?card=test"),
]


# ──────────────────────────────────────────────
# OFF 時: 一人回しは停止、コアは無傷
# ──────────────────────────────────────────────

class TestSoloDisabled:
    def test_solitaire_page_returns_503_with_notice(self, monkeypatch):
        _, client = _load_app(monkeypatch, "0")
        resp = client.get("/solitaire")
        assert resp.status_code == 503
        # 案内文が含まれること（solo_disabled.html）
        assert "ご利用いただけません" in resp.get_data(as_text=True)

    def test_replay_get_returns_404(self, monkeypatch):
        _, client = _load_app(monkeypatch, "0")
        assert client.get("/api/solitaire/replay/dummyid").status_code == 404

    def test_replay_save_returns_404(self, monkeypatch):
        _, client = _load_app(monkeypatch, "0")
        assert client.post("/api/solitaire/replay", json={"logs": [1]}).status_code == 404

    def test_nav_link_hidden_on_index(self, monkeypatch):
        _, client = _load_app(monkeypatch, "0")
        html = client.get("/").get_data(as_text=True)
        assert 'href="/solitaire"' not in html

    def test_core_routes_registered(self, monkeypatch):
        """コア route が flag によって url_map から除去されていないこと。"""
        app_module, _ = _load_app(monkeypatch, "0")
        rules = [r.rule for r in app_module.app.url_map.iter_rules()]
        for prefix in CORE_RULE_PREFIXES:
            assert any(r.startswith(prefix) for r in rules), \
                f"コア route {prefix} が url_map から消えている（コアが壊れた）"

    @pytest.mark.parametrize("method,path", CORE_LIVE_ROUTES)
    def test_core_routes_respond(self, monkeypatch, method, path):
        """外部依存なしで叩けるコア route が flag OFF でも 404 化されていないこと。
        外部依存が無い環境では 500/503 もあり得るが、kill-switch の影響（=404）は受けない。"""
        _, client = _load_app(monkeypatch, "0")
        resp = client.open(path, method=method)
        assert resp.status_code != 404, f"{method} {path} が 404 になっている（コアが壊れた）"


# ──────────────────────────────────────────────
# ON 時: 一人回しは従来通り表示
# ──────────────────────────────────────────────

class TestSoloEnabled:
    def test_solitaire_page_returns_200(self, monkeypatch):
        _, client = _load_app(monkeypatch, "1")
        assert client.get("/solitaire").status_code == 200

    def test_nav_link_present_on_index(self, monkeypatch):
        _, client = _load_app(monkeypatch, "1")
        html = client.get("/").get_data(as_text=True)
        assert 'href="/solitaire"' in html
