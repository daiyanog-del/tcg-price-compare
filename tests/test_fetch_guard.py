"""
tests/test_fetch_guard.py — fetch_guard.py のホワイトリスト担保テスト

テスト方針:
  - ホワイトリスト違反URLは validate 段階で WhitelistViolation を送出するため、
    ネットワークには一切出ない。requests.Session.get をパッチして呼び出されないことも検証。
  - リダイレクト検証はモックレスポンスで確認。
  - AST検査: watch_unreleased.py / unreleased_extractor.py に直接 HTTP ライブラリの
    import がないことを構造保証する。
"""

import ast
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# テスト対象モジュール
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fetch_guard import (
    ALLOWED_HOSTS,
    ALLOWED_PATH_PREFIXES,
    WhitelistViolation,
    fetch_whitelisted,
    is_whitelisted,
)

# リポジトリルートのパス
REPO_ROOT = Path(__file__).resolve().parent.parent


# ──────────────────────────────────────────────
# is_whitelisted のテスト
# ──────────────────────────────────────────────

class TestIsWhitelisted:
    """is_whitelisted() の真偽値テスト"""

    def test_yu_gi_oh_jp_root(self):
        assert is_whitelisted("https://www.yu-gi-oh.jp/") is True

    def test_yu_gi_oh_jp_any_path(self):
        assert is_whitelisted("https://www.yu-gi-oh.jp/products/abc") is True

    def test_yu_gi_oh_jp_no_www(self):
        assert is_whitelisted("https://yu-gi-oh.jp/news/") is True

    def test_konami_yugioh_path(self):
        assert is_whitelisted("https://www.konami.com/yugioh/") is True

    def test_konami_yugioh_deep_path(self):
        assert is_whitelisted("https://www.konami.com/yugioh/tcg/") is True

    def test_konami_non_yugioh_path(self):
        # /yugioh/ 以外のパスは許可しない
        assert is_whitelisted("https://www.konami.com/") is False

    def test_konami_games_path(self):
        assert is_whitelisted("https://www.konami.com/games/") is False

    def test_yugioh_card_com_not_whitelisted(self):
        # yugioh-card.com はWatcherの対象外（抽出対象は yu-gi-oh.jp。価格収集系とは独立）
        assert is_whitelisted("https://www.yugioh-card.com/japan/products/") is False
        assert is_whitelisted("https://www.yugioh-card.com/") is False

    def test_http_rejected(self):
        assert is_whitelisted("http://www.yu-gi-oh.jp/") is False

    def test_external_domain(self):
        assert is_whitelisted("https://example.com/") is False

    def test_google(self):
        assert is_whitelisted("https://www.google.com/") is False

    def test_subdomain_not_in_whitelist(self):
        assert is_whitelisted("https://blog.yu-gi-oh.jp/") is False


# ──────────────────────────────────────────────
# fetch_whitelisted のホワイトリスト違反テスト
# （違反URLでは Session.get が一切呼ばれないことも確認）
# ──────────────────────────────────────────────

class TestFetchWhitelistedViolation:
    """ホワイトリスト違反URLで WhitelistViolation が発生し、ネットワークに出ないことを検証"""

    def _assert_violation_without_network(self, url: str):
        """指定URLで WhitelistViolation が発生し Session.get が呼ばれないことを確認"""
        mock_get = MagicMock()
        with patch("requests.Session.get", mock_get):
            with pytest.raises(WhitelistViolation):
                fetch_whitelisted(url)
        # ネットワーク呼び出しが一切行われていないことを確認
        mock_get.assert_not_called()

    def test_external_domain_example(self):
        self._assert_violation_without_network("https://example.com/")

    def test_external_domain_google(self):
        self._assert_violation_without_network("https://www.google.com/")

    def test_http_scheme(self):
        self._assert_violation_without_network("http://www.yu-gi-oh.jp/")

    def test_konami_non_yugioh_path(self):
        self._assert_violation_without_network("https://www.konami.com/")

    def test_konami_games_path(self):
        self._assert_violation_without_network("https://www.konami.com/games/jp/ja/")

    def test_subdomain_not_whitelisted(self):
        self._assert_violation_without_network("https://sub.yu-gi-oh.jp/")

    def test_malicious_external(self):
        self._assert_violation_without_network("https://evil.example.jp/")


# ──────────────────────────────────────────────
# リダイレクト先検証テスト
# ──────────────────────────────────────────────

