"""
tests/test_error_handlers.py — エラーハンドラ（404/413/429/500）の分岐とキャッシュ制御の検証

検証方針:
  /api/ 配下はJSON、それ以外はHTML（トップへの戻りリンク付き）を返すこと、
  エラーレスポンスには常に Cache-Control: no-store が付くことを担保する。
  413/429/500 は実ルートで再現しづらいため、登録済みハンドラ関数を
  test_request_context 内で直接呼び出して分岐を検証する。
  あわせて、全応答共通のセキュリティヘッダ（X-Content-Type-Options /
  Referrer-Policy / X-Frame-Options）が付くことも担保する（2026-08-20 第2バッチ）。
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import app as app_module


def _client():
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_api_404_returns_json_and_no_store():
    client = _client()
    resp = client.get("/api/this-route-does-not-exist")
    assert resp.status_code == 404
    assert resp.get_json() == {"error": "not_found"}
    assert resp.headers.get("Cache-Control") == "no-store"


def test_non_api_404_returns_html_and_no_store():
    client = _client()
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404
    body = resp.get_data(as_text=True)
    assert "ページが見つかりません" in body
    assert "トップへ戻る" in body
    assert resp.headers.get("Cache-Control") == "no-store"


@pytest.mark.parametrize("status_code,json_error", [
    (413, "payload_too_large"),
    (429, "too_many_requests"),
    (500, "internal_error"),
])
def test_error_handler_json_branch_for_api_paths(status_code, json_error):
    handler = app_module._make_error_handler(status_code, json_error)
    with app_module.app.test_request_context(f"/api/whatever-{status_code}"):
        resp, code = handler(Exception("boom"))
        assert code == status_code
        assert resp.get_json() == {"error": json_error}


@pytest.mark.parametrize("status_code,json_error", [
    (413, "payload_too_large"),
    (429, "too_many_requests"),
    (500, "internal_error"),
])
def test_error_handler_html_branch_for_non_api_paths(status_code, json_error):
    handler = app_module._make_error_handler(status_code, json_error)
    with app_module.app.test_request_context(f"/some-page-{status_code}"):
        body, code = handler(Exception("boom"))
        assert code == status_code
        assert "トップへ戻る" in body


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "SAMEORIGIN",
}


@pytest.mark.parametrize("path", ["/", "/api/this-route-does-not-exist"])
def test_security_headers_present_on_all_responses(path):
    """主要3つのセキュリティヘッダが / と /api/ 応答の両方に付くこと。"""
    client = _client()
    resp = client.get(path)
    for name, value in _SECURITY_HEADERS.items():
        assert resp.headers.get(name) == value, f"{path} に {name} が期待値で付いていない"
