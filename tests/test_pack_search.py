"""
tests/test_pack_search.py — 新弾の収録カードを型番の頭でまとめて取る（pack_search.py・2026-10-31）

- 型番の頭はいちばん多いもの。収録の半分に届かなければ決めない
- 商品は収録カードに振り分ける（複数当たればいちばん長い名前）。このパックの型番でない商品は捨てる
- 取得に失敗した店は failed（その店は全カードをカード名で探す）
- name_filter_off の中だけカード名の一致を見ない（外には漏れない）
- まんぞく屋は max_pages で次のページも読む（既定は1ページのまま）
- collect_featured: 型番で取れた (カード, 店) はカードごとの検索をしない。再録・失敗した店・見つからないカードはカード名で探す
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bs4 import BeautifulSoup

import collect_featured
import pack_search
import scraper


def _item(name, code, price=100, rarity="ノーマル", shop="遊々亭"):
    return {"shop": shop, "name": name, "code": code, "rarity": rarity, "price": price,
            "sold_out": False, "stock": 1, "condition": "-", "url": "", "image": ""}


# ── 型番の頭 ──

def test_pack_code_head_majority():
    numbers = {"A": "IMPH-JP001", "B": "IMPH-JP002", "C": "IMPH-JP003", "D": "SD12-JP001"}
    assert pack_search.pack_code_head(numbers) == "IMPH"


def test_pack_code_head_undecided():
    assert pack_search.pack_code_head({}) is None
    # 半分に届かない（いちばん多い頭が1/3）
    assert pack_search.pack_code_head({"A": "AAAA-JP001", "B": "BBBB-JP001", "C": "CCCC-JP001"}) is None


# ── 振り分け ──

def test_assign_to_cards_longest_name_and_unassigned():
    names = ["白鴉の騎士アン", "白鴉の森の炎魔", "秋"]
    items = [_item("白鴉の騎士アン", "IMPH-JP011"), _item("白鴉の森の炎魔", "IMPH-JP009"),
             _item("秋", "IMPH-JP071"), _item("篝火", "IMPH-JP090")]
    by_card, unassigned = pack_search.assign_to_cards(items, names)
    assert set(by_card) == {"白鴉の騎士アン", "白鴉の森の炎魔", "秋"}
    assert unassigned == 1  # 篝火（再録など収録リストに無いもの）


def test_search_pack_shop_filters_other_packs_and_turns_name_filter_off():
    seen = {}

    def fake_shop(query):
        seen["query"] = query
        # スクレイパーの中ではカード名（ここでは検索語）との一致を見ないこと
        seen["filter_off"] = scraper.is_target_card(query, "白鴉の騎士アン")
        return [_item("白鴉の騎士アン", "IMPH-JP011"), _item("白鴉の騎士アン", "LEDE-JP011", price=50)]

    res = pack_search.search_pack_shop("遊々亭", "IMPH", ["白鴉の騎士アン"], fn=fake_shop)
    assert seen == {"query": "IMPH", "filter_off": True}
    assert not res.failed
    assert [it["code"] for it in res.by_card["白鴉の騎士アン"]] == ["IMPH-JP011"], "別のパックの版は使わない"
    # 外では今までどおりカード名の一致を見る
    assert scraper.is_target_card("IMPH-JP", "白鴉の騎士アン") is False


def test_search_pack_shop_failed_on_fetch_errors():
    def fake_shop(query):
        scraper._note_fetch_error()
        return [_item("白鴉の騎士アン", "IMPH-JP011")]

    res = pack_search.search_pack_shop("カードラッシュ", "IMPH", ["白鴉の騎士アン"], fn=fake_shop)
    assert res.failed and res.by_card == {}


# ── まんぞく屋のページ送り ──

def _manzoku_page(names_codes, next_page=None):
    lis = "".join(
        f'<li><a href="/products/detail/{i}">{code} 《{name}》 ノーマル ￥{100 + i}</a> 在庫: 2</li>'
        for i, (name, code) in enumerate(names_codes))
    pager = f'<a href="/products/list?pageno={next_page}">次へ</a>' if next_page else ""
    return BeautifulSoup(f"<ul>{lis}</ul>{pager}", "html.parser")


def test_manzoku_reads_next_pages_only_when_asked(monkeypatch):
    urls = []
    pages = {1: _manzoku_page([("白鴉の騎士アン", "IMPH-JP011")], next_page=2),
             2: _manzoku_page([("白鴉の森の炎魔", "IMPH-JP009")])}

    def fake_get(url, *a, **k):
        urls.append(url)
        return pages[2 if "pageno=2" in url else 1]

    monkeypatch.setattr(scraper, "safe_get", fake_get)
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)
    monkeypatch.setattr(scraper.time, "sleep", lambda *_: None)
    with scraper.name_filter_off():
        got = scraper.scrape_manzoku("IMPH-JP", max_pages=10)
    assert [r["name"] for r in got] == ["白鴉の騎士アン", "白鴉の森の炎魔"]
    assert len(urls) == 2, "次のページが無ければ止まる"

    urls.clear()
    assert [r["name"] for r in scraper.scrape_manzoku("白鴉の騎士アン")] == ["白鴉の騎士アン"]
    assert len(urls) == 1, "既定は1ページだけ（カード名の検索は今までどおり）"


# ── collect_featured への組み込み ──

def test_plan_pack_search_skips_reprints_and_failed_shops(monkeypatch):
    import pack_scraper
    monkeypatch.setattr(pack_scraper, "fetch_pack_card_numbers",
                        lambda name: {"新A": "IMPH-JP001", "新B": "IMPH-JP002", "再録C": "IMPH-JP003"})
    monkeypatch.setattr(collect_featured, "find_reprints", lambda sb, names, start: {"再録C"})
    monkeypatch.setattr(collect_featured.time, "sleep", lambda *_: None)
    asked = []

    def fake_search(shop, head, cards):
        asked.append((shop, head, list(cards)))
        if shop == "カードラッシュ":
            return pack_search.PackShopResult(shop, failed=True)
        return pack_search.PackShopResult(shop, by_card={"新A": [_item("新A", "IMPH-JP001", shop=shop)],
                                                         "再録C": [_item("再録C", "IMPH-JP003", shop=shop)]})

    monkeypatch.setattr(collect_featured, "search_pack_shop", fake_search)
    monkeypatch.setattr(collect_featured, "drop_higher_than_today", lambda sb, plan, today, dropped=None: plan)
    plan = collect_featured.plan_pack_search(
        None, {"pack_name": "P", "start_date": "2026-10-31"}, ["新A", "新B", "再録C"],
        ["遊々亭", "カードラッシュ", "トレコロCB"], "2026-10-31")
    assert [a[0] for a in asked] == ["遊々亭", "カードラッシュ"], "型番で探す店だけ・使える店だけ（トレコロは対象外）"
    # 振り分けの候補には再録の名前も入れ（新規の名前へ流れないように）、再録に当たった商品は捨てる
    assert all(a[1] == "IMPH" and a[2] == ["新A", "新B", "再録C"] for a in asked)
    assert plan == {"新A": {"遊々亭": [_item("新A", "IMPH-JP001", shop="遊々亭")]}}


def test_plan_pack_search_off_when_head_or_reprints_unknown(monkeypatch):
    import pack_scraper
    monkeypatch.setattr(collect_featured, "search_pack_shop",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("探さない")))
    pack = {"pack_name": "P", "start_date": "2026-10-31"}
    monkeypatch.setattr(pack_scraper, "fetch_pack_card_numbers", lambda name: {})
    assert collect_featured.plan_pack_search(None, pack, ["新A"], ["遊々亭"]) == {}
    monkeypatch.setattr(pack_scraper, "fetch_pack_card_numbers", lambda name: {"新A": "IMPH-JP001"})
    monkeypatch.setattr(collect_featured, "find_reprints", lambda sb, names, start: None)
    assert collect_featured.plan_pack_search(None, pack, ["新A"], ["遊々亭"]) == {}
    # 今日の行と比べられなければ使わない
    monkeypatch.setattr(collect_featured, "find_reprints", lambda sb, names, start: set())
    monkeypatch.setattr(collect_featured, "search_pack_shop", lambda shop, head, cards:
                        pack_search.PackShopResult(shop, by_card={"新A": [_item("新A", "IMPH-JP001")]}))
    monkeypatch.setattr(collect_featured, "drop_higher_than_today", lambda sb, plan, today, dropped=None: None)
    assert collect_featured.plan_pack_search(None, pack, ["新A"], ["遊々亭"], "2026-10-31") == {}
    # 手動指定などで発売日が無ければ使わない
    assert collect_featured.plan_pack_search(None, {"pack_name": "P"}, ["新A"], ["遊々亭"]) == {}


def test_main_uses_pack_items_and_searches_the_rest_by_name(monkeypatch):
    calls = {}

    def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None, pack_items=None, pack_code=""):
        calls[card] = (list(shops), [it["shop"] for it in pack_items or []])
        return 0, []

    monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
    monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
    monkeypatch.setattr(collect_featured, "_resolve_pack",
                        lambda sb, a: {"pack_name": "P", "start_date": "2026-10-31"})
    monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: ["新A", "新B", "再録C"])
    monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["遊々亭", "カードラボ"])
    monkeypatch.setattr(collect_featured, "fetch_cards_collected_today", lambda *a: set())
    monkeypatch.setattr(collect_featured, "plan_pack_search",
                        lambda sb, pack, cards, shops, today=None, dropped=None, head=None: {"新A": {"遊々亭": [_item("新A", "IMPH-JP001")]}})
    monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
    monkeypatch.setattr(collect_featured, "resolve_pack_head", lambda pack: "IMPH")
    monkeypatch.setattr(collect_featured.time, "sleep", lambda *_: None)
    monkeypatch.setattr(collect_featured, "should_run_now", lambda pack, now: True)
    monkeypatch.setattr(collect_featured, "shops_open_now", lambda pack, shops, now: shops)

    collect_featured.main()

    assert calls["新A"] == (["カードラボ"], ["遊々亭"]), "型番で取れた店はカード名で探さず、商品を渡す"
    assert calls["新B"] == (["遊々亭", "カードラボ"], []), "型番で見つからなかったカードはカード名で探す"
    assert calls["再録C"] == (["遊々亭", "カードラボ"], [])


def test_collect_and_save_merges_pack_items(monkeypatch):
    saved_rows = []
    monkeypatch.setattr(collect_featured, "compare_prices",
                        lambda name, shop_names=None, status_out=None:
                        (status_out.update({s: {"count": 1, "fetch_errors": 0} for s in shop_names})
                         or [_item(name, "IMPH-JP001", price=300, shop="カードラボ")]))
    monkeypatch.setattr(collect_featured, "upsert_price_rows",
                        lambda sb, rows, raise_on_error=False: saved_rows.extend(rows) or len(rows))
    saved, failed = collect_featured.collect_and_save_for_shops(
        None, "新A", "2026-10-31", ["カードラボ"], pack_items=[_item("新A", "IMPH-JP001", price=200)])
    assert failed == []
    assert sorted((r["shop"], r["min_price"]) for r in saved_rows) == [("カードラボ", 300), ("遊々亭", 200)]


def test_collect_and_save_with_only_pack_items(monkeypatch):
    """カード名で探す店が無くても（全店型番で取れた）、型番の商品だけで保存する"""
    saved_rows = []
    monkeypatch.setattr(collect_featured, "upsert_price_rows",
                        lambda sb, rows, raise_on_error=False: saved_rows.extend(rows) or len(rows))
    saved, failed = collect_featured.collect_and_save_for_shops(
        None, "新A", "2026-10-31", [], pack_items=[_item("新A", "IMPH-JP001", price=200)])
    assert failed == [] and [(r["shop"], r["min_price"]) for r in saved_rows] == [("遊々亭", 200)]


def test_collect_and_save_writes_pack_version_rows(monkeypatch):
    """pack_code を渡すと、このパックの版の出品だけの行も新弾用の小さな表に書く（2026-11-01）"""
    pack_rows = []
    monkeypatch.setattr(collect_featured, "compare_prices",
                        lambda name, shop_names=None, status_out=None:
                        (status_out.update({s: {"count": 2, "fetch_errors": 0} for s in shop_names})
                         or [_item(name, "MB01-JP003", price=300, shop="カードラボ"),
                             _item(name, "IMPH-JPS01", price=980, shop="カードラボ")]))
    monkeypatch.setattr(collect_featured, "upsert_price_rows",
                        lambda sb, rows, raise_on_error=False: len(rows))
    monkeypatch.setattr(collect_featured, "upsert_price_pack_rows", lambda sb, rows: pack_rows.extend(rows) or len(rows))
    collect_featured.collect_and_save_for_shops(None, "再録A", "2026-11-01", ["カードラボ"], pack_code="IMPH")
    assert [(r["pack_code"], r["shop"], r["min_price"]) for r in pack_rows] == [("IMPH", "カードラボ", 980)]


def test_drop_higher_than_today(monkeypatch):
    """型番で取った値が今日の行より高い (カード, 店) は外す（カード名の検索で確かめる）。今日の行が無いレアリティ・安いものは使う"""
    import price_history_reads
    row = lambda card, rarity, p_any, p=None: {"card_name": card, "shop": "遊々亭", "rarity": rarity,
                                                "min_price_any": p_any, "min_price": p if p is not None else p_any}
    today_rows = [row("新A", "ノーマル", 100), row("新B", "ノーマル", 100), row("新D", "ノーマル", 100),
                  row("新E", "ノーマル", 100, p=90)]
    monkeypatch.setattr(price_history_reads, "fetch_daily_rows_by_card_names",
                        lambda sb, names, cols, days, tag: (today_rows, set()))
    plan = {
        "新A": {"遊々亭": [_item("新A", "IMPH-JP001", price=150)]},           # 朝より高い → 外す
        "新B": {"遊々亭": [_item("新B", "IMPH-JP002", price=80)]},            # 安い → 使う
        "新C": {"遊々亭": [_item("新C", "IMPH-JP003", price=50)]},            # 今日の行が無い → 確かめられない
        "新D": {"遊々亭": [_item("新D", "IMPH-JP004", price=80),
                          _item("新D", "IMPH-JP004", price=900, rarity="シークレット")]},  # 今日の行に無いレアリティ
        "新E": {"遊々亭": [_item("新E", "IMPH-JP005", price=95)]},            # min_price だけ高い（95>90）→ 外す
    }
    dropped = set()
    kept = collect_featured.drop_higher_than_today(None, plan, "2026-10-31", dropped)
    assert set(kept) == {"新B"}
    assert dropped == {("新A", "遊々亭"), ("新C", "遊々亭"), ("新D", "遊々亭"), ("新E", "遊々亭")}


def test_drop_higher_than_today_read_failure(monkeypatch):
    import price_history_reads
    monkeypatch.setattr(price_history_reads, "fetch_daily_rows_by_card_names",
                        lambda sb, names, cols, days, tag: ([], {"新A"}))
    assert collect_featured.drop_higher_than_today(
        None, {"新A": {"遊々亭": [_item("新A", "IMPH-JP001")]}}, "2026-10-31") is None


def test_search_pack_shop_truncated_is_failed(monkeypatch):
    """ページの上限に届いた（打ち切られたかもしれない）店は失敗扱い"""
    monkeypatch.setattr(pack_search, "PACK_MAX_PAGES", 1)
    # 上限1ページでも、絞り込み後に1件でもあれば「最後のページまで読んだかもしれない」
    items = [_item("白鴉の騎士アン", "IMPH-JP011", shop="カードラッシュ")]
    res = pack_search.search_pack_shop("カードラッシュ", "IMPH", ["白鴉の騎士アン"], fn=lambda q: items)
    assert res.failed


# ── いつ動くか（2026-10-31 ユーザー指定） ──

def _jst(y, mo, d, h, mi=1):
    from datetime import datetime
    from constants import JST
    return datetime(y, mo, d, h, mi, tzinfo=JST)


def test_should_run_now_early_and_later_days():
    pack = {"start_date": "2026-10-31"}
    run = lambda *a: collect_featured.should_run_now(pack, _jst(*a))
    # 発売日〜3日目（0〜2日目）: 7・10・13・16・19・21時
    assert [h for h in range(24) if run(2026, 10, 31, h)] == [7, 10, 13, 16, 19, 21]
    assert [h for h in range(24) if run(2026, 11, 2, h)] == [7, 10, 13, 16, 19, 21]
    # 4日目以降: 10・15・20時
    assert [h for h in range(24) if run(2026, 11, 3, h)] == [10, 15, 20]
    assert [h for h in range(24) if run(2026, 11, 7, h)] == [10, 15, 20]
    # 発売前（手動で先に登録した弾）は10時の1日1回
    assert [h for h in range(24) if run(2026, 10, 29, h)] == [10]
    # 発売日が分からなければ従来どおり動かす
    assert collect_featured.should_run_now({"pack_name": "P"}, _jst(2026, 10, 31, 3))


def test_shops_open_now_on_release_day():
    pack = {"start_date": "2026-10-31"}
    shops = ["カードラッシュ", "トレコロCB", "遊々亭"]
    assert collect_featured.shops_open_now(pack, shops, _jst(2026, 10, 31, 7)) == ["トレコロCB"], "発売日の7時はトレコロだけ"
    assert collect_featured.shops_open_now(pack, shops, _jst(2026, 10, 31, 10)) == shops
    assert collect_featured.shops_open_now(pack, shops, _jst(2026, 11, 1, 7)) == shops, "2日目からは全店"


def test_main_exits_outside_the_hours(monkeypatch):
    """動かない回は店にも DB にも行かずに終わる。手動（--any-time）は動く"""
    import pytest
    monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
    monkeypatch.setattr(collect_featured, "_resolve_pack",
                        lambda sb, a: {"pack_name": "P", "start_date": "2026-10-31"})
    monkeypatch.setattr(collect_featured, "should_run_now", lambda pack, now: False)
    monkeypatch.setattr(collect_featured, "get_featured_cards",
                        lambda sb, p: (_ for _ in ()).throw(AssertionError("収録カードを読まない")))
    monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
    with pytest.raises(SystemExit) as e:
        collect_featured.main()
    assert e.value.code == 0
    monkeypatch.setattr(sys, "argv", ["collect_featured.py", "--any-time"])
    with pytest.raises(AssertionError, match="収録カードを読まない"):
        collect_featured.main()


def test_main_rechecks_dropped_torecolo_even_if_collected(monkeypatch):
    """今日の行で確かめられなかったトレコロの組は、朝に取れていてもカード名で探す（値上がりも入るように）"""
    calls = {}

    def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None, pack_items=None, pack_code=""):
        calls[card] = list(shops)
        return 0, []

    def fake_plan(sb, pack, cards, shops, today=None, dropped=None, head=None):
        dropped.add(("新A", "トレコロCB"))   # 型番の値が今日の行より高かった
        return {}

    monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
    monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
    monkeypatch.setattr(collect_featured, "_resolve_pack",
                        lambda sb, a: {"pack_name": "P", "start_date": "2026-10-31"})
    monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: ["新A", "新B"])
    monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["トレコロCB"])
    monkeypatch.setattr(collect_featured, "fetch_cards_collected_today", lambda *a: {"新A", "新B"})
    monkeypatch.setattr(collect_featured, "plan_pack_search", fake_plan)
    monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
    monkeypatch.setattr(collect_featured, "resolve_pack_head", lambda pack: "IMPH")
    monkeypatch.setattr(collect_featured.time, "sleep", lambda *_: None)
    monkeypatch.setattr(collect_featured, "should_run_now", lambda pack, now: True)
    monkeypatch.setattr(collect_featured, "shops_open_now", lambda pack, shops, now: shops)

    collect_featured.main()

    assert calls == {"新A": ["トレコロCB"]}, "確かめられなかった新Aだけトレコロで探し、新Bは朝の値のまま"
