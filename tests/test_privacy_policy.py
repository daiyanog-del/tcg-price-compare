"""
tests/test_privacy_policy.py — プライバシーポリシー（アクセス解析明示）の担保テスト

検証方針:
  トップページのプライバシーポリシーモーダル（templates/index.html #privacyOverlay）に
  Google Analytics 4 利用の明示が含まれていること、旧文言（アクセス解析ツール未使用）が
  もう残っていないことを HTML レベルで担保する。
"""

import sys
from pathlib import Path

# リポジトリルートを import パスに追加（conftest.py 不在のため各テストで挿入）
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import app as app_module


def _client():
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_index_mentions_google_analytics():
    """トップページのHTMLに Google Analytics（アクセス解析の明示）が含まれること。"""
    client = _client()
    html = client.get("/").get_data(as_text=True)
    assert "Google Analytics" in html


def test_index_does_not_claim_no_analytics():
    """旧文言（アクセス解析ツール未使用）が残っていないこと。"""
    client = _client()
    html = client.get("/").get_data(as_text=True)
    assert "アクセス解析ツールを使用していません" not in html