class TestFetchWhitelistedRedirect:
    """リダイレクト先URLもホワイトリスト検証されることを確認"""

    def _make_redirect_response(self, location: str, status: int = 302):
        """Locationヘッダ付きのモックレスポンスを生成"""
        mock_resp = MagicMock()
        mock_resp.status_code = status
        mock_resp.headers = {"Location": location}
        return mock_resp

    def test_redirect_to_external_raises_violation(self):
        """302リダイレクト先が外部ドメインの場合 WhitelistViolation"""
        mock_resp = self._make_redirect_response("https://evil.example.com/steal")

        with patch("requests.Session.get", return_value=mock_resp):
            with pytest.raises(WhitelistViolation):
                fetch_whitelisted("https://www.yu-gi-oh.jp/some-page")

    def test_redirect_to_http_raises_violation(self):
        """302リダイレクト先が http の場合 WhitelistViolation"""
        mock_resp = self._make_redirect_response("http://www.yu-gi-oh.jp/page")

        with patch("requests.Session.get", return_value=mock_resp):
            with pytest.raises(WhitelistViolation):
                fetch_whitelisted("https://www.yu-gi-oh.jp/some-page")

    def test_redirect_to_whitelisted_succeeds(self):
        """302リダイレクト先がホワイトリスト内であれば成功（最終レスポンスを返す）"""
        # 1回目: リダイレクトレスポンス
        redirect_resp = self._make_redirect_response("https://www.yu-gi-oh.jp/new-page")
        # 2回目: 正常レスポンス
        final_resp = MagicMock()
        final_resp.status_code = 200
        final_resp.headers = {}

        with patch("requests.Session.get", side_effect=[redirect_resp, final_resp]):
            # sleep を無効化してテストを高速化
            with patch("fetch_guard.time.sleep"):
                result = fetch_whitelisted("https://www.yu-gi-oh.jp/some-page")
        assert result.status_code == 200

    def test_redirect_hop_limit(self):
        """3ホップを超えるリダイレクトで WhitelistViolation"""
        # 全て同じホワイトリスト内URLにリダイレクトし続けるモック
        redirect_resp = self._make_redirect_response("https://www.yu-gi-oh.jp/redirect")
        redirect_resp.status_code = 301

        with patch("requests.Session.get", return_value=redirect_resp):
            with patch("fetch_guard.time.sleep"):
                with pytest.raises(WhitelistViolation, match="3ホップ"):
                    fetch_whitelisted("https://www.yu-gi-oh.jp/start")


# ──────────────────────────────────────────────
# AST検査: 対象ファイルに直接HTTPライブラリの import がないことを確認
# ──────────────────────────────────────────────

# 禁止 import モジュール名
# urllib.parse はURLパース専用（HTTP通信なし）のため許可。
# HTTP通信が可能な urllib.request / urllib.error は禁止する。
# Discord 通知は discord_notify モジュールに隔離済み（信頼済みWebhookへの一方向POST）。
_FORBIDDEN_HTTP_MODULES = {
    "requests", "urllib3", "httpx", "aiohttp",
    "urllib.request", "urllib.error",
}

# 検査対象ファイル
_AST_CHECK_FILES = [
    "watch_unreleased.py",
    "unreleased_extractor.py",
]


def _collect_imports(source: str) -> set[str]:
    """Pythonソースコードから import されるモジュール名を収集する。
    トップレベル名（"requests"）とドット付きフルパス（"urllib.request"）の両方を含める。
    関数内の遅延importも ast.walk で検出される。
    """
    tree = ast.parse(source)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)                  # "urllib.request"
                modules.add(alias.name.split(".")[0])    # "urllib"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
                modules.add(node.module.split(".")[0])
    return modules


class TestASTImportGuard:
    """watch_unreleased.py / unreleased_extractor.py に直接HTTPライブラリimportがないことをAST検査"""

    @pytest.mark.parametrize("filename", _AST_CHECK_FILES)
    def test_no_direct_http_import(self, filename: str):
        filepath = REPO_ROOT / filename

        # ファイルが存在しない場合はスキップ（実装前の段階での実行に対応）
        if not filepath.exists():
            pytest.skip(f"{filename} がまだ存在しません")

        source = filepath.read_text(encoding="utf-8")
        imported = _collect_imports(source)
        violations = imported & _FORBIDDEN_HTTP_MODULES

        assert not violations, (
            f"{filename} に禁止されたHTTPライブラリの直接importがあります: {violations}\n"
            f"  HTTPリクエストは fetch_guard.fetch_whitelisted() 経由のみ許可されています。"
        )


# ──────────────────────────────────────────────
# ユーティリティ関数の追加テスト
# ──────────────────────────────────────────────

class TestWhitelistConstants:
    """ALLOWED_HOSTS / ALLOWED_PATH_PREFIXES の定義確認"""

    def test_allowed_hosts_is_frozenset(self):
        assert isinstance(ALLOWED_HOSTS, frozenset)

    def test_required_hosts_present(self):
        assert "www.yu-gi-oh.jp" in ALLOWED_HOSTS
        assert "yu-gi-oh.jp" in ALLOWED_HOSTS
        assert "www.konami.com" in ALLOWED_HOSTS

    def test_unrelated_hosts_absent(self):
        # ホワイトリストは仕様の初期値（yu-gi-oh.jp ＋ konami.com）のみ
        assert "www.yugioh-card.com" not in ALLOWED_HOSTS

    def test_konami_path_prefix_defined(self):
        assert "www.konami.com" in ALLOWED_PATH_PREFIXES
        prefixes = ALLOWED_PATH_PREFIXES["www.konami.com"]
        assert "/yugioh/" in prefixes

    def test_yu_gi_oh_jp_no_path_restriction(self):
        # yu-gi-oh.jp にはパス制限がないこと
        assert "www.yu-gi-oh.jp" not in ALLOWED_PATH_PREFIXES
        assert "yu-gi-oh.jp" not in ALLOWED_PATH_PREFIXES
