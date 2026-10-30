"""wiki_link: カード名から遊戯王カードWiki のURLを解決する（通信は偽物に差し替える）"""
from urllib.parse import quote

import pytest
import requests

import wiki_link
from wiki_link import page_url, resolve_wiki_url, search_url, to_wiki_title


class _Res:
    def __init__(self, status):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def close(self):
        pass


def _fake_get(existing, calls=None):
    """existing に入っているページ名（《》付き）だけ 200、ほかは 404 を返す get。"""
    def get(url, **kwargs):
        if calls is not None:
            calls.append(url)
        ok = any(url == f"{wiki_link.WIKI_BASE}?{quote(t)}" for t in existing)
        return _Res(200 if ok else 404)
    return get


@pytest.fixture(autouse=True)
def _clear_cache():
    wiki_link._cache.clear()
    yield
    wiki_link._cache.clear()


def test_to_wiki_title_fullwidth_alnum():
    assert to_wiki_title("EMスカイ・マジシャン") == "ＥＭスカイ・マジシャン"
    assert to_wiki_title("A・O・G リターンゼロ") == "Ａ・Ｏ・Ｇ リターンゼロ"
    # 記号と全角はそのまま
    assert to_wiki_title("RR－サテライト・キャノン・ファルコン") == "ＲＲ－サテライト・キャノン・ファルコン"
    assert to_wiki_title("灰流うらら") == "灰流うらら"


def test_page_found_with_fullwidth_title():
    get = _fake_get({"《Ｄ・ゲイザー》"})
    assert resolve_wiki_url("D・ゲイザー", get=get) == page_url("Ｄ・ゲイザー")


def test_page_found_with_original_title():
    # 英字のまま載っているカード
    get = _fake_get({"《Moonlight at Dawn》"})
    assert resolve_wiki_url("Moonlight at Dawn", get=get) == page_url("Moonlight at Dawn")


def test_missing_page_goes_to_title_search():
    get = _fake_get(set())
    url = resolve_wiki_url("ブンボーグゼロゼロファイブ", get=get)
    assert url == search_url("ブンボーグゼロゼロファイブ")
    assert "cmd=search" in url and "scope=title" in url


def test_network_error_goes_to_search_and_is_not_cached():
    def boom(url, **kwargs):
        raise requests.ConnectionError("down")
    assert resolve_wiki_url("灰流うらら", get=boom) == search_url("灰流うらら")
    # 次は通信できれば直接のページ
    assert resolve_wiki_url("灰流うらら", get=_fake_get({"《灰流うらら》"})) == page_url("灰流うらら")


def test_result_is_cached():
    calls = []
    get = _fake_get({"《灰流うらら》"}, calls)
    resolve_wiki_url("灰流うらら", get=get)
    resolve_wiki_url("灰流うらら", get=get)
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["", "   ", "あ" * 51])
def test_invalid_name(name):
    assert resolve_wiki_url(name, get=_fake_get(set())) is None
