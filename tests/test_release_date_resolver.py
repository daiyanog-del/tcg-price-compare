"""公式発売日照合の境界を、通信なしで検証する。"""

import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest
import requests

import release_date_resolver as resolver


def index_row(name="IMMORTAL PHOENIX", path="imph", day="2026年10月31日(土)", number=1):
    return f'p[{number}]=' + json.dumps({"title": name, "url": path, "release-date": day}, ensure_ascii=False) + ";"


def detail(name="IMMORTAL PHOENIX", day="2026年10月31日(土) 発売"):
    return f'<header><h1>遊戯王OCGデュエルモンスターズ {name}</h1><p class="release-date">{day}</p></header>'


@pytest.fixture(autouse=True)
def empty_cache():
    resolver._cache.clear()
    yield
    resolver._cache.clear()


def mock_pages(monkeypatch, index=None, page=None):
    fetch = MagicMock(side_effect=lambda url: (index if index is not None else index_row())
                     if url == resolver.PRODUCTS_URL else (page if page is not None else detail()))
    monkeypatch.setattr(resolver, "_fetch_html", fetch)
    return fetch


def test_verified_exact_product_ignores_other_products(monkeypatch):
    mock_pages(monkeypatch, index_row() + index_row("別の商品", "other", "2027年1月23日", 2),
               detail("別の商品", "2027年1月23日") + detail())
    result = resolver.resolve_release_date("IMMORTAL PHOENIX", source_text="2025年10月31日発売")
    assert result["status"] == "verified"
    assert result["release_date"] == "2026-10-31"
    assert result["source_url"].endswith("/imph/")
    assert result["checked_at"] and "2026年10月31日" in result["evidence"]


@pytest.mark.parametrize("name", ["PHOENIX", "IMMORTAL PHOENIX 特典", "IMMORTALPHOENIX", ""])
def test_names_do_not_match_substrings(monkeypatch, name):
    mock_pages(monkeypatch)
    assert resolver.resolve_release_date(name)["release_date"] is None


def test_limited_normalization(monkeypatch):
    mock_pages(monkeypatch, index_row("デッキ − テスト"), detail("デッキ-テスト"))
    assert resolver.resolve_release_date("デッキ　－ テスト")["status"] == "verified"


@pytest.mark.parametrize("page", [detail("別の商品"), detail().replace("release-date", "changed"),
    detail(day="2026年2月30日"), detail(day="10月31日"), detail(day="2026年10月下旬"),
    '<header><h1>IMMORTAL PHOENIX</h1><h1>別の商品</h1><p class="release-date">2026年10月31日</p></header>'])
def test_missing_or_invalid_detail_is_unverified(monkeypatch, page):
    mock_pages(monkeypatch, page=page)
    result = resolver.resolve_release_date("IMMORTAL PHOENIX")
    assert result["release_date"] is None
    assert result["status"] == "unverified"


@pytest.mark.parametrize("page", [detail(day="2026年11月1日"), detail() + detail(day="2026年11月1日"),
                                 detail(day="2026年10月31日 または 2026年11月1日")])
def test_conflicting_dates(monkeypatch, page):
    mock_pages(monkeypatch, page=page)
    result = resolver.resolve_release_date("IMMORTAL PHOENIX")
    assert result["release_date"] is None
    assert result["status"] == "conflict"


@pytest.mark.parametrize("index", ["構造変更", index_row(path=""), index_row(path="#modal"),
    index_row() + index_row(path="different", number=2), index_row(day="2026年2月30日")])
def test_unresolvable_index(monkeypatch, index):
    mock_pages(monkeypatch, index=index)
    assert resolver.resolve_release_date("IMMORTAL PHOENIX")["status"] == "unverified"


@pytest.mark.parametrize("url", ["https://evil.example/test/", "http://www.yugioh-card.com/japan/products/imph/",
    "https://www.yugioh-card.com/japan/news/", "https://www.yugioh-card.com/japan/products/../news/",
    "https://user@www.yugioh-card.com/japan/products/imph/", "//evil.example/test/",
    "https://www.yugioh-card.com/japan/products/%2e%2e/news/", "imph?redirect=external"])
def test_index_cannot_cause_external_fetch(monkeypatch, url):
    fetch = mock_pages(monkeypatch, index=index_row(path=url))
    assert resolver.resolve_release_date("IMMORTAL PHOENIX")["status"] == "unverified"
    assert fetch.call_count == 1


def test_no_future_cutoff(monkeypatch):
    mock_pages(monkeypatch, index_row(day="2030年10月31日"), detail(day="2030年10月31日"))
    assert resolver.resolve_release_date("IMMORTAL PHOENIX")["release_date"] == "2030-10-31"


def test_failure_logged_and_not_cached(monkeypatch, caplog):
    fetch = MagicMock(side_effect=requests.Timeout("タイムアウト"))
    monkeypatch.setattr(resolver, "_fetch_html", fetch)
    for _ in range(2):
        assert resolver.resolve_release_date("IMMORTAL PHOENIX")["status"] == "unverified"
    assert fetch.call_count == 2
    assert "公式発売日照合失敗" in caplog.text


def test_cache_is_shared_for_concurrent_requests_and_expires(monkeypatch):
    fetch = mock_pages(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(resolver.resolve_release_date, ["IMMORTAL PHOENIX"] * 8))
    assert all(result["status"] == "verified" for result in results)
    assert fetch.call_count == 2
    assert len({r["checked_at"] for r in results}) == 1
    for url, (_, html, checked_at) in list(resolver._cache.items()):
        resolver._cache[url] = (-resolver._CACHE_TTL, html, checked_at)
    resolver.resolve_release_date("IMMORTAL PHOENIX")
    assert fetch.call_count == 4


def response_mock(status=200, headers=None, content=b"ok"):
    response = MagicMock(status_code=status, headers=headers or {})
    response.__enter__.return_value = response
    response.iter_content.return_value = [content]
    return response


def test_redirect_is_validated_before_next_request(monkeypatch):
    get = MagicMock(return_value=response_mock(302, {"Location": "https://evil.example/"}))
    monkeypatch.setattr(resolver.requests, "get", get)
    with pytest.raises(ValueError, match="公式商品ページ以外"):
        resolver._fetch_html(resolver.PRODUCTS_URL)
    assert get.call_count == 1
    assert get.call_args.kwargs["allow_redirects"] is False


def test_size_limit(monkeypatch):
    get = MagicMock(return_value=response_mock(content=b"x" * (resolver._MAX_BYTES + 1)))
    monkeypatch.setattr(resolver.requests, "get", get)
    with pytest.raises(ValueError, match="サイズ上限"):
        resolver._fetch_html(resolver.PRODUCTS_URL)
