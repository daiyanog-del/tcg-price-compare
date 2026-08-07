"""
tests/test_pagination.py — 店舗スクレイパーのページ送りの回帰テスト

背景:
  2026-08-03 の横断調査で、1ページ目しか取っていない経路が商品を取りこぼして
  いることが実測で判明した。
    - カードラボ販売: 既定は1ページ60件。「ブラック・マジシャン」は全246件（5ページ）
      あるのに60件しか取れていなかった。2026-08-07 に `&num=120` で1ページ120件へ
      拡張（403対策のリクエスト削減。240は無効で60に落ちる＝上限120）
    - カードラッシュ買取: 1ページ100件が上限。同カードは全182件（2ページ）
  終端の検出方法は店舗ごとに異なるため、実測で確定した仕様をここで固定する。
    - カードラボ: 範囲外のページを要求すると **1ページ目の内容が返る**（件数は減らない）
      ため、総ヒット件数(.item_count)由来の必要ページ数を第一に、
      ページャの最終ページ番号／120件未満／全件既出 を保険として止める
    - カードラッシュ買取: `props.pageProps.lastPage` に総ページ数があり、
      範囲外は **空の buyingPrices** を返す

テスト方針:
  - safe_get をモックし、ネットワークに一切出ない状態で検証する。
  - 「何ページ取りに行ったか」（リクエストURLの列）も検証する。夜間収集の
    リクエスト数に直結するため、無駄打ちが増えていないことを固定する。
"""

