"""
tests/test_cardrush_catalog.py — カードラッシュの型番の頭の検索でまとめて読む巡回（cardrush_catalog.py・2026-11-01）

- scraper.cardrush_row_from_item: 検索（_parse_cardrush_items）と巡回で同じ行を作る
- crawl_heads: 型番の頭ごとに在庫ありの範囲だけ読み、在庫なしのページで止まる。重複は URL で除く。
  取得失敗・上限・時間切れ・並びの崩れ・下限は不成立にする。遅い応答のあとは余計に待つ
- collect_cardrush_catalog: 全頭より少ない晩は試し読み（毎晩同じ頭・名寄せしない）
- match_rows: 元の商品名で is_target_card を通す（検索方式と同じ）
- collect_prices: CARDRUSH_CATALOG_MODE（shadow / on / off）・試し読み（probe）の配線と shadow の比べ
- collect_prices: 直近2晩に行の無かったカードの検索の間引き（CARDRUSH_EMPTY_REST_DAYS）
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

import cardrush_catalog as rc
import collect_prices as cp
import scraper


def _li(pid, raw, price=100, stock=1):
    stock_txt = f"在庫数 {stock}枚" if stock else "在庫数 ×"
    return (f'<li class="list_item_cell list_item_{pid}"><a class="item_data_link" '
            f'href="https://www.cardrush.jp/product/{pid}"><img src="i{pid}.jpg"></a>'
            f'<p class="item_name">{raw}</p><div class="price">{price:,}円</div>'
            f'<p class="stock">{stock_txt}</p></li>')


def _page(items, total=1000):
    html = (f'<div class="category_item_count"><p class="count_number"><span class="number">{total:,}</span>'
            f'<span class="count_suffix">件</span></p></div><ul>{"".join(items)}</ul>')
    return BeautifulSoup(html, "html.parser")


def _name(card, rarity="ノーマル", code="AB01-JP001", reading="ヨミ"):
    return f"{card}【{rarity}】{{{code}}}《モンスター》[{reading}]"


def _patch_pages(monkeypatch, pages, page_size=3, min_in_stock=0, secs=None):
    """pages: {型番の頭: [1ページ目, 2ページ目, ...]}。範囲外のページは None（取得失敗）にする。
    secs: {(頭, ページ): 応答にかかる秒}（_now を進める。既定は0.1秒）。"""
    calls = []
    clock = [0.0]
    sleeps = []
    monkeypatch.setattr(rc, "MIN_IN_STOCK", min_in_stock)
    monkeypatch.setattr(scraper, "CARDRUSH_PAGE_SIZE", page_size)
    monkeypatch.setattr(rc.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(rc, "_now", lambda: clock[0])

    def fake(head, page):
        calls.append((head, page))
        clock[0] += (secs or {}).get((head, page), 0.1)
        ps = pages.get(head, [])
        return ps[page - 1] if page - 1 < len(ps) else None

    monkeypatch.setattr(rc, "_fetch_page", fake)
    return calls, sleeps


def test_search_url():
    assert rc._search_url("IMPH", 1) == "https://www.cardrush.jp/product-list?keyword=IMPH"
    assert rc._search_url("IMPH", 3) == "https://www.cardrush.jp/product-list?keyword=IMPH&page=3"


def test_row_from_item_matches_search():
    li = BeautifulSoup(_li(5, _name("灰流うらら", "シークレット", "RC04-JP009"), price=1200, stock=3),
                       "html.parser").li
    raw, row = scraper.cardrush_row_from_item(li)
    assert raw.startswith("灰流うらら【シークレット】")
    assert row["name"] == "灰流うらら" and row["price"] == 1200 and row["stock"] == 3 and not row["sold_out"]
    assert row["url"].endswith("/product/5") and row["code"] == "RC04-JP009"
    assert scraper._parse_cardrush_items("灰流うらら", [li]) == [row], "検索の読み方と同じ行"
    assert scraper._parse_cardrush_items("増殖するG", [li]) == []
    goods = BeautifulSoup(_li(6, "ストレージ【-】{-}《その他》[x]"), "html.parser").li
    assert scraper.cardrush_row_from_item(goods) is None, "【-】の封入商品・グッズは捨てる"
    sold = BeautifulSoup(_li(7, _name("灰流うらら"), stock=0), "html.parser").li
    assert scraper.cardrush_row_from_item(sold)[1]["sold_out"] is True


def test_crawl_stops_each_head_at_sold_out_page(monkeypatch):
    pages = {
        "AB01": [_page([_li(1, _name("A")), _li(2, _name("B")), _li(3, _name("A", "スーパー"))]),
                 _page([_li(4, _name("C")), _li(5, _name("D"), stock=0), _li(6, _name("E"), stock=0)]),
                 _page([_li(7, _name("F"), stock=0)] * 3)],
        "CD02": [_page([_li(8, _name("G", code="CD02-JP001"))])],
    }
    calls, _ = _patch_pages(monkeypatch, pages)
    rows_by_name, st = rc.crawl_heads(["AB01", "CD02"], page_wait=0)
    assert calls == [("AB01", 1), ("AB01", 2), ("CD02", 1)], "在庫なしが出たページ・件数の足りないページで止める"
    assert st["listed"] == 5 and st["rows"] == 5 and st["heads_read"] == 2 and st["pages"] == 3
    assert all(not r["sold_out"] for rows in rows_by_name.values() for r in rows)
    assert [h["in_stock"] for h in st["per_head"]] == [4, 1]
    monkeypatch.setattr(rc, "crawl_heads", lambda heads: (rows_by_name, dict(st)))
    monkeypatch.setattr(rc, "load_heads", lambda: ["AB01", "CD02"])
    matched, mst = rc.collect_cardrush_catalog(["A", "C", "Z"], heads_limit=None)
    assert {r["url"][-1] for r in matched["A"]} == {"1", "3"} and len(matched["C"]) == 1
    assert "Z" not in matched and mst["unmatched_products"] == 2 and mst["probe"] is False


def test_item_without_stock_field_is_not_sold_out_boundary(monkeypatch):
    """在庫の欄が無い商品（初利用の方限定のまとめ売り）は、在庫なしの範囲に入った印にしない（11-01 の全件一覧の1ページ目）"""
    no_stock = _li(9, "【当店初利用の方限定特価品】A、B").replace('<p class="stock">在庫数 1枚</p>', "")
    pages = {"AB01": [_page([_li(1, _name("A")), no_stock, _li(2, _name("B"))]),
                      _page([_li(3, _name("C")), _li(4, _name("D"), stock=0), _li(5, _name("E"), stock=0)])]}
    calls, _ = _patch_pages(monkeypatch, pages)
    _, st = rc.crawl_heads(["AB01"], page_wait=0)
    assert len(calls) == 2 and st["order_breaks"] == 0 and st["listed"] == 3 and st["skipped_items"] == 1


def test_crawl_dedupes_shifted_and_overlapping_items(monkeypatch):
    """入荷で1つ後ろへずれた商品・別の頭の検索にも出た商品は、URL で重複を除いて数える"""
    pages = {"AB01": [_page([_li(1, _name("A")), _li(2, _name("B")), _li(3, _name("C"))]),
                      _page([_li(3, _name("C")), _li(4, _name("D")), _li(5, _name("E"), stock=0)])],
             "AB1": [_page([_li(4, _name("D"))])]}
    _patch_pages(monkeypatch, pages)
    rows_by_name, st = rc.crawl_heads(["AB01", "AB1"], page_wait=0)
    assert st["duplicates"] == 2 and st["listed"] == 4
    assert len(rows_by_name[_name("C")]) == 1


def test_crawl_out_of_range_page_returns_first_page(monkeypatch):
    """存在しないページは1ページ目が返る。新しい商品が無ければその頭は読み終えている"""
    first = _page([_li(1, _name("A")), _li(2, _name("B")), _li(3, _name("C"))])
    calls, _ = _patch_pages(monkeypatch, {"AB01": [first, first, first]})
    _, st = rc.crawl_heads(["AB01"], page_wait=0)
    assert len(calls) == 2 and st["listed"] == 3 and not st["truncated_heads"]


def test_crawl_failure_stops_night(monkeypatch):
    pages = {"AB01": [_page([_li(i, _name(f"A{i}")) for i in (1, 2, 3)])]}  # 2ページ目は None（失敗）
    calls, _ = _patch_pages(monkeypatch, pages)
    _, st = rc.crawl_heads(["AB01", "CD02"], page_wait=0)
    assert st["failed"] and calls == [("AB01", 1), ("AB01", 2)], "失敗したらほかの頭も読まない"
    monkeypatch.setattr(rc, "load_heads", lambda: ["AB01", "CD02"])
    matched, _ = rc.collect_cardrush_catalog([], heads_limit=None)
    assert matched is None, "取得失敗の晩は不成立"


def test_crawl_truncated_by_pages_and_time(monkeypatch):
    pages = {"AB01": [_page([_li(10 * p + i, _name(f"A{p}{i}")) for i in (1, 2, 3)]) for p in range(10)]}
    _patch_pages(monkeypatch, pages)
    monkeypatch.setattr(rc, "HEAD_MAX_PAGES", 2)
    _, st = rc.crawl_heads(["AB01"], page_wait=0)
    assert st["truncated_heads"] == ["AB01"] and st["pages"] == 2
    monkeypatch.setattr(rc, "HEAD_MAX_PAGES", 20)
    monkeypatch.setattr(rc, "CATALOG_TIME_BUDGET", 5)
    one = _page([_li(1, _name("A"))])
    _patch_pages(monkeypatch, {"AB01": [one], "CD02": [one]}, secs={("AB01", 1): 6})
    _, st = rc.crawl_heads(["AB01", "CD02"], page_wait=0)
    assert st["time_truncated"] and st["heads_read"] == 1, "時間の上限に届いたら残りの頭を読まない"


def test_crawl_waits_longer_after_slow_page(monkeypatch):
    pages = {"AB01": [_page([_li(1, _name("A"))])], "CD02": [_page([_li(2, _name("B"))])]}
    _, sleeps = _patch_pages(monkeypatch, pages, secs={("AB01", 1): 12})
    _, st = rc.crawl_heads(["AB01", "CD02"], page_wait=1.5)
    assert sleeps[0] == 12 and sleeps[1] == 1.5, "遅い応答のあとはその分だけ待つ"
    assert st["slow_pages"] == 1 and st["page_sec_max"] == 12
    assert [h["first_sec"] for h in st["per_head"]] == [12, 0.1]


def test_collect_short_on_order_break_and_min(monkeypatch):
    monkeypatch.setattr(rc, "load_heads", lambda: ["AB01"])
    pages = {"AB01": [_page([_li(1, _name("A")), _li(2, _name("B"), stock=0)]
                            + [_li(10 + i, _name(f"C{i}")) for i in range(5)])]}
    _patch_pages(monkeypatch, pages, page_size=7)
    monkeypatch.setattr(rc, "ORDER_BREAK_TOLERANCE", 3)
    matched, st = rc.collect_cardrush_catalog(["A"], heads_limit=None)
    assert matched is None and st["order_breaks"] == 5 and st["short"], "在庫なしの後ろに在庫ありが続いたら並びが崩れた疑い"
    _patch_pages(monkeypatch, {"AB01": [_page([_li(1, _name("A")), _li(2, _name("B"), stock=0)])]}, min_in_stock=5)
    matched, st = rc.collect_cardrush_catalog(["A"], heads_limit=None)
    assert matched is None and st["short"] and st["listed"] == 1


def test_collect_probe_reads_fixed_subset_and_never_matches(monkeypatch):
    heads = [f"H{i:03d}" for i in range(20)]
    monkeypatch.setattr(rc, "load_heads", lambda: heads)
    read = []
    monkeypatch.setattr(rc, "crawl_heads", lambda hs: (read.append(list(hs)) or ({}, {"rows": 0, "per_head": []})))
    matched, st = rc.collect_cardrush_catalog(["A"], heads_limit=5)
    matched2, _ = rc.collect_cardrush_catalog(["A"], heads_limit=5)
    assert matched is None and matched2 is None and st["probe"] is True
    assert read[0] == read[1] and len(read[0]) == 5, "試し読みは毎晩同じ頭（2晩目に速くなるかを見る）"
    assert read[0] == rc.probe_heads(heads, 5)


def test_heads_file_is_loadable():
    heads = rc.load_heads()
    assert len(heads) >= 600 and "IMPH" in heads and len(set(heads)) == len(heads)
    assert all(re.fullmatch(r"[A-Z0-9]{2,5}", h) for h in heads)


def test_match_uses_raw_name_boundaries():
    """元の商品名で照合する。読み [..] の中や、別カードの名前の一部には結びつけない"""
    rows = {}
    for pid, raw in [(1, _name("サイバー・ドラゴン", reading="サイバードラゴン")),
                     (2, _name("サイバー・ドラゴン・ノヴァ", reading="サイバードラゴンノヴァ")),
                     (3, _name("ブラック・マジシャン・ガール", reading="ブラックマジシャンガール"))]:
        li = BeautifulSoup(_li(pid, raw), "html.parser").li
        r, row = scraper.cardrush_row_from_item(li)
        rows.setdefault(r, []).append(row)
    matched, st = rc.match_rows(rows, ["サイバー・ドラゴン", "ブラック・マジシャン", "サイバー・ドラゴン・ノヴァ"])
    assert [r["url"][-1] for r in matched["サイバー・ドラゴン"]] == ["1"]
    assert [r["url"][-1] for r in matched["サイバー・ドラゴン・ノヴァ"]] == ["2"]
    assert "ブラック・マジシャン" not in matched
    assert st["matched_products"] == 2 and st["unmatched_products"] == 1


# ── collect_prices との配線 ──

def _row(shop, name, url, price=100, condition="-", stock=1):
    return {"shop": shop, "name": name, "rarity": "ノーマル", "code": "AB01-JP001", "condition": condition,
            "price": price, "stock": stock, "sold_out": stock == 0, "url": url, "image": ""}


def test_prepare_cardrush_modes(monkeypatch):
    matched = {"c": [_row("カードラッシュ", "c", "r1")]}
    got = {}

    def fake(names, heads_limit=None):
        got["heads_limit"] = heads_limit
        return matched, {"listed": 1, "pages": 1, "duplicates": 0, "matched_products": 1}

    monkeypatch.setattr(cp, "collect_cardrush_catalog", fake)
    monkeypatch.setattr(cp, "CARDRUSH_CATALOG_HEADS", None)
    sel = [{"card_name": "c"}]
    both = ["遊々亭", "カードラッシュ"]
    stats = {}
    shops, m = cp._prepare_cardrush_catalog(sel, both, both, stats, mode="shadow")
    assert shops == both and m is matched and stats["カードラッシュ"]["mode"] == "shadow"
    assert got["heads_limit"] is None, "CARDRUSH_CATALOG_HEADS を渡す"
    shops, m = cp._prepare_cardrush_catalog(sel, both, both, stats, mode="on")
    assert shops == ["遊々亭"] and m is matched and stats["カードラッシュ"]["mode"] == "catalog"
    shops, m = cp._prepare_cardrush_catalog(sel, both, both, stats, mode="off")
    assert m is None and shops == both
    shops, m = cp._prepare_cardrush_catalog(sel, both, both, {}, mode="true")
    assert m is None and shops == both, "分からない値は巡回しない"
    shops, m = cp._prepare_cardrush_catalog(sel, both, ["遊々亭"], {}, mode="shadow")
    assert m is None, "カードラッシュが使えない晩は巡回しない"
    monkeypatch.setattr(cp, "collect_cardrush_catalog", lambda names, heads_limit=None: (None, {"failed": True}))
    shops, m = cp._prepare_cardrush_catalog(sel, both, both, stats, mode="on")
    assert m is None and shops == both and stats["カードラッシュ"]["mode"] == "fallback"
    monkeypatch.setattr(cp, "collect_cardrush_catalog", lambda names, heads_limit=None: 1 / 0)
    shops, m = cp._prepare_cardrush_catalog(sel, both, both, stats, mode="shadow")
    assert m is None and shops == both and stats["カードラッシュ"]["mode"] == "error"


def test_prepare_cardrush_probe(monkeypatch):
    """試し読みの晩は統計だけ残し、on でもカードラッシュはカード名で探す（日次レポートの警告も出さない）"""
    probe_stats = {"probe": True, "heads": 100, "heads_read": 100, "pages": 120, "page_sec_sum": 300.0,
                   "slow_pages": 4}
    monkeypatch.setattr(cp, "collect_cardrush_catalog", lambda names, heads_limit=None: (None, probe_stats))
    both = ["遊々亭", "カードラッシュ"]
    for mode in ("shadow", "on"):
        stats = {}
        shops, m = cp._prepare_cardrush_catalog([{"card_name": "c"}], both, both, stats, mode=mode)
        assert shops == both and m is None
        assert stats["カードラッシュ"]["mode"] == "probe" and stats["カードラッシュ"]["catalog"] is probe_stats


def test_int_env(monkeypatch):
    monkeypatch.setenv("X_TEST_INT", "all")
    assert cp._int_env("X_TEST_INT", 100, allow_all=True) is None
    monkeypatch.setenv("X_TEST_INT", "abc")
    assert cp._int_env("X_TEST_INT", 100) == 100, "読めない値は既定値（収集を起動時に落とさない）"
    monkeypatch.setenv("X_TEST_INT", " 7 ")
    assert cp._int_env("X_TEST_INT", 100) == 7


def test_compare_cardrush_shadow_ignores_sold_out_search_rows():
    a = _row("カードラッシュ", "灰流うらら", "r1", price=100)
    b = _row("カードラッシュ", "灰流うらら", "r2", price=80)
    sold = _row("カードラッシュ", "灰流うらら", "r3", price=50, stock=0)
    matched = {"c1": [a], "c2": [a, b], "c3": [a]}
    sink = {"c1": [a, sold], "c2": [a], "c3": None}
    st = cp._compare_catalog_shadow("カードラッシュ", [{"card_name": n} for n in ["c1", "c2", "c3", "c4"]],
                                    matched, sink, "2026-11-02", in_stock_only=True)
    assert st["products_only_search"] == 0, "検索の売り切れの商品は比べない（巡回は在庫ありだけ読む）"
    assert st["products_both"] == 2 and st["products_only_catalog"] == 1
    assert st["same"] == 1 and st["catalog_lower"] == 1
    assert st["search_failed_cards"] == 1 and st["not_searched_cards"] == 1


def test_collect_and_save_with_cardrush_products_and_sink(monkeypatch):
    monkeypatch.setattr(scraper, "SHOPS", [("遊々亭", lambda n: [_row("遊々亭", n, "y1")]),
                                           ("カードラッシュ", lambda n: [_row("カードラッシュ", n, "s1")])])
    monkeypatch.setattr(cp, "compare_prices", scraper.compare_prices)
    saved = []
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: saved.extend(rows) or len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    stats = {}
    cp.collect_and_save(None, "灰流うらら", "2026-11-02", ["遊々亭"], shop_stats=stats,
                        cardrush_products=[_row("カードラッシュ", "灰流うらら", "r1", price=80)])
    assert {r["shop"] for r in saved} == {"遊々亭", "カードラッシュ"}
    assert stats["カードラッシュ"] == {"ok": 1, "empty": 0, "error": 0}
    sink = {}
    cp.collect_and_save(None, "灰流うらら", "2026-11-02", ["遊々亭", "カードラッシュ"], cardrush_sink=sink)
    assert [r["url"] for r in sink["灰流うらら"]] == ["s1"]


# ── 直近2晩に行の無かったカードの検索の間引き（B1） ──

class _RowsQuery:
    def __init__(self, sb):
        self.sb = sb

    def select(self, cols):
        return self

    def eq(self, k, v):
        self.sb.filters[k] = v
        return self

    def in_(self, k, v):
        self.sb.filters[k] = list(v)
        return self

    def order(self, *a, **k):
        return self

    def range(self, a, b):
        self.sb.ranges.append((a, b))
        return self

    def execute(self):
        if self.sb.fail:
            raise TimeoutError("timed out")
        return type("R", (), {"data": self.sb.pages.pop(0) if self.sb.pages else []})()


class _RowsSb:
    def __init__(self, pages=(), fail=False):
        self.pages, self.fail, self.ranges, self.filters = list(pages), fail, [], {}

    def table(self, name):
        return _RowsQuery(self)


def test_fetch_cardrush_cards_with_rows(monkeypatch):
    monkeypatch.setattr(cp, "_PAGE_SIZE", 2)
    sb = _RowsSb([[{"card_name": "A"}, {"card_name": "A"}], [{"card_name": "B"}]])
    assert cp._fetch_cardrush_cards_with_rows(sb, ["2026-11-01", "2026-10-31"]) == {"A", "B"}
    assert sb.ranges == [(0, 1), (2, 3)], "1000行ずつ最後まで読む"
    assert sb.filters == {"shop": "カードラッシュ", "recorded_at": ["2026-11-01", "2026-10-31"]}
    assert cp._fetch_cardrush_cards_with_rows(_RowsSb(fail=True), ["2026-11-01"]) is None


def _rest_day_for(name, rest_days, today):
    """そのカードが今夜検索する日かどうか（crc32 の日ずらし）。"""
    import zlib
    from datetime import date
    return zlib.crc32(name.encode("utf-8")) % rest_days == date.fromisoformat(today).toordinal() % rest_days


def test_cardrush_rests_rules():
    today = "2026-11-02"
    names = [f"カード{i}" for i in range(30)]
    resting = [n for n in names if not _rest_day_for(n, 3, today)]
    searching = [n for n in names if _rest_day_for(n, 3, today)]
    assert resting and searching
    yesterday_collected = "2026-11-01T06:00:00+09:00"
    n = resting[0]
    card = {"card_name": n, "last_collected_at": yesterday_collected}
    assert cp._cardrush_rests(card, set(), today, 3) is True, "昨日集めたのに2晩とも行が無い → 休む"
    assert cp._cardrush_rests(card, {n}, today, 3) is False, "直近に行がある → 休まない"
    assert cp._cardrush_rests({**card, "last_collected_at": "2026-10-30T06:00:00+09:00"}, set(), today, 3) is False, \
        "昨日集めていない（打ち切り・保存0件）→ 休まない"
    assert cp._cardrush_rests({**card, "last_collected_at": None}, set(), today, 3) is False, "初めてのカードは休まない"
    assert cp._cardrush_rests(card, set(), today, 1) is False, "1以下は間引かない"
    assert cp._cardrush_rests({"card_name": searching[0], "last_collected_at": yesterday_collected},
                              set(), today, 3) is False, "3日に1回は検索する"
    # 3日続けて見ると、どのカードもちょうど1回は検索する
    for name in names[:10]:
        c = {"card_name": name, "last_collected_at": "2026-11-05T06:00:00+09:00"}
        days = [f"2026-11-0{d}" for d in (6, 7, 8)]
        flags = [cp._cardrush_rests({**c, "last_collected_at": f"{(d[:8])}{int(d[8:]) - 1:02d}T06:00:00+09:00"},
                                    set(), d, 3) for d in days]
        assert flags.count(False) == 1


def test_cardrush_rest_set(monkeypatch):
    today = "2026-11-02"
    sel = [{"card_name": f"カード{i}", "last_collected_at": "2026-11-01T06:00:00+09:00"} for i in range(30)]
    sb = _RowsSb([[{"card_name": "カード0"}]])
    rest, st = cp._cardrush_rest_set(sb, sel, today, rest_days=3)
    assert "カード0" not in rest and st["cards"] == len(rest) > 0 and not st["read_failed"]
    assert sb.filters["recorded_at"] == ["2026-11-01", "2026-10-31"], "昨日と一昨日を読む"
    rest, st = cp._cardrush_rest_set(_RowsSb(fail=True), sel, today, rest_days=3)
    assert rest == set() and st["read_failed"], "読めない晩は間引かない"
    rest, st = cp._cardrush_rest_set(_RowsSb(), sel, today, rest_days=1)
    assert rest == set() and st["cards"] == 0


# ── main の配線 ──

from tests.test_torecolo_catalog import _PrevSb  # noqa: E402


def _run_main(monkeypatch, mode, catalog_ok=True, rest=(), probe=False):
    names = ["c0", "c1"]
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
    monkeypatch.setattr(cp, "SHOPS", [(s, None) for s in ["遊々亭", "カードラッシュ"]])
    monkeypatch.setattr(cp, "SKIP_SHOPS_IN_CI", set())
    monkeypatch.setattr(cp, "check_shop_availability", lambda s: list(s))
    monkeypatch.setattr(cp, "TORECOLO_CATALOG_MODE", "off")
    monkeypatch.setattr(cp, "KANABELL_CATALOG_MODE", "off")
    monkeypatch.setattr(cp, "CARDRUSH_CATALOG_MODE", mode)
    monkeypatch.setattr(cp, "_cardrush_rest_set",
                        lambda sb, selected, today: (set(rest), {"days": 3, "cards": len(rest), "read_failed": False}))

    def fake_catalog(ns, heads_limit=None):
        if probe:
            return None, {"probe": True, "heads": 100, "heads_read": 100, "pages": 1, "page_sec_sum": 1.0,
                          "slow_pages": 0}
        return (({n: [_row("カードラッシュ", n, f"r-{n}")] for n in ns} if catalog_ok else None),
                {"listed": len(ns), "pages": 1, "duplicates": 0, "rows": len(ns), "matched_products": len(ns)})

    monkeypatch.setattr(cp, "collect_cardrush_catalog", fake_catalog)
    calls = []

    def fake_collect(sb, card_name, today, shop_names, **kw):
        calls.append((card_name, list(shop_names), kw))
        if "cardrush_sink" in kw and "カードラッシュ" in shop_names:
            kw["cardrush_sink"][card_name] = [_row("カードラッシュ", card_name, f"r-{card_name}")]
        return 1

    monkeypatch.setattr(cp, "collect_and_save", fake_collect)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    reports = []
    monkeypatch.setattr(cp, "send_daily_report", lambda *a, **k: reports.append((a, k)))
    monkeypatch.setattr(cp, "cleanup_old_data", lambda sb: None)
    monkeypatch.setattr(cp, "get_host_request_counts", lambda: {})
    runs = []
    monkeypatch.setattr(cp, "record_collection_run", lambda sb, kind, *a, **k: runs.append(k) or True)
    monkeypatch.setattr(cp, "watch_suspended_shops", lambda: None)
    cp.main()
    return calls, runs[-1]["shop_stats"]


def test_main_cardrush_shadow(monkeypatch):
    calls, stats = _run_main(monkeypatch, "shadow")
    assert calls and all("カードラッシュ" in shops and "cardrush_sink" in kw and "cardrush_products" not in kw
                         for _, shops, kw in calls)
    st = stats["カードラッシュ"]
    assert st["mode"] == "shadow" and st["shadow"]["same"] == 2 and st["shadow"]["products_both"] == 2
    assert st["rest"]["cards"] == 0


def test_main_cardrush_on(monkeypatch):
    calls, stats = _run_main(monkeypatch, "on")
    assert calls
    for card, shops, kw in calls:
        assert shops == ["遊々亭"], "カードラッシュは検索しない"
        assert [r["url"] for r in kw["cardrush_products"]] == [f"r-{card}"]
        assert "cardrush_sink" not in kw
    assert stats["カードラッシュ"]["mode"] == "catalog" and "rest" not in stats["カードラッシュ"], \
        "on の晩は検索しないので間引きも読まない"


def test_main_cardrush_fallback_searches(monkeypatch):
    calls, stats = _run_main(monkeypatch, "on", catalog_ok=False)
    assert calls and all("カードラッシュ" in shops and "cardrush_products" not in kw for _, shops, kw in calls)
    assert stats["カードラッシュ"]["mode"] == "fallback"


def test_main_cardrush_probe_and_rest(monkeypatch):
    """試し読みの晩: カードラッシュはカード名で探し、間引くカードだけ検索から外す"""
    calls, stats = _run_main(monkeypatch, "shadow", probe=True, rest={"c1"})
    shops_by_card = {card: shops for card, shops, _ in calls}
    assert "カードラッシュ" in shops_by_card["c0"] and shops_by_card["c1"] == ["遊々亭"]
    st = stats["カードラッシュ"]
    assert st["mode"] == "probe" and st["rest"]["cards"] == 1
    assert "skipped" not in st, "間引いた数を check_collection の skipped（連続失敗）に入れない"
