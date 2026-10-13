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


def test_index_mentions_mercari_ambassador():
    """メルカリアンバサダー利用の明示があり、第三者提供の文と併存すること。"""
    html = _client().get("/").get_data(as_text=True)
    assert "メルカリアンバサダー（株式会社メルカリ）" in html
    assert "上記のアフィリエイトASP以外の第三者に提供することはありません" in html
    # 画面の表記（メルカリ欄の見出しの「PR」の印）と一致していること
    assert "メルカリ欄のリンク（「PR」と表示しています）" in html
    assert "メルカリで見る（PR）" not in html



def test_index_mentions_surugaya_affiliate():
    """駿河屋アフィリエイト利用とPR表示の明示があること。"""
    html = _client().get("/").get_data(as_text=True)
    assert "駿河屋アフィリエイトプログラム（株式会社駿河屋）" in html
    assert "「PR」と表示しています" in html
