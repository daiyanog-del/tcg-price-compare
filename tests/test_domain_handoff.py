"""
tests/test_domain_handoff.py — 独自ドメイン移行の振り分け（domain_handoff.py・site_urls.py）

設計文書: docs/design-domain-migration-2026-10-21.md
  - CANONICAL_BASE_URL 未設定のあいだは何も変わらない（切り替え前の本番）
  - 設定後、旧URLの画面は引き継ぎページになり、API・静的ファイル・/sync はそのまま
  - 新ドメインの画面はそのまま。canonical・sitemap・robots は新ドメインを指す
  - X 投稿・通知の URL は環境変数で切り替わる
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402
import site_urls  # noqa: E402

LEGACY = "https://tcg-price-compare.onrender.com"
NEW = "https://greedy-lab.com"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def switched(monkeypatch):
    monkeypatch.setenv("CANONICAL_BASE_URL", NEW)
    monkeypatch.delenv("LEGACY_HOSTS", raising=False)


def _get(client, base, path, **kw):
    return client.get(path, base_url=base, **kw)


# ── 切り替え前 ──

def test_before_switch_legacy_pages_unchanged(client, monkeypatch):
    monkeypatch.delenv("CANONICAL_BASE_URL", raising=False)
    res = _get(client, LEGACY, "/new-cards")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "handoffConfig" not in body
    assert f'<link rel="canonical" href="{LEGACY}/new-cards">' in body


def test_before_switch_site_url_is_legacy(monkeypatch):
    monkeypatch.delenv("CANONICAL_BASE_URL", raising=False)
    assert site_urls.site_url() == LEGACY


# ── 切り替え後: 旧URL ──

@pytest.mark.parametrize("path", ["/", "/new-cards", "/featured", "/solitaire", "/solitaire/replays", "/admin",
                                  "/share-target", "/card/by-cid/1",
                                  "/card/%E7%81%B0%E6%B5%81%E3%81%86%E3%82%89%E3%82%89", "/buy/x"])
def test_legacy_pages_serve_handoff(client, switched, path):
    res = _get(client, LEGACY, path)
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert 'data-mode="send"' in body
    assert f'data-canonical-base="{NEW}"' in body
    # 引き継ぎページは保存領域の中身を # で運ぶので GA を載せない・保存させない
    assert "googletagmanager" not in body
    assert res.headers["Cache-Control"] == "no-store"
    # 旧URLの Search Console の所有確認と、共有リンクのプレビュー
    assert 'name="google-site-verification"' in body
    assert f'property="og:image" content="{NEW}/static/og-image.png' in body


def test_legacy_handoff_head_and_host_case(client, switched):
    head = client.head("/", base_url=LEGACY)
    assert head.status_code == 200 and head.headers["Cache-Control"] == "no-store"
    body = _get(client, "https://TCG-PRICE-COMPARE.onrender.com", "/").get_data(as_text=True)
    assert 'data-mode="send"' in body


def test_legacy_card_page_og_title(client, switched):
    body = _get(client, LEGACY, "/card/%E7%81%B0%E6%B5%81%E3%81%86%E3%82%89%E3%82%89").get_data(as_text=True)
    assert '<meta property="og:title" content="灰流うらら の価格比較 | 強欲なラボ">' in body
    assert f'<link rel="canonical" href="{NEW}/card/%E7%81%B0%E6%B5%81%E3%81%86%E3%82%89%E3%82%89">' in body


def test_legacy_handoff_keeps_query_for_extension_import(client, switched):
    res = _get(client, LEGACY, "/?tcgym_import=1")
    body = res.get_data(as_text=True)
    assert 'data-target-path="/?tcgym_import=1"' in body
    assert f'<link rel="canonical" href="{NEW}/">' in body


@pytest.mark.parametrize("path", ["/robots.txt", "/sync", "/static/shared/domain-handoff.js"])
def test_legacy_non_page_paths_pass_through(client, switched, path):
    res = _get(client, LEGACY, path)
    assert res.status_code == 200
    assert "handoffConfig" not in res.get_data(as_text=True) or path == "/static/shared/domain-handoff.js"


def test_legacy_api_passes_through(client, switched):
    res = client.post("/api/sync/link/preview", json={}, base_url=LEGACY)
    # 引き継ぎページ（200 HTML）ではなく、API 本来の応答（token 無しは 400）
    assert res.status_code == 400
    assert res.is_json


def test_legacy_post_is_not_handed_off(client, switched):
    res = client.post("/", base_url=LEGACY)
    assert "handoffConfig" not in res.get_data(as_text=True)


# ── 切り替え後: 新ドメイン ──

def test_new_domain_pages_are_normal(client, switched):
    res = _get(client, NEW, "/new-cards")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert "handoffConfig" not in body
    assert f'<link rel="canonical" href="{NEW}/new-cards">' in body


def test_receive_page_lists_legacy_origin(client, switched):
    res = _get(client, NEW, "/handoff?next=/")
    body = res.get_data(as_text=True)
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert 'data-mode="receive"' in body
    assert f'data-legacy-origins="{LEGACY}"' in body
    assert 'noindex' in body
    assert "googletagmanager" not in body


def test_receive_page_only_on_canonical_host(client, switched):
    # 旧URLの /handoff を返すと、旧URL内の細工リンクで referrer の判定をすり抜けられる
    assert _get(client, LEGACY, "/handoff").status_code == 404
    assert _get(client, "https://other.example", "/handoff").status_code == 404


def test_receive_page_absent_before_switch(client, monkeypatch):
    monkeypatch.delenv("CANONICAL_BASE_URL", raising=False)
    assert _get(client, LEGACY, "/handoff").status_code == 404
    assert _get(client, NEW, "/handoff").status_code == 404


def test_canonical_host_in_legacy_hosts_is_not_handed_off(client, monkeypatch):
    monkeypatch.setenv("CANONICAL_BASE_URL", NEW)
    monkeypatch.setenv("LEGACY_HOSTS", "tcg-price-compare.onrender.com,greedy-lab.com")
    assert "handoffConfig" not in _get(client, NEW, "/").get_data(as_text=True)


def test_robots_and_sitemap_point_to_new_domain_even_on_legacy(client, switched):
    robots = _get(client, LEGACY, "/robots.txt").get_data(as_text=True)
    assert f"Sitemap: {NEW}/sitemap.xml" in robots


def test_local_hosts_for_e2e(client, monkeypatch):
    # ローカルの結合テストでは localhost を旧、127.0.0.1 を新に見立てる
    monkeypatch.setenv("CANONICAL_BASE_URL", "http://127.0.0.1:5099")
    monkeypatch.setenv("LEGACY_HOSTS", "localhost:5099")
    body = _get(client, "http://localhost:5099", "/").get_data(as_text=True)
    assert 'data-canonical-base="http://127.0.0.1:5099"' in body
    recv = _get(client, "http://127.0.0.1:5099", "/handoff").get_data(as_text=True)
    assert 'data-legacy-origins="http://localhost:5099"' in recv


# ── 定期実行が載せる URL ──

def test_site_url_follows_env(monkeypatch):
    monkeypatch.setenv("CANONICAL_BASE_URL", NEW + "/")
    assert site_urls.site_url() == NEW
    monkeypatch.setenv("CANONICAL_BASE_URL", "")
    assert site_urls.site_url() == LEGACY


# ── 送る側・受け取る側の JS（tests/js/domain_handoff_check.js） ──

def test_domain_handoff_js():
    root = Path(__file__).resolve().parent.parent
    res = subprocess.run(['node', str(root / 'tests/js/domain_handoff_check.js')],
                         capture_output=True, text=True, encoding='utf-8', timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
