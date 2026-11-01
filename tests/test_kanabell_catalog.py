"""
tests/test_kanabell_catalog.py — カーナベルの全件巡回（kanabell_catalog.py・2026-11-01）

- scraper.kanabell_rows_from_source: 検索（scrape_kanabell）と巡回で同じ行を作る
- crawl_catalog: search_after でページ送りし、取得失敗・総件数との不足・上限は不成立にする
- match_rows: ダッシュ・長音の表記ゆれを吸収して名寄せする（検索方式と同じ）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
import requests

import kanabell_catalog as kc
import scraper


def _src(pid, name, price=100, stock=1, code="AB01-JP001"):
    inv = {"selling_price": price, "inventory_num": stock}
    return {"inventory_product_id": pid, "product_name": name,
            "yg_card": {"serial_number": code, "rarity": {"yg_rarity_abbreviation": "N"}},
            "rank_s_inventory": inv}


def _hit(pid, name, **k):
    return {"_source": _src(pid, name, **k), "sort": [pid]}


def test_rows_from_source_matches_search(monkeypatch):
    rows = scraper.kanabell_rows_from_source(_src("p1", "灰流うらら", price=500, stock=0))
    assert len(rows) == 1
    r = rows[0]
    assert r["shop"] == "カーナベル" and r["price"] == 500 and r["sold_out"] is True and r["stock"] == 0
    assert r["url"].endswith("/single/p1") and r["condition"] == "状態:S"
    # 名前の絞り込み関数が False なら空
    assert scraper.kanabell_rows_from_source(_src("p1", "灰流うらら"), lambda n: False) == []
    # 価格0の状態は行にしない
    assert scraper.kanabell_rows_from_source(_src("p2", "x", price=0)) == []


def _patch_pages(monkeypatch, pages, total, min_products=0):
    calls = []
    monkeypatch.setattr(kc, "MIN_PRODUCTS", min_products)

    def fake(after, timeout=60):
        calls.append(after)
        i = len(calls) - 1
        if isinstance(pages[i], Exception):
            raise pages[i]
        return pages[i], total

    monkeypatch.setattr(kc, "_fetch_page", fake)
    monkeypatch.setattr(kc, "PAGE_SIZE", 2)
    monkeypatch.setattr(kc.time, "sleep", lambda s: None)
    return calls


def test_crawl_pages_with_search_after(monkeypatch):
    pages = [[_hit("a", "A"), _hit("b", "B")], [_hit("c", "C")]]
    calls = _patch_pages(monkeypatch, pages, 3)
    rows, st = kc.crawl_catalog()
    assert [r["name"] for r in rows] == ["A", "B", "C"]
    assert calls == [None, ["b"]]
    assert st["listed"] == 3 and st["pages"] == 2 and not (st["failed"] or st["short"] or st["truncated"])


def test_crawl_failure_stops(monkeypatch):
    _patch_pages(monkeypatch, [[_hit("a", "A"), _hit("b", "B")], requests.ConnectionError("x")], 10)
    rows, st = kc.crawl_catalog()
    assert st["failed"] is True


def test_crawl_short_when_total_not_reached(monkeypatch):
    _patch_pages(monkeypatch, [[_hit("a", "A")]], 1000)
    rows, st = kc.crawl_catalog()
    assert st["short"] is True


def test_collect_none_when_not_ok(monkeypatch):
    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "http://x")
    monkeypatch.setattr(scraper, "_KANABELL_ES_API_KEY", "k")
    _patch_pages(monkeypatch, [[_hit("a", "灰流うらら")]], 1000)
    matched, stats = kc.collect_kanabell_catalog(["灰流うらら"])
    assert matched is None and stats["short"] is True


def test_collect_ok(monkeypatch):
    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "http://x")
    monkeypatch.setattr(scraper, "_KANABELL_ES_API_KEY", "k")
    _patch_pages(monkeypatch, [[_hit("a", "灰流うらら")]], 1)
    matched, stats = kc.collect_kanabell_catalog(["灰流うらら", "別のカード"])
    assert list(matched) == ["灰流うらら"] and matched["灰流うらら"][0]["url"].endswith("/single/a")


def test_match_dash_variants():
    rows = scraper.kanabell_rows_from_source(_src("d", "閃刀姫ーレイ"))
    matched, _ = kc.match_rows(rows, ["閃刀姫－レイ"])
    assert "閃刀姫－レイ" in matched


# ── collect_prices との配線 ──

import collect_prices as cp


def _row(shop, name, url, price=100, condition="状態:A", stock=1):
    return {"shop": shop, "name": name, "rarity": "ノーマル", "code": "AB01-JP001", "condition": condition,
            "price": price, "stock": stock, "sold_out": stock == 0, "url": url, "image": ""}


def test_collect_and_save_with_kanabell_products(monkeypatch):
    """on: カーナベルは検索せず巡回の行を使い、取得0件は empty に数える"""
    monkeypatch.setattr(scraper, "SHOPS", [("遊々亭", lambda n: [_row("遊々亭", n, "y1")]),
                                           ("カーナベル", lambda n: 1 / 0)])
    monkeypatch.setattr(cp, "compare_prices", scraper.compare_prices)
    saved = []
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: saved.extend(rows) or len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    stats = {}
    cp.collect_and_save(None, "灰流うらら", "2026-11-02", ["遊々亭"], shop_stats=stats,
                        kanabell_products=[_row("カーナベル", "灰流うらら", "k1", price=80)])
    assert {r["shop"] for r in saved} == {"遊々亭", "カーナベル"}
    assert stats["カーナベル"] == {"ok": 1, "empty": 0, "error": 0}
    cp.collect_and_save(None, "増殖するG", "2026-11-02", ["遊々亭"], shop_stats=stats, kanabell_products=[])
    assert stats["カーナベル"] == {"ok": 1, "empty": 1, "error": 0}


def test_compare_kanabell_shadow():
    a = _row("カーナベル", "灰流うらら", "k1", price=100)
    b = _row("カーナベル", "灰流うらら", "k2", price=80)
    a_sold = {**a, "stock": 0, "sold_out": True}
    matched = {"c1": [a], "c2": [a, b], "c3": [a], "c4": [a]}
    sink = {"c1": [a], "c2": [a], "c3": [a_sold], "c4": None}
    st = cp._compare_kanabell_shadow([{"card_name": n} for n in ["c1", "c2", "c3", "c4", "c5"]],
                                     matched, sink, "2026-11-02")
    assert st["products_both"] == 3 and st["products_only_catalog"] == 1 and st["products_value_differ"] == 1
    assert st["same"] == 1, "c1 は同じ"
    assert st["catalog_lower"] == 1, "c2 は巡回だけの安い商品で最安が下がる"
    assert st["only_catalog"] == 1, "c3 は検索では売り切れで行が無い"
    assert st["search_failed_cards"] == 1 and st["not_searched_cards"] == 1
    ex = [e for e in st["examples"] if e["kind"] == "product_only_catalog"][0]
    assert ex["url"] == "k2" and ex["name"] == "灰流うらら", "例に商品名を残す"


def test_collect_and_save_fills_kanabell_sink(monkeypatch):
    def ok(card_name, shop_names=None, status_out=None):
        status_out.update({"遊々亭": {"count": 1, "fetch_errors": 0}, "カーナベル": {"count": 1, "fetch_errors": 0}})
        return [_row("カーナベル", card_name, "k1"), _row("遊々亭", card_name, "y1")]

    monkeypatch.setattr(cp, "compare_prices", ok)
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    sink = {}
    cp.collect_and_save(None, "灰流うらら", "2026-11-02", ["遊々亭", "カーナベル"], kanabell_sink=sink)
    assert [r["url"] for r in sink["灰流うらら"]] == ["k1"]

    def fail(card_name, shop_names=None, status_out=None):
        status_out.update({"遊々亭": {"count": 1, "fetch_errors": 0}, "カーナベル": {"count": 0, "fetch_errors": 1}})
        return [_row("遊々亭", card_name, "y1")]

    monkeypatch.setattr(cp, "compare_prices", fail)
    cp.collect_and_save(None, "増殖するG", "2026-11-02", ["遊々亭", "カーナベル"], kanabell_sink=sink)
    assert sink["増殖するG"] is None


# ── レビューの指摘（2026-11-01）への手当て ──

class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


@pytest.mark.parametrize("data", [
    {"timed_out": True, "_shards": {"failed": 0}, "hits": {"hits": [], "total": {"value": 0}}},
    {"timed_out": False, "_shards": {"failed": 1}, "hits": {"hits": [], "total": {"value": 0}}},
])
def test_fetch_page_partial_failure_is_error(monkeypatch, data):
    """ES が 200 で一部の結果だけ返したら取得失敗（総件数もその分しか数えないので突き合わせで見抜けない）"""
    monkeypatch.setattr(kc.requests, "post", lambda *a, **k: _Resp(data))
    with pytest.raises(ValueError):
        kc._fetch_page(None)


def test_crawl_short_below_min_products(monkeypatch):
    _patch_pages(monkeypatch, [[_hit("a", "A")]], 1, min_products=10)
    _, st = kc.crawl_catalog()
    assert st["short"] is True, "総件数と合っていても下限に届かなければ不成立"


def test_crawl_total_none_is_short(monkeypatch):
    _patch_pages(monkeypatch, [[_hit("a", "A")]], None)
    _, st = kc.crawl_catalog()
    assert st["short"] is True


def test_crawl_truncated_by_pages_and_time(monkeypatch):
    two = [_hit("a", "A"), _hit("b", "B")]
    _patch_pages(monkeypatch, [two] * 5, 100)
    monkeypatch.setattr(kc, "CATALOG_MAX_PAGES", 2)
    _, st = kc.crawl_catalog()
    assert st["truncated"] is True and st["pages"] == 2
    _patch_pages(monkeypatch, [two] * 5, 100)
    monkeypatch.setattr(kc, "CATALOG_MAX_PAGES", 100)
    clock = iter([0, 0, 1000, 1000])
    monkeypatch.setattr(kc, "_now", lambda: next(clock))
    _, st = kc.crawl_catalog()
    assert st["truncated"] is True and st["pages"] == 1


def test_match_rows_by_name_counts_products(monkeypatch):
    """名寄せは商品名ごとに1回。統計は商品（URL）の数"""
    rows = (scraper.kanabell_rows_from_source(
                {**_src("u1", "灰流うらら"), "rank_a_inventory": {"selling_price": 90, "inventory_num": 2}})
            + scraper.kanabell_rows_from_source(_src("u2", "灰流うらら"))
            + scraper.kanabell_rows_from_source(_src("u3", "増殖するG")))
    calls = []
    real = kc.match_products

    def spy(products, index):
        calls.append(len(products))
        return real(products, index)

    monkeypatch.setattr(kc, "match_products", spy)
    matched, st = kc.match_rows(rows, ["灰流うらら"])
    assert calls == [2], "商品名2種類だけを名寄せする"
    assert len(matched["灰流うらら"]) == 3
    assert st["matched_products"] == 2 and st["unmatched_products"] == 1


# ── main の配線 ──

from tests.test_torecolo_catalog import _PrevSb  # noqa: E402


def _run_main(monkeypatch, mode, n_cards=2, tore_mode="off", catalog_ok=True, avail=None):
    names = [f"c{i}" for i in range(n_cards)]
    monkeypatch.setattr(cp, "get_supabase", lambda: _PrevSb())
    monkeypatch.setattr(cp, "sync_latest_packs", lambda sb: (set(names), set()))
    for fn in ("sync_meta_decks", "sync_regulation"):
        monkeypatch.setattr(cp, fn, lambda sb: set())
    monkeypatch.setattr(cp, "sync_searched_cards", lambda sb, **k: set())
    for fn in ("sync_recipe_decks", "sync_theme_wiki_cards", "sync_trending_cards", "save_pack_list"):
        monkeypatch.setattr(cp, fn, lambda sb: None)
    monkeypatch.setattr(cp, "sync_remake_themes", lambda sb, new: None)
    monkeypatch.setattr(cp, "fetch_tracked_cards_with_meta",
                        lambda sb: [{"card_name": n, "last_collected_at": None} for n in names])
    monkeypatch.setattr(cp, "SHOPS", [(s, None) for s in ["遊々亭", "トレコロCB", "カーナベル"]])
    monkeypatch.setattr(cp, "SKIP_SHOPS_IN_CI", set())
    # main の事前確認は ES の2つの環境変数を直に見る（巡回そのものは差し替えるので外へは出ない）
    monkeypatch.setenv("KANABELL_ES_ENDPOINT", "x")
    monkeypatch.setenv("KANABELL_ES_API_KEY", "x")
    monkeypatch.setattr(cp, "check_shop_availability", lambda s: list(avail if avail is not None else s))
    monkeypatch.setattr(cp, "TORECOLO_CATALOG_MODE", tore_mode)
    monkeypatch.setattr(cp, "KANABELL_CATALOG_MODE", mode)
    monkeypatch.setattr(cp, "collect_torecolo_catalog", lambda ns: (
        {n: [{**_row("トレコロCB", n, f"t-{n}"), "condition": "-"}] for n in ns},
        {"products": len(ns), "matched_products": len(ns)}))
    monkeypatch.setattr(cp, "_fetch_torecolo_prev_rows", lambda sb, day: {})
    monkeypatch.setattr(cp, "collect_kanabell_catalog", lambda ns: (
        ({n: [_row("カーナベル", n, f"k-{n}")] for n in ns} if catalog_ok else None),
        {"listed": len(ns), "rows": len(ns), "matched_products": len(ns)}))
    calls = []

    def fake_collect(sb, card_name, today, shop_names, **kw):
        calls.append((card_name, list(shop_names), kw))
        if "kanabell_sink" in kw:
            kw["kanabell_sink"][card_name] = [_row("カーナベル", card_name, f"k-{card_name}")]
        return 1

    monkeypatch.setattr(cp, "collect_and_save", fake_collect)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cp, "send_daily_report", lambda *a, **k: None)
    monkeypatch.setattr(cp, "cleanup_old_data", lambda sb: None)
    monkeypatch.setattr(cp, "get_host_request_counts", lambda: {})
    runs = []
    monkeypatch.setattr(cp, "record_collection_run", lambda sb, kind, *a, **k: runs.append(k) or True)
    monkeypatch.setattr(cp, "watch_suspended_shops", lambda: None)
    cp.main()
    return calls, runs[-1]["shop_stats"]


def test_main_kanabell_shadow(monkeypatch):
    calls, stats = _run_main(monkeypatch, "shadow")
    assert calls and all("カーナベル" in shops and "kanabell_sink" in kw and "kanabell_products" not in kw
                         for _, shops, kw in calls)
    st = stats["カーナベル"]
    assert st["mode"] == "shadow" and st["shadow"]["same"] == 2 and st["shadow"]["products_both"] == 2


def test_main_kanabell_on_with_torecolo_on(monkeypatch):
    calls, stats = _run_main(monkeypatch, "on", tore_mode="on")
    assert calls
    for card, shops, kw in calls:
        assert shops == ["遊々亭"], "トレコロもカーナベルも検索しない"
        assert [r["url"] for r in kw["kanabell_products"]] == [f"k-{card}"]
        assert [r["url"] for r in kw["torecolo_products"]] == [f"t-{card}"]
        assert "kanabell_sink" not in kw
    assert stats["カーナベル"]["mode"] == "catalog" and stats["トレコロCB"]["mode"] == "catalog"


def test_main_kanabell_fallback_searches(monkeypatch):
    calls, stats = _run_main(monkeypatch, "on", catalog_ok=False)
    assert calls and all("カーナベル" in shops and "kanabell_products" not in kw for _, shops, kw in calls)
    assert stats["カーナベル"]["mode"] == "fallback"


def test_prepare_kanabell_modes(monkeypatch):
    matched = {"c": [_row("カーナベル", "c", "k1")]}
    monkeypatch.setattr(cp, "collect_kanabell_catalog", lambda names: (matched, {"listed": 1, "rows": 1, "matched_products": 1}))
    sel = [{"card_name": "c"}]
    stats = {}
    shops, m = cp._prepare_kanabell_catalog(sel, ["遊々亭", "カーナベル"], ["遊々亭", "カーナベル"], stats, mode="shadow")
    assert shops == ["遊々亭", "カーナベル"] and m is matched and stats["カーナベル"]["mode"] == "shadow"
    shops, m = cp._prepare_kanabell_catalog(sel, ["遊々亭", "カーナベル"], ["遊々亭", "カーナベル"], stats, mode="on")
    assert shops == ["遊々亭"] and m is matched and stats["カーナベル"]["mode"] == "catalog"
    shops, m = cp._prepare_kanabell_catalog(sel, ["遊々亭", "カーナベル"], ["遊々亭", "カーナベル"], stats, mode="off")
    assert m is None
    monkeypatch.setattr(cp, "collect_kanabell_catalog", lambda names: (None, {"failed": True}))
    shops, m = cp._prepare_kanabell_catalog(sel, ["遊々亭", "カーナベル"], ["遊々亭", "カーナベル"], stats, mode="on")
    assert m is None and shops == ["遊々亭", "カーナベル"] and stats["カーナベル"]["mode"] == "fallback"


def test_main_kanabell_on_when_other_shops_down(monkeypatch):
    """on で、ほかの店がすべて使えない晩も、カーナベルの巡回の行だけで保存する（ループは回し、待ちは飛ばす）"""
    calls, stats = _run_main(monkeypatch, "on", avail=["カーナベル"])
    assert calls and all(shops == [] and kw["kanabell_products"] for _, shops, kw in calls)
    assert stats["カーナベル"]["mode"] == "catalog"
