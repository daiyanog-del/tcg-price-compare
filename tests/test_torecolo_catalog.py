"""
tests/test_torecolo_catalog.py — トレコロの在庫一覧の巡回（torecolo_catalog.py・2026-10-31）

- scrape_torecolo は extra_query を URL に足し、ページの間は page_wait 秒待つ（既定は従来どおり）
- crawl_catalog: 系列ごとに読み、URL で重複を除く。取得の失敗・上限に届いた系列は記録する
- collect_torecolo_catalog: 失敗・上限・0件なら不成立（None）
- crawl_catalog: 系列の総件数の表示と読んだ件数を突き合わせ、足りない系列は不成立。1つ失敗したらその晩はやめる
- collect_torecolo_catalog: 新しい系列が出たら不成立
- collect_prices: TORECOLO_CATALOG_MODE が shadow なら保存せず、ループで得たトレコロの結果と巡回の値を比べる
- on（2026-11-01）: カードごとに巡回の商品をほかの店の行と合わせて保存する（型番の補完・カードラボの判定材料に入る）。
  前日のレアリティが巡回に無いカードはカード名で検索し直す。想定外の例外で抜けたら、処理したカードまでで止める
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
    assert loop == ["遊々亭"] and matched, "on はトレコロをループから外す"
    assert stats["トレコロCB"]["mode"] == "catalog" and stats["トレコロCB"]["ok"] == 0
    assert stats["トレコロCB"]["recheck"] == {"cards": 0, "prev_cards": None, "prev_read_failed": False}
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


# ── on: カードごとに巡回の商品をほかの店の行と合わせる ──

def _shop_item(shop, name, code, rarity="ウルトラ", price=500, url="y"):
    return {**_item(name, url, price=price, rarity=rarity), "shop": shop, "code": code}


def test_compare_prices_extra_results_get_codes_from_other_shops(monkeypatch):
    """巡回の商品（検索しない）も、取得したほかの店の行と合わせて型番の補完を通る（reviewer 高1）"""
    monkeypatch.setattr(scraper, "SHOPS", [("遊々亭", lambda name: [_shop_item("遊々亭", name, "RC04-JP009")])])
    extra = [_shop_item("トレコロCB", "灰流うらら", "RC04-JP009UR", url="t1"),
             _shop_item("トレコロCB", "灰流うらら", "12345678", rarity="ノーマル", url="t2")]
    status = {}
    rows = scraper.compare_prices("灰流うらら", shop_names=["遊々亭"], status_out=status, extra_results=extra)
    by_url = {r["url"]: r for r in rows}
    assert by_url["t1"]["code"] == "RC04-JP009", "接尾辞つきの値をほかの店の型番で直す"
    assert by_url["t2"]["code"] == "", "決まらない値は空にする（そのまま残さない）"
    assert set(status) == {"遊々亭"}, "巡回の商品は取得の状況に書かない"
    # ほかの店が無い晩も、巡回の商品だけで補完を通して返す
    rows = scraper.compare_prices("灰流うらら", shop_names=[], extra_results=extra)
    assert {r["url"] for r in rows} == {"t1", "t2"} and all("code_source" in r for r in rows)
    assert scraper.compare_prices("灰流うらら", shop_names=[], extra_results=[]) == []


def test_collect_and_save_with_catalog_products(monkeypatch):
    """on: トレコロは検索せず巡回の商品を使い、ほかの店と同じ行として保存する。判定材料にも入る（reviewer 中2）"""
    fetched = []

    def yuyu(name):
        fetched.append(name)
        return [_shop_item("遊々亭", name, "RC04-JP009")]

    monkeypatch.setattr(scraper, "SHOPS", [("遊々亭", yuyu), ("トレコロCB", lambda n: 1 / 0)])
    monkeypatch.setattr(cp, "compare_prices", scraper.compare_prices)
    saved_rows = []
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: saved_rows.extend(rows) or len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    products = [_shop_item("トレコロCB", "灰流うらら", "RC04-JP009UR", price=400, url="t1"),
                _shop_item("トレコロCB", "灰流うらら", "RC04-JP010", rarity="スーパー", price=300, url="t2")]
    stats, art = {}, {}
    n = cp.collect_and_save(None, "灰流うらら", "2026-11-02", ["遊々亭"], shop_stats=stats, art_contexts=art,
                            torecolo_products=products)
    tore = {r["rarity"]: r for r in saved_rows if r["shop"] == "トレコロCB"}
    assert n == 3 and set(tore) == {"ウルトラ", "スーパー"}
    assert tore["ウルトラ"]["code"] == "RC04-JP009", "型番を補完してから保存する"
    assert fetched == ["灰流うらら"], "トレコロはカード名で検索しない"
    assert stats["トレコロCB"] == {"ok": 1, "empty": 0, "error": 0}
    assert art["灰流うらら"]["overframe"]["RC04-JP010"]["plain"] == {"スーパー"}, "カードラボの判定材料にトレコロの行が入る"
    # 巡回に商品が無いカードは0件として数える（取得失敗ではない）
    cp.collect_and_save(None, "増殖するG", "2026-11-02", ["遊々亭"], shop_stats=stats, torecolo_products=[])
    assert stats["トレコロCB"] == {"ok": 1, "empty": 1, "error": 0}


def test_torecolo_card_plan():
    matched = {"灰流うらら": [_item("灰流うらら", "u1", price=100, rarity="ノーマル"),
                         _item("灰流うらら", "u2", price=900, rarity="シークレット")]}
    loop = ["遊々亭"]
    # 前日のレアリティが全部巡回にあり、値も上がっていない → 巡回の商品を使う
    prev = {"灰流うらら": {"ノーマル": (100, 100), "シークレット": (950, 950)}}
    shops, products, recheck = cp._torecolo_card_plan("灰流うらら", loop, matched, prev, "2026-11-02")
    assert (shops, recheck) == (loop, False) and products == matched["灰流うらら"]
    # 前日にあったレアリティが巡回に無い（読み落としか売り切れか分からない）→ トレコロもカード名で検索する
    prev = {"灰流うらら": {"ノーマル": (100, 100), "ウルトラ": (300, 300)}}
    shops, products, recheck = cp._torecolo_card_plan("灰流うらら", loop, matched, prev, "2026-11-02")
    assert (shops, products, recheck) == (["遊々亭", "トレコロCB"], None, True)
    assert loop == ["遊々亭"], "ループの店の一覧は書き換えない"
    shops, products, recheck = cp._torecolo_card_plan(
        "増殖するG", loop, matched, {"増殖するG": {"ノーマル": (80, 80)}}, "2026-11-02")
    assert recheck and products is None, "巡回に1件も無く、前日はあったカードも検索し直す"
    # 前日の行が無いカード・前日を読めなかった晩は検索し直さない
    assert cp._torecolo_card_plan("増殖するG", loop, matched, {}, "2026-11-02") == (loop, [], False)
    assert cp._torecolo_card_plan("灰流うらら", loop, matched, None, "2026-11-02")[2] is False
    # 売り切れの商品しか無いレアリティは、巡回に無いのと同じ（保存の行にならない）
    sold = {"灰流うらら": [{**_item("灰流うらら", "u3", rarity="ウルトラ"), "stock": 0, "sold_out": True}]}
    assert cp._torecolo_card_plan("灰流うらら", loop, sold, {"灰流うらら": {"ウルトラ": (300, 300)}}, "2026-11-02")[2]
    # 巡回だけにある（前日に無い）レアリティでは検索し直さない（カード名の検索に出ないキズあり品があるため）
    assert not cp._torecolo_card_plan("灰流うらら", loop, matched, {"灰流うらら": {"ノーマル": (100, 100)}}, "2026-11-02")[2]


def test_torecolo_looks_missed_same_rarity():
    """同じレアリティの中で最安の商品・通常品だけが落ちても、検索し直しにする（reviewer 高1）"""
    def rows(m, a, rarity="ウルトラ"):
        return [{"rarity": rarity, "min_price": m, "min_price_any": a}]
    # 300円が落ちて800円だけ残った（偽の値上がり）
    assert cp._torecolo_looks_missed({"ウルトラ": (300, 300)}, rows(800, 800))
    # 状態を問わない最安だけ上がった（安いキズありが落ちた）
    assert cp._torecolo_looks_missed({"ウルトラ": (500, 400)}, rows(500, 450))
    # 前日は通常品500・キズあり400 → 今日は通常品が無くキズありだけ（min_price が min_price_any に落ちる）
    assert cp._torecolo_looks_missed({"ウルトラ": (500, 400)}, rows(400, 400))
    # 値下がり・同じ値は巡回の値を使う
    assert not cp._torecolo_looks_missed({"ウルトラ": (500, 400)}, rows(480, 380))
    assert not cp._torecolo_looks_missed({"ウルトラ": (500, 500)}, rows(500, 500))
    # 前日が通常品だけ・今日がキズありだけは分からない（値下がりに見える。受け入れた穴）
    assert not cp._torecolo_looks_missed({"ウルトラ": (500, 500)}, rows(400, 400))
    # 値が欠けた前日の行では値を比べない（レアリティの有無だけ見る）
    assert not cp._torecolo_looks_missed({"ウルトラ": (None, 300)}, rows(800, 800))


def test_torecolo_card_plan_overframe_mark_matches_saved_rarity():
    """商品名に OF の印があれば保存の行は OF のレアリティになるので、判定もそろえる（毎晩の検索し直しにしない。reviewer 低4）"""
    matched = {"真紅眼の超越黒竜": [_item("真紅眼の超越黒竜(オーバーフレーム)", "u1", price=59800, rarity="プリシク")]}
    prev = {"真紅眼の超越黒竜": {"OFプリシク": (59800, 59800)}}
    assert cp._torecolo_card_plan("真紅眼の超越黒竜", ["遊々亭"], matched, prev, "2026-11-02")[2] is False


class _PrevQuery:
    def __init__(self, sb):
        self.sb = sb
        self.filters = {}

    def select(self, cols):
        return self

    def eq(self, k, v):
        self.filters[k] = v
        return self

    def order(self, *a, **k):
        return self

    def range(self, a, b):
        self.sb.ranges.append((a, b))
        self.sb.filters.append(dict(self.filters))
        return self

    def execute(self):
        if self.sb.fail:
            raise TimeoutError("timed out")
        return type("R", (), {"data": self.sb.pages.pop(0) if self.sb.pages else []})()


class _PrevSb:
    def __init__(self, pages=(), fail=False):
        self.pages, self.fail, self.ranges, self.filters = list(pages), fail, [], []

    def table(self, name):
        return _PrevQuery(self)


def test_fetch_torecolo_prev_rows(monkeypatch):
    monkeypatch.setattr(cp, "_PAGE_SIZE", 2)
    sb = _PrevSb([[{"card_name": "A", "rarity": "ノーマル", "min_price": 100, "min_price_any": 80},
                   {"card_name": "A", "rarity": "ウルトラ", "min_price": 300, "min_price_any": 300}],
                  [{"card_name": "B", "rarity": "ノーマル", "min_price": 10, "min_price_any": 10}]])
    assert cp._fetch_torecolo_prev_rows(sb, "2026-11-01") == {
        "A": {"ノーマル": (100, 80), "ウルトラ": (300, 300)}, "B": {"ノーマル": (10, 10)}}
    assert sb.ranges == [(0, 1), (2, 3)], "1000行ずつ最後まで読む"
    assert sb.filters[0] == {"shop": "トレコロCB", "recorded_at": "2026-11-01"}
    assert cp._fetch_torecolo_prev_rows(_PrevSb(fail=True), "2026-11-01") is None, "読めなければ None"


# ── main の通し（on の配線・例外で抜けたときの処理位置） ──

def _run_main(monkeypatch, n_cards, collect=None, mode="on", prev=None, clabo=False, avail=None,
              catalog_ok=True, real_collect=False, cold=()):
    names = [f"c{i}" for i in range(n_cards)]
    monkeypatch.setattr(cp, "get_supabase", lambda: _PrevSb())
    monkeypatch.setattr(cp, "sync_latest_packs", lambda sb: ({n for n in names if n not in cold}, set()))
    for fn in ("sync_meta_decks", "sync_regulation"):
        monkeypatch.setattr(cp, fn, lambda sb: set())
    monkeypatch.setattr(cp, "sync_searched_cards", lambda sb, **k: set())
    for fn in ("sync_recipe_decks", "sync_theme_wiki_cards", "sync_trending_cards", "save_pack_list"):
        monkeypatch.setattr(cp, fn, lambda sb: None)
    monkeypatch.setattr(cp, "sync_remake_themes", lambda sb, new: None)
    monkeypatch.setattr(cp, "fetch_tracked_cards_with_meta",
                        lambda sb: [{"card_name": n, "last_collected_at": None} for n in names])
    shops = ["遊々亭", "トレコロCB"] + (["カードラボ"] if clabo else [])
    monkeypatch.setattr(cp, "SHOPS", [(s, None) for s in shops])
    monkeypatch.setattr(cp, "SKIP_SHOPS_IN_CI", set())
    monkeypatch.setattr(cp, "check_shop_availability", lambda s: list(avail if avail is not None else s))
    monkeypatch.setattr(cp, "TORECOLO_CATALOG_MODE", mode)
    monkeypatch.setattr(cp, "collect_torecolo_catalog", lambda ns: (
        {n: [_item(n, f"t-{n}")] for n in ns} if catalog_ok else None,
        {"products": len(ns), "matched_products": len(ns)}))
    clabo_saved = []
    if clabo:
        monkeypatch.setattr(cp, "collect_clabo_catalog", lambda ns: ({n: [] for n in ns}, {"products": 1}))
        monkeypatch.setattr(cp, "_save_clabo_catalog_rows",
                            lambda sb, processed, *a, **k: clabo_saved.extend(c["card_name"] for c in processed) or 0)
    monkeypatch.setattr(cp, "_fetch_torecolo_prev_rows", lambda sb, day: prev)
    calls = []
    real = cp.collect_and_save

    def fake_collect(sb, card_name, today, shop_names, **kw):
        calls.append((card_name, list(shop_names), kw))
        if real_collect:
            return real(sb, card_name, today, shop_names, **kw)
        return collect(card_name) if collect else 1

    monkeypatch.setattr(cp, "collect_and_save", fake_collect)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    monkeypatch.setattr(cp, "send_daily_report", lambda *a, **k: None)
    monkeypatch.setattr(cp, "cleanup_old_data", lambda sb: None)
    monkeypatch.setattr(cp, "get_host_request_counts", lambda: {})
    runs = []
    monkeypatch.setattr(cp, "record_collection_run", lambda sb, kind, *a, **k: runs.append(k) or True)
    monkeypatch.setattr(cp, "watch_suspended_shops", lambda: None)
    err = None
    try:
        cp.main()
    except BaseException as e:  # noqa: BLE001 — 例外も SystemExit も呼び元で確かめる
        err = e
    return calls, runs, clabo_saved, err


def test_main_on_passes_catalog_products_per_card(monkeypatch):
    calls, runs, _, err = _run_main(monkeypatch, 3, prev={"c1": {"ウルトラ": (300, 300)}})
    assert err is None
    by_card = {c: (shops, kw) for c, shops, kw in calls}
    assert by_card["c0"][0] == ["遊々亭"] and [p["url"] for p in by_card["c0"][1]["torecolo_products"]] == ["t-c0"]
    assert "torecolo_sink" not in by_card["c0"][1], "on では比べない"
    assert by_card["c1"][0] == ["遊々亭", "トレコロCB"] and "torecolo_products" not in by_card["c1"][1], \
        "前日のレアリティが巡回に無いカードはカード名で検索し直す"
    st = runs[-1]["shop_stats"]["トレコロCB"]
    assert st["mode"] == "catalog" and st["recheck"] == {"cards": 1, "prev_cards": 1, "prev_read_failed": False}


def test_main_on_prev_read_failed_is_recorded(monkeypatch):
    calls, runs, _, err = _run_main(monkeypatch, 2, prev=None)
    assert err is None and all("torecolo_products" in kw for _, _, kw in calls)
    assert runs[-1]["shop_stats"]["トレコロCB"]["recheck"]["prev_read_failed"] is True


def test_main_on_runs_loop_when_other_shops_are_gone(monkeypatch):
    """ほかの店が全滅した晩（カードラボは巡回）も、トレコロの巡回の商品はカードごとに保存する"""
    calls, runs, clabo_saved, err = _run_main(monkeypatch, 2, clabo=True, avail=["トレコロCB", "カードラボ"])
    assert err is None and [(c, shops) for c, shops, _ in calls] == [("c0", []), ("c1", [])]
    assert all(kw["torecolo_products"] for _, _, kw in calls) and sorted(clabo_saved) == ["c0", "c1"]


def test_main_shadow_still_compares(monkeypatch):
    calls, runs, _, err = _run_main(monkeypatch, 2, mode="shadow")
    assert err is None
    assert all(shops == ["遊々亭", "トレコロCB"] and "torecolo_sink" in kw and "torecolo_products" not in kw
               for _, shops, kw in calls)
    assert "shadow" in runs[-1]["shop_stats"]["トレコロCB"]


def test_main_unexpected_exception_saves_catalog_only_for_processed(monkeypatch):
    """想定外の例外でループを抜けたら、カタログの保存は処理し終えたカードまで（reviewer 中5）"""
    def collect(card_name):
        if card_name == "c2":
            raise ValueError("想定外")
        return 1

    calls, _, clabo_saved, err = _run_main(monkeypatch, 5, collect=collect, clabo=True)
    assert isinstance(err, ValueError), "例外はそのまま上がる"
    assert sorted(clabo_saved) == ["c0", "c1"], "例外の出たカードと後ろのカードにはカタログの行を書かない"


def test_main_on_counts_shop_stats_with_real_collect(monkeypatch):
    """on で実際の collect_and_save・compare_prices を通す: ほかの店が無い晩もトレコロの行を保存し、ok／empty を数える"""
    saved = []
    monkeypatch.setattr(cp, "upsert_price_rows", lambda sb, rows, **k: saved.extend(rows) or len(rows))
    monkeypatch.setattr(cp, "upsert_price_art_rows", lambda sb, rows, **k: None)
    monkeypatch.setattr(cp, "compare_prices", scraper.compare_prices)
    monkeypatch.setattr(scraper, "SHOPS", [])
    calls, runs, _, err = _run_main(monkeypatch, 2, real_collect=True, avail=["トレコロCB"])
    assert err is None
    assert sorted(r["card_name"] for r in saved if r["shop"] == "トレコロCB") == ["c0", "c1"]
    st = runs[-1]["shop_stats"]["トレコロCB"]
    assert (st["ok"], st["empty"], st["error"], st["mode"]) == (2, 0, 0, "catalog")


def test_main_on_catalog_not_ok_falls_back_to_search(monkeypatch):
    calls, runs, _, err = _run_main(monkeypatch, 2, catalog_ok=False)
    assert err is None
    assert all(shops == ["遊々亭", "トレコロCB"] and "torecolo_products" not in kw and "torecolo_sink" not in kw
               for _, shops, kw in calls), "不成立の晩はカード名で探す"
    assert runs[-1]["shop_stats"]["トレコロCB"]["mode"] == "fallback"


def test_main_on_time_budget_cutoff(monkeypatch):
    """時間予算で周辺カードを打ち切った晩は、打ち切ったカードにトレコロもカードラボも書かない"""
    monkeypatch.setattr(cp, "TIME_BUDGET_SEC", -1)
    calls, runs, clabo_saved, err = _run_main(monkeypatch, 3, clabo=True, cold=("c1", "c2"))
    assert err is None
    assert [c for c, _, _ in calls] == ["c0"] and clabo_saved == ["c0"]
    assert runs[-1]["cutoff_count"] == 2


def test_daily_report_warns_torecolo_recheck(monkeypatch):
    sent = []
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/hook")

    class _Req:
        @staticmethod
        def post(url, json=None, timeout=None):
            sent.append(json["content"])

    import requests
    monkeypatch.setattr(requests, "post", _Req.post)
    base = {"ok": 3, "empty": 0, "error": 0, "mode": "catalog"}
    for recheck, expect in (({"cards": 2, "prev_cards": None, "prev_read_failed": True}, "前日の行を読めず"),
                            ({"cards": 0, "prev_cards": 0, "prev_read_failed": False}, "前日の行が0枚"),
                            ({"cards": 5, "prev_cards": 300, "prev_read_failed": False}, "検索し直し: 5枚")):
        sent.clear()
        cp.send_daily_report(_PrevSb(), "2026-11-02", 3, 0, 0, 10.0, {"トレコロCB": {**base, "recheck": recheck}})
        assert sent and expect in sent[0]
    assert "警告: トレコロ" not in sent[0]
