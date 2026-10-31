"""
tests/test_torecolo_catalog.py — トレコロの在庫一覧の巡回（torecolo_catalog.py・2026-10-31）

- scrape_torecolo は extra_query を URL に足し、ページの間は page_wait 秒待つ（既定は従来どおり）
- crawl_catalog: 系列ごとに読み、URL で重複を除く。取得の失敗・上限に届いた系列は記録する
- collect_torecolo_catalog: 失敗・上限・0件なら不成立（None）
- crawl_catalog: 系列の総件数の表示と読んだ件数を突き合わせ、足りない系列は不成立。1つ失敗したらその晩はやめる
- collect_torecolo_catalog: 新しい系列が出たら不成立
- collect_prices: TORECOLO_CATALOG_MODE が shadow なら保存せず、ループで得たトレコロの結果と巡回の値を比べる。
  on は準備中（TORECOLO_CATALOG_ON_READY が False の間は shadow として動く）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

import collect_prices as cp
import scraper
import torecolo_catalog as tc


def _item(name, url, price=100, rarity="ノーマル", condition="-"):
    return {"shop": "トレコロCB", "name": name, "rarity": rarity, "code": "", "condition": condition,
            "price": price, "stock": 1, "sold_out": False, "url": url, "image": ""}


# ── scrape_torecolo の引数 ──

def test_scrape_torecolo_extra_query_and_page_wait(monkeypatch):
    urls, waits = [], []
    page = BeautifulSoup('<dl class="block-thumbnail-t--goods"></dl><a href="?p=2">次</a>', "html.parser")
    empty = BeautifulSoup("", "html.parser")

    def fake_get(url, *a, **k):
        urls.append(url)
        return page if len(urls) == 1 else empty

    monkeypatch.setattr(scraper, "safe_get", fake_get)
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: waits.append(s))
    scraper.scrape_torecolo("", max_pages=3, extra_query="&ct3=1010EV&filtercode13=1&seq=g", page_wait=1.2)
    assert urls[0].endswith("&ct2=1010&ct3=1010EV&filtercode13=1&seq=g")
    assert waits == [1.2]


def _meta_for(html, monkeypatch):
    monkeypatch.setattr(scraper, "safe_get", lambda url, *a, **k: BeautifulSoup(html, "html.parser"))
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)
    meta = {}
    scraper.scrape_torecolo("", max_pages=1, meta_out=meta)
    return meta


def test_scrape_torecolo_meta_total(monkeypatch):
    # 商品のあるページ: 「N 件あります」
    meta = _meta_for('<div class="pager-total">11 件あります</div><dl class="block-thumbnail-t--goods"></dl>', monkeypatch)
    assert meta == {"total": 11, "pages": 1, "listed": 1}
    # 0件のページ: 総件数の表示が無く、この文言だけ出る（実店「ノーマルカード」1010EU の形）
    meta = _meta_for(f'<p>{scraper.TORECOLO_NO_RESULTS_TEXT}。</p>', monkeypatch)
    assert meta == {"total": 0, "pages": 1, "listed": 0}
    # 文言も表示も無い空ページ（WAF など）は None のまま＝読み切れていない扱い
    meta = _meta_for('<html><body></body></html>', monkeypatch)
    assert meta["total"] is None


# ── 巡回 ──

def _fake_series(table):
    """table: 系列 → (商品, total, listed, pages, 取得の失敗)"""
    calls = []

    def fake_scrape(card_name, max_pages=5, ct2="1010", extra_query="", page_wait=0.5, meta_out=None):
        calls.append((card_name, max_pages, extra_query, page_wait))
        sr = extra_query.split("ct3=")[1].split("&")[0]
        items, total, listed, pages, fail = table[sr]
        if meta_out is not None:
            meta_out.update({"total": total, "listed": listed, "pages": pages})
        if fail:
            scraper._note_fetch_error()
        return items
    return fake_scrape, calls


def test_crawl_catalog_dedupes_and_checks_counts(monkeypatch):
    ok_items = [_item("灰流うらら", "u1"), _item("灰流うらら", "u1"), _item("増殖するG", "u2")]
    fake, calls = _fake_series({
        "A": (ok_items, 3, 3, 1, False),
        "C": ([_item("X", "u-c")], 99999, 10000, tc.CATALOG_MAX_PAGES, False),   # 上限のページ数に届いた
        "D": ([_item("Y", "u-d")], 20, 10, 1, False),                            # 総件数に足りない
        "E": ([_item("Z", "u-e")], None, 1, 1, False),                            # 総件数が読めない
        "F": ([_item("W", "u-f")], 5, 3, 1, False),                               # 足りないのが許す幅（3）以内
    })
    monkeypatch.setattr(scraper, "scrape_torecolo", fake)
    products, stats = tc.crawl_catalog({k: f"系列{k}" for k in "ACDEF"})
    assert sorted(p["url"] for p in products) == ["u-f", "u1", "u2"], "URL で重複を除き、読み切れていない系列は使わない"
    assert stats["series_truncated"] == ["C"]
    assert stats["series_short"] == ["D", "E"]
    assert calls[0] == ("", tc.CATALOG_MAX_PAGES, "&ct3=A&filtercode13=1&seq=g", tc.CATALOG_PAGE_WAIT)


def test_crawl_catalog_stops_on_failure(monkeypatch):
    fake, calls = _fake_series({"A": ([], 0, 0, 1, True), "B": ([_item("X", "u")], 1, 1, 1, False)})
    monkeypatch.setattr(scraper, "scrape_torecolo", fake)
    products, stats = tc.crawl_catalog({"A": "系列A", "B": "系列B"})
    assert stats["series_failed"] == ["A"] and stats["stopped_early"]
    assert len(calls) == 1, "1つ失敗したらその晩はやめる（店が弱っているときに読み続けない）"


def test_crawl_catalog_turns_name_filter_off(monkeypatch):
    seen = []

    def fake_scrape(card_name, meta_out=None, **k):
        seen.append(scraper.is_target_card(card_name, "灰流うらら"))
        meta_out.update({"total": 0, "listed": 0, "pages": 1})
        return []

    monkeypatch.setattr(scraper, "scrape_torecolo", fake_scrape)
    tc.crawl_catalog({"A": "系列A"})
    assert seen == [True], "巡回の中ではカード名の一致を見ない（キーワードは空）"


def test_check_series_drift():
    known = dict(tc.TORECOLO_SERIES)
    assert tc.check_series_drift(None) is None
    assert tc.check_series_drift(known) == []
    changed = dict(known)
    changed.pop("1010EW")
    changed["1010ZZ"] = "新しい系列"
    w = tc.check_series_drift(changed)
    assert any("新しい系列" in x for x in w) and any("そのほか" in x for x in w)


def test_collect_torecolo_catalog_ok_and_not_ok(monkeypatch):
    monkeypatch.setattr(tc, "harvest_series", lambda: dict(tc.TORECOLO_SERIES))
    base = {"series": 41, "series_failed": [], "series_truncated": [], "series_short": [], "per_series": {}}
    monkeypatch.setattr(tc, "crawl_catalog", lambda: (
        [_item("灰流うらら", "u1"), _item("灰流うらら（ノーマル）", "u2")], {**base, "products": 2}))
    matched, stats = tc.collect_torecolo_catalog(["灰流うらら", "増殖するG"])
    assert set(matched) == {"灰流うらら"} and len(matched["灰流うらら"]) == 2
    monkeypatch.setattr(tc, "crawl_catalog", lambda: (
        [_item("灰流うらら", "u1")], {**base, "series_failed": ["1010EV"], "products": 1}))
    matched, stats = tc.collect_torecolo_catalog(["灰流うらら"])
    assert matched is None, "系列の取得に失敗したら不成立"
    monkeypatch.setattr(tc, "crawl_catalog", lambda: ([_item("灰流うらら", "u1")], {**base, "series_short": ["1010CL"], "products": 1}))
    assert tc.collect_torecolo_catalog(["灰流うらら"])[0] is None, "読み切れていない系列があれば不成立"
    monkeypatch.setattr(tc, "crawl_catalog", lambda: ([], {**base, "products": 0}))
    assert tc.collect_torecolo_catalog(["灰流うらら"])[0] is None, "0件は不成立"


def test_collect_torecolo_catalog_new_series_is_not_ok(monkeypatch):
    harvested = dict(tc.TORECOLO_SERIES)
    harvested["1010ZZ"] = "新しい系列"
    monkeypatch.setattr(tc, "harvest_series", lambda: harvested)
    monkeypatch.setattr(tc, "crawl_catalog", lambda: (_ for _ in ()).throw(AssertionError("巡回しない")))
    matched, stats = tc.collect_torecolo_catalog(["灰流うらら"])
    assert matched is None and any("新しい系列" in w for w in stats["series_drift"])


# ── collect_prices への組み込み ──

SELECTED = [{"card_name": "灰流うらら"}, {"card_name": "増殖するG"}]
SHOPS = ["遊々亭", "トレコロCB"]


def test_prepare_modes(monkeypatch):
    monkeypatch.setattr(cp, "collect_torecolo_catalog",
                        lambda names: ({"灰流うらら": [_item("灰流うらら", "u1")]}, {"products": 1, "matched_products": 1}))
    stats = {}
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, stats, mode="shadow")
    assert loop == SHOPS and matched and stats["トレコロCB"]["mode"] == "shadow", "shadow はトレコロをループに残す"
    stats = {}
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, stats, mode="on")
    assert loop == SHOPS and matched and stats["トレコロCB"]["mode"] == "shadow", "on は準備中の間 shadow として動く"
    monkeypatch.setattr(cp, "TORECOLO_CATALOG_ON_READY", True)
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, {}, mode="on")
    assert loop == ["遊々亭"] and matched, "準備ができたら on はトレコロをループから外す"
    monkeypatch.setattr(cp, "TORECOLO_CATALOG_ON_READY", False)
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, {}, mode="true")
    assert loop == SHOPS and matched is None, "分からない値は巡回しない（警告を出す）"
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, {}, mode="off")
    assert loop == SHOPS and matched is None
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, ["遊々亭"], ["遊々亭"], {}, mode="on")
    assert matched is None, "トレコロが使えない晩は巡回しない"


def test_prepare_fallback_and_exception(monkeypatch):
    monkeypatch.setattr(cp, "collect_torecolo_catalog", lambda names: (None, {"series_failed": ["1010EV"]}))
    stats = {}
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, stats, mode="shadow")
    assert loop == SHOPS and matched is None and stats["トレコロCB"]["mode"] == "fallback"

    def boom(names):
        raise RuntimeError("つながらない")

    monkeypatch.setattr(cp, "collect_torecolo_catalog", boom)
    stats = {}
    loop, matched, _ = cp._prepare_torecolo_catalog(SELECTED, SHOPS, SHOPS, stats, mode="shadow")
    assert loop == SHOPS and matched is None and stats["トレコロCB"]["mode"] == "error"


def test_save_rows_only_for_processed(monkeypatch):
    saved_rows = []
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: saved_rows.extend(rows) or len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    matched = {"灰流うらら": [_item("灰流うらら", "u1", price=180)], "増殖するG": [_item("増殖するG", "u2", price=80)]}
    stats = {}
    n = cp._save_torecolo_catalog_rows(None, SELECTED[:1], matched, "2026-11-01", stats, {"products": 2})
    assert n == 1 and [(r["card_name"], r["min_price_any"]) for r in saved_rows] == [("灰流うらら", 180)]
    assert stats["トレコロCB"]["mode"] == "catalog" and stats["トレコロCB"]["ok"] == 1


def test_compare_shadow_uses_loop_results():
    """DB は読まず、ループで得たトレコロの結果と比べる。取得に失敗したカード・未処理のカードは数えるだけ"""
    selected = [{"card_name": n} for n in ("灰流うらら", "増殖するG", "墓穴の指名者", "抹殺の指名者")]
    sink = {
        "灰流うらら": [_item("灰流うらら", "s1", price=180)],
        "増殖するG": [_item("増殖するG", "s2", price=70), _item("増殖するG", "s3", price=300, rarity="スーパー")],
        "墓穴の指名者": None,   # トレコロの取得に失敗
        # 抹殺の指名者: ループで処理されなかった
    }
    matched = {"灰流うらら": [_item("灰流うらら", "u1", price=180),
                         _item("灰流うらら", "u3", price=900, rarity="シークレット")],
               "増殖するG": [_item("増殖するG", "u2", price=80)],
               "墓穴の指名者": [_item("墓穴の指名者", "u4", price=10)]}
    st = cp._compare_torecolo_shadow(selected, matched, sink, "2026-11-01")
    assert (st["same"], st["catalog_higher"], st["catalog_lower"], st["mixed"], st["only_catalog"], st["only_search"]) \
        == (1, 1, 0, 0, 1, 1)
    assert st["search_failed_cards"] == 1 and st["not_searched_cards"] == 1
    assert {e["kind"] for e in st["examples"]} == {"catalog_higher", "only_catalog", "only_search"}


def test_compare_shadow_mixed_direction():
    """状態をそろえた値と状態を問わない値で向きが違えば「上下混在」"""
    sink = {"灰流うらら": [_item("灰流うらら", "s1", price=100), _item("灰流うらら", "s2", price=50, condition="中古キズあり")]}
    matched = {"灰流うらら": [_item("灰流うらら", "u1", price=90), _item("灰流うらら", "u2", price=60, condition="中古キズあり")]}
    st = cp._compare_torecolo_shadow([{"card_name": "灰流うらら"}], matched, sink, "2026-11-01")
    assert st["mixed"] == 1


def test_collect_and_save_fills_torecolo_sink(monkeypatch):
    def fake_compare(card_name, shop_names=None, status_out=None):
        status_out.update({"遊々亭": {"count": 1, "fetch_errors": 0}, "トレコロCB": {"count": 1, "fetch_errors": 0}})
        return [_item(card_name, "t1", price=100), {**_item(card_name, "y1", price=90), "shop": "遊々亭"}]

    monkeypatch.setattr(cp, "compare_prices", fake_compare)
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    sink = {}
    cp.collect_and_save(None, "灰流うらら", "2026-11-01", ["遊々亭", "トレコロCB"], torecolo_sink=sink)
    assert [r["url"] for r in sink["灰流うらら"]] == ["t1"], "トレコロの商品だけ残す"

    def fake_fail(card_name, shop_names=None, status_out=None):
        status_out.update({"遊々亭": {"count": 1, "fetch_errors": 0}, "トレコロCB": {"count": 0, "fetch_errors": 1}})
        return [{**_item(card_name, "y1", price=90), "shop": "遊々亭"}]

    monkeypatch.setattr(cp, "compare_prices", fake_fail)
    cp.collect_and_save(None, "増殖するG", "2026-11-01", ["遊々亭", "トレコロCB"], torecolo_sink=sink)
    assert sink["増殖するG"] is None, "トレコロの取得に失敗したら None（比べない）"
