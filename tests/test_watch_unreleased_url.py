"""
tests/test_watch_unreleased_url.py — yu-gi-oh.jp 旧ニュースURL(news_detail.php)を
新形式(/news/oid-<id>/)へ正規化する _canonicalize_url() の単体テストと、
_add_new_links() が正規化後に重複を1件へ統合することの検証。

背景: 2026-09-01のサイトリニューアルで記事URL形式が変わり、
watched_pages の重複判定（URL文字列の完全一致）をすり抜けて同一記事が
旧URL・新URLの2行として登録されていた（本番で35組発生・手作業で移行済み）。
コード側の正規化を怠ると再発するため、この単体テストで固定する。

ネットワーク・実Supabaseは使用しない（スタブに差し替え）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from watch_unreleased import _canonicalize_url, _add_new_links, _is_article_page


def test_canonicalize_old_url_to_new_format():
    old = "https://yu-gi-oh.jp/news_detail.php?page=details&id=2602"
    assert _canonicalize_url(old) == "https://yu-gi-oh.jp/news/oid-2602/"


def test_canonicalize_query_order_independent():
    old = "https://yu-gi-oh.jp/news_detail.php?id=2602&page=details"
    assert _canonicalize_url(old) == "https://yu-gi-oh.jp/news/oid-2602/"


def test_canonicalize_www_host():
    old = "https://www.yu-gi-oh.jp/news_detail.php?page=details&id=2602"
    assert _canonicalize_url(old) == "https://yu-gi-oh.jp/news/oid-2602/"


def test_canonicalize_already_new_format_unchanged():
    new = "https://yu-gi-oh.jp/news/oid-2602/"
    assert _canonicalize_url(new) == new


def test_canonicalize_other_slug_unchanged():
    other = "https://yu-gi-oh.jp/news/43bhpy_m9y/"
    assert _canonicalize_url(other) == other


def test_canonicalize_missing_id_unchanged():
    no_id = "https://yu-gi-oh.jp/news_detail.php?page=details"
    assert _canonicalize_url(no_id) == no_id


def test_canonicalize_other_domain_unchanged():
    other_domain = "https://example.com/news_detail.php?page=details&id=2602"
    assert _canonicalize_url(other_domain) == other_domain


# ──────────────────────────────────────────────
# _add_new_links の重複統合検証
# ──────────────────────────────────────────────

class _FakeTable:
    """任意のチェーンメソッドを self を返すだけで模倣し、upsert() に渡された
    rows を記録する（tests/test_deck_ignore_duplicates.py のスタブ作法に合わせる）。"""

    def __init__(self, name: str, sink: list):
        self.name = name
        self.sink = sink
        self._pending = None

    def __getattr__(self, item):
        def _chain(*a, **k):
            return self
        return _chain

    def upsert(self, rows, on_conflict=None, ignore_duplicates=None):
        if self.name == "watched_pages":
            self.sink.append({"rows": rows, "on_conflict": on_conflict, "ignore_duplicates": ignore_duplicates})
        self._pending = rows
        return self

    def execute(self):
        from types import SimpleNamespace
        self._pending = None
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self, sink: list):
        self.sink = sink

    def table(self, name):
        return _FakeTable(name, self.sink)


def test_add_new_links_merges_old_and_new_url_duplicates():
    old_url = "https://yu-gi-oh.jp/news_detail.php?page=details&id=2602"
    new_url = "https://yu-gi-oh.jp/news/oid-2602/"

    sink: list = []
    sb = _FakeSupabase(sink)

    added = _add_new_links(sb, [old_url, new_url])

    assert added == [new_url]
    assert sink, "watched_pages.upsert が呼ばれていない"
    rows = sink[-1]["rows"]
    assert len(rows) == 1
    assert rows[0]["url"] == new_url


# ──────────────────────────────────────────────
# _is_article_page: 抽出対象は記事ページのみ（一覧ページは除外）
# ──────────────────────────────────────────────

def test_is_article_page_new_format_true():
    assert _is_article_page("https://yu-gi-oh.jp/news/4pai9s7t1s/")
    assert _is_article_page("https://yu-gi-oh.jp/news/k-czhitni_7n/")
    assert _is_article_page("https://yu-gi-oh.jp/news/0gcx_stpd/")


def test_is_article_page_old_format_true():
    assert _is_article_page("https://yu-gi-oh.jp/news_detail.php?page=details&id=2602")
    assert _is_article_page("https://www.yu-gi-oh.jp/news_detail.php?page=details&id=2602")


def test_is_article_page_list_pages_false():
    assert not _is_article_page("https://yu-gi-oh.jp/")
    assert not _is_article_page("https://yu-gi-oh.jp/news/")
    assert not _is_article_page("https://yu-gi-oh.jp/news/search/?tag=vjump")
    assert not _is_article_page("https://yu-gi-oh.jp/card/?tag=ocg")
    assert not _is_article_page("https://yu-gi-oh.jp/card/")


def test_main_skips_extraction_for_list_page(monkeypatch):
    """一覧ページは差分検知・リンク発見は行うが extract_cards_from_html を呼ばない。"""
    import sys
    import types
    from types import SimpleNamespace
    import watch_unreleased as w

    list_url = "https://yu-gi-oh.jp/news/search/?tag=vjump"
    calls = {"extract": 0, "changed": 0, "links": 0}

    fake_mod = types.ModuleType("unreleased_extractor")

    def _fake_extract(html, url):
        calls["extract"] += 1
        return []

    fake_mod.extract_cards_from_html = _fake_extract
    monkeypatch.setitem(sys.modules, "unreleased_extractor", fake_mod)

    monkeypatch.setattr(w, "SUPABASE_URL", "http://dummy")
    monkeypatch.setattr(w, "SUPABASE_KEY", "dummy")
    monkeypatch.setattr(w, "create_client", lambda *a, **k: object())
    monkeypatch.setattr(w, "_seed_watched_pages", lambda sb: None)
    monkeypatch.setattr(w, "_get_enabled_pages", lambda sb: [{"url": list_url, "content_hash": ""}])
    monkeypatch.setattr(
        w, "fetch_whitelisted",
        lambda url: SimpleNamespace(status_code=200, text="<html><body>list</body></html>"),
    )
    monkeypatch.setattr(w, "_update_changed", lambda *a, **k: calls.__setitem__("changed", calls["changed"] + 1))
    monkeypatch.setattr(w, "_update_checked", lambda *a, **k: None)

    def _fake_links(html, url):
        calls["links"] += 1
        return []

    monkeypatch.setattr(w, "_extract_candidate_links", _fake_links)
    monkeypatch.setattr(w, "_add_new_links", lambda sb, links: [])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy")

    w.main()

    assert calls["extract"] == 0
    assert calls["changed"] == 1
    assert calls["links"] == 1