import json
import sys
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
from scraper import (
    CLABO_PAGE_SIZE,
    CARDRUSH_BUY_PAGE_SIZE,
    scrape_clabo,
    scrape_cardrush_buy,
    _clabo_last_page,
    _clabo_total_count,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """ページ間ウェイトでテストを待たせない"""
    monkeypatch.setattr(scraper.time, "sleep", lambda *_a, **_k: None)


# ── カードラボ販売 ──

def _clabo_item_html(idx: int) -> str:
    return (
        f'<li><a href="/product/{idx}">x</a>'
        f'<div class="inner_item_data">'
        f'<span class="goods_name">【遊戯】テストカード【ウルトラ/通常】TEST-JP{idx:03d}</span>'
        f'<span class="figure">1,000円</span>'
        f'<p class="stock">在庫3点</p>'
        f'<div class="async_image_box" data-src="https://img/{idx}.jpg"></div>'
        f"</div></li>"
    )


def _clabo_page_html(indices, last_page: int | None = None,
                     total: int | None = None) -> str:
    """商品リスト・ページャ・総件数表示からなる検索結果ページを組み立てる"""
    pager = ""
    if last_page and last_page > 1:
        links = "".join(
            f'<a class="pager_btn" href="/product-list?keyword=x&page={p}">{p}</a>'
            for p in range(2, last_page)
        )
        pager = (
            '<div class="pager"><strong>1</strong>' + links
            + f'<a class="to_last_page pager_btn" href="/product-list?keyword=x&page={last_page}">{last_page}</a>'
            + "</div>"
        )
    count = f'<p class="item_count">{total:,}件</p>' if total is not None else ""
    items = "".join(_clabo_item_html(i) for i in indices)
    return f"<html><body>{count}<ul>{items}</ul>{pager}</body></html>"


def _install_clabo_pages(monkeypatch, pages: dict, calls: list):
    """page番号 -> HTML（または None＝取得失敗）の辞書で safe_get を差し替える"""
    def fake_get(url, timeout=15, retries=1):
        calls.append(url)
        page = 1
        if "&page=" in url:
            page = int(url.split("&page=")[1].split("&")[0])
        html = pages.get(page)
        if html is None:
            return None
        return BeautifulSoup(html, "html.parser")

    monkeypatch.setattr(scraper, "safe_get", fake_get)


class TestClaboLastPage:
    def test_reads_last_page_from_pager(self):
        soup = BeautifulSoup(_clabo_page_html(range(1, 4), last_page=5), "html.parser")
        assert _clabo_last_page(soup) == 5

    def test_returns_1_when_no_pager(self):
        soup = BeautifulSoup(_clabo_page_html(range(1, 4)), "html.parser")
        assert _clabo_last_page(soup) == 1


class TestClaboTotalCount:
    def test_reads_total_with_comma(self):
        soup = BeautifulSoup(_clabo_page_html(range(1, 4), total=7603), "html.parser")
        assert _clabo_total_count(soup) == 7603

    def test_returns_none_when_missing(self):
        soup = BeautifulSoup(_clabo_page_html(range(1, 4)), "html.parser")
        assert _clabo_total_count(soup) is None


class TestScrapeClaboPagination:
    def test_collects_items_from_all_pages(self, monkeypatch):
        """総件数が示す限り次ページを取りに行く（120件/ページ）"""
        calls = []
        _install_clabo_pages(monkeypatch, {
            1: _clabo_page_html(range(1, 121), last_page=3, total=250),
            2: _clabo_page_html(range(121, 241), last_page=3, total=250),
            3: _clabo_page_html(range(241, 251), last_page=3, total=250),
        }, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 250
        assert len(calls) == 3
        codes = {r["code"] for r in results}
        assert "TEST-JP001" in codes and "TEST-JP250" in codes

    def test_single_page_makes_only_one_request(self, monkeypatch):
        """120件未満なら1リクエストで終わる（既存カードの負荷を増やさない）"""
        calls = []
        _install_clabo_pages(monkeypatch, {1: _clabo_page_html(range(1, 31), total=30)}, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 30
        assert calls == ["https://www.c-labo-online.jp/product-list?keyword=%E3%83%86%E3%82%B9%E3%83%88%E3%82%AB%E3%83%BC%E3%83%89&num=120"]

    def test_stops_at_exact_page_boundary_without_extra_request(self, monkeypatch):
        """総件数がちょうど120件×Nなら、空振りの次ページを踏まない"""
        calls = []
        _install_clabo_pages(monkeypatch, {
            1: _clabo_page_html(range(1, 121), last_page=2, total=240),
            2: _clabo_page_html(range(121, 241), last_page=2, total=240),
            3: _clabo_page_html(range(500, 620), last_page=2, total=240),  # 取りに行ったら失敗
        }, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 240
        assert len(calls) == 2

    def test_total_count_stops_even_without_pager(self, monkeypatch):
        """ページャが無くても総件数だけで必要ページ数を判断する
        （総件数130件=2ページ。旧実装のページャ依存だと1ページで
        打ち切られて10件を取りこぼす）"""
        calls = []
        _install_clabo_pages(monkeypatch, {
            1: _clabo_page_html(range(1, 121), total=130),
            2: _clabo_page_html(range(121, 131), total=130),
        }, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 130
        assert len(calls) == 2

    def test_falls_back_to_pager_when_no_total_count(self, monkeypatch):
        """item_count が読めない場合はページャの最終番号で打ち切る"""
        calls = []
        _install_clabo_pages(monkeypatch, {
            1: _clabo_page_html(range(1, 121), last_page=2),
            2: _clabo_page_html(range(121, 241), last_page=2),
            3: _clabo_page_html(range(500, 620), last_page=2),  # 取りに行ったら失敗
        }, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 240
        assert len(calls) == 2

    def test_stops_when_out_of_range_page_repeats_first_page(self, monkeypatch):
        """範囲外ページが1ページ目を返す挙動への保険（総件数が過大でも重複で止める）"""
        calls = []
        first = _clabo_page_html(range(1, 121), last_page=9, total=1080)
        _install_clabo_pages(monkeypatch, {1: first, 2: first, 3: first}, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 120     # 2ページ目の重複分は加算されない
        assert len(calls) == 2         # 重複を検出した時点で打ち切る

    def test_respects_max_pages(self, monkeypatch):
        """max_pages を超えて取りに行かない"""
        calls = []
        pages = {p: _clabo_page_html(range(p * 1000, p * 1000 + 120), last_page=10, total=1200)
                 for p in range(1, 11)}
        _install_clabo_pages(monkeypatch, pages, calls)

        results = scrape_clabo("テストカード", max_pages=3)

        assert len(calls) == 3
        assert len(results) == 3 * CLABO_PAGE_SIZE

    def test_keeps_earlier_pages_when_later_fetch_fails(self, monkeypatch):
        """途中のページが取得失敗しても、取れた分は捨てない"""
        calls = []
        _install_clabo_pages(monkeypatch, {
            1: _clabo_page_html(range(1, 121), last_page=3, total=250),
            2: None,
        }, calls)

        results = scrape_clabo("テストカード")

        assert len(results) == 120
        assert len(calls) == 2

    def test_returns_empty_when_first_fetch_fails(self, monkeypatch):
        calls = []
        _install_clabo_pages(monkeypatch, {1: None}, calls)
        assert scrape_clabo("テストカード") == []


# ── カードラッシュ買取 ──

def _cardrush_buy_html(count: int, last_page, start: int = 0) -> str:
    prices = [
        {"name": "テストカード", "model_number": f"TEST-JP{start + i:03d}",
         "amount": 100 + i, "rarity": "ウルトラレア", "is_hot": False}
        for i in range(count)
    ]
    payload = {"props": {"pageProps": {"buyingPrices": prices, "lastPage": last_page}}}
    if last_page is None:
        del payload["props"]["pageProps"]["lastPage"]
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + "</script></body></html>"
    )


def _install_cardrush_buy_pages(monkeypatch, pages: dict, calls: list):
    def fake_get(url, timeout=15, retries=1):
        calls.append(url)
        page = 1
        if "&page=" in url:
            page = int(url.split("&page=")[1].split("&")[0])
        html = pages.get(page)
        if html is None:
            return None
        return BeautifulSoup(html, "html.parser")

    monkeypatch.setattr(scraper, "safe_get", fake_get)


class TestScrapeCardrushBuyPagination:
    def test_collects_items_from_all_pages(self, monkeypatch):
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {
            1: _cardrush_buy_html(100, last_page=2, start=0),
            2: _cardrush_buy_html(82, last_page=2, start=100),
        }, calls)

        results = scrape_cardrush_buy("テストカード")

        assert len(results) == 182
        assert len(calls) == 2
        assert "&page=2" in calls[1]

    def test_single_page_makes_only_one_request(self, monkeypatch):
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {1: _cardrush_buy_html(79, last_page=1)}, calls)

        results = scrape_cardrush_buy("テストカード")

        assert len(results) == 79
        assert len(calls) == 1

    def test_stops_on_empty_page_when_last_page_missing(self, monkeypatch):
        """lastPage が無い場合の保険。範囲外は空配列が返るのでそこで止める"""
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {
            1: _cardrush_buy_html(CARDRUSH_BUY_PAGE_SIZE, last_page=None, start=0),
            2: _cardrush_buy_html(0, last_page=None),
        }, calls)

        results = scrape_cardrush_buy("テストカード")

        assert len(results) == CARDRUSH_BUY_PAGE_SIZE
        assert len(calls) == 2

    def test_stops_on_partial_page_when_last_page_missing(self, monkeypatch):
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {
            1: _cardrush_buy_html(CARDRUSH_BUY_PAGE_SIZE, last_page=None, start=0),
            2: _cardrush_buy_html(30, last_page=None, start=100),
            3: _cardrush_buy_html(30, last_page=None, start=200),
        }, calls)

        results = scrape_cardrush_buy("テストカード")

        assert len(results) == CARDRUSH_BUY_PAGE_SIZE + 30
        assert len(calls) == 2

    def test_respects_max_pages(self, monkeypatch):
        calls = []
        pages = {p: _cardrush_buy_html(CARDRUSH_BUY_PAGE_SIZE, last_page=9, start=p * 1000)
                 for p in range(1, 10)}
        _install_cardrush_buy_pages(monkeypatch, pages, calls)

        results = scrape_cardrush_buy("テストカード", max_pages=2)

        assert len(calls) == 2
        assert len(results) == 2 * CARDRUSH_BUY_PAGE_SIZE

    def test_keeps_earlier_pages_when_later_fetch_fails(self, monkeypatch):
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {
            1: _cardrush_buy_html(CARDRUSH_BUY_PAGE_SIZE, last_page=3, start=0),
            2: None,
        }, calls)

        results = scrape_cardrush_buy("テストカード")

        assert len(results) == CARDRUSH_BUY_PAGE_SIZE
        assert len(calls) == 2

    def test_returns_empty_when_first_fetch_fails(self, monkeypatch):
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {1: None}, calls)
        assert scrape_cardrush_buy("テストカード") == []

    def test_page_url_points_to_the_page_the_item_came_from(self, monkeypatch):
        """商品URLは検索結果ページ。2ページ目の商品には page=2 付きのURLを持たせる"""
        calls = []
        _install_cardrush_buy_pages(monkeypatch, {
            1: _cardrush_buy_html(CARDRUSH_BUY_PAGE_SIZE, last_page=2, start=0),
            2: _cardrush_buy_html(5, last_page=2, start=100),
        }, calls)

        results = scrape_cardrush_buy("テストカード")

        assert "&page=" not in results[0]["url"]
        assert "&page=2" in results[-1]["url"]
