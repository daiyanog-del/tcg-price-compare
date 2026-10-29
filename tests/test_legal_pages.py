"""
tests/test_legal_pages.py — 利用規約・プライバシーポリシー（2026-10-29 改定）

  - /terms・/privacy の単独ページが開き、トップのモーダルと同じ本文を出す
  - 新しい規約は適用日（2026-11-12）を示し、それまで適用する旧版も読める
  - シミュレーター・リプレイ一覧から規約へのリンクがある
"""

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _client(monkeypatch):
    monkeypatch.setenv("ENABLE_VISUAL_SOLO_PLAY", "1")
    import solitaire_routes
    importlib.reload(solitaire_routes)
    import app as app_module
    importlib.reload(app_module)
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_terms_page_shows_new_terms_with_effective_date_and_previous_version(monkeypatch):
    html = _client(monkeypatch).get("/terms").get_data(as_text=True)
    assert "<title>利用規約 | 強欲なラボ</title>" in html
    assert "2026年11月12日" in html and "第9条（免責・責任の範囲）" in html
    assert "直接かつ通常の損害に限り" in html
    assert "いかなる損害についても、運営者は一切の責任を負いません" in html   # 旧版（11-11まで適用）も読める
    assert 'href="/privacy"' in html
    import re
    assert re.search(r'rel="canonical" href="[^"]+/terms"', html)


def test_privacy_page_reflects_actual_behavior(monkeypatch):
    html = _client(monkeypatch).get("/privacy").get_data(as_text=True)
    assert "<title>プライバシーポリシー | 強欲なラボ</title>" in html
    assert "元に戻せない形に変換した値" in html
    assert "Discord" in html
    assert "永続的な保存は行いません" not in html


def test_index_modals_use_the_same_bodies(monkeypatch):
    html = _client(monkeypatch).get("/").get_data(as_text=True)
    assert 'id="termsOverlay"' in html and "第13条（連絡先）" in html
    assert 'id="privacyOverlay"' in html and "元に戻せない形に変換した値" in html
    assert "css/legal.css" in html


def test_simulator_and_replays_pages_link_to_terms(monkeypatch):
    client = _client(monkeypatch)
    for path in ("/solitaire", "/solitaire/replays"):
        html = client.get(path).get_data(as_text=True)
        assert 'href="/terms"' in html and 'href="/privacy"' in html, path
