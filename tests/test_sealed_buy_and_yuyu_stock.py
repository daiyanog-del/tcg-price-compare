"""未開封・付属品つきを買取の最高額から外す（2026-10-28 ユーザー決定）と、遊々亭の在庫数（「在庫 : ◯」）。
商品名・HTML は 2026-10-28 に各店から取れた実際の形。"""
import pytest
from bs4 import BeautifulSoup

import art_variant as av
import collect_buyback
import scraper


@pytest.mark.parametrize("name,sealed", [
    ("(未開封)ブラックマジシャン", True),
    ("(封筒付き未開封)ブラックマジシャン", True),
    ("(未開封)ブラックマジシャン(当選通知書付き、ダンボール付き)", True),
    ("ブラックマジシャン(台紙、封筒付き)", True),
    ("(BD付き未開封)死者蘇生", True),
    ("《未開封》【遊戯】青眼の白龍", True),
    ("ブラックマジシャン", False),
    ("灰流うらら(イラスト違い版)", False),
    ("ブラックマジシャン(モノクロ)", False),
])
def test_is_sealed(name, sealed):
    assert av.is_sealed(name) is sealed


def _buy(shop, name, price, rarity="ウルトラ", code="25TH-JP001"):
    return {"shop": shop, "name": name, "price": price, "rarity": rarity, "code": code,
            "condition": "-", "sold_out": False, "url": ""}


def test_collect_buyback_records_regular_max(monkeypatch):
    rows = [
        _buy("カードラッシュ", "ブラックマジシャン", 25000),
        _buy("カードラッシュ", "(未開封)ブラックマジシャン(当選通知書付き、ダンボール付き)", 45000),
        _buy("カードラッシュ", "(未開封)ブラックマジシャン", 35000, rarity="スーパー"),  # 未開封しか無いレアリティは記録しない
        _buy("遊々亭", "ブラック・マジシャン", 24000),
    ]
    saved = []
    monkeypatch.setattr(collect_buyback, "compare_buyback", lambda *a, **k: rows)
    monkeypatch.setattr(collect_buyback, "upsert_buyback_rows", lambda sb, r, **k: saved.extend(r) or len(r))
    collect_buyback.collect_and_save_buyback(None, "ブラック・マジシャン", "2026-10-28", ["カードラッシュ", "遊々亭"])
    assert sorted((r["shop"], r["rarity"], r["max_price"]) for r in saved) == [
        ("カードラッシュ", "ウルトラ", 25000), ("遊々亭", "ウルトラ", 24000)]


def test_build_buyback_done_marks_sealed_without_mutating():
    import app as app_module

    rows = [_buy("カードラッシュ", "ブラックマジシャン", 25000),
            _buy("カードラッシュ", "(封筒付き未開封)ブラックマジシャン", 45000)]
    done = app_module._build_buyback_done(rows)
    assert [r["sealed"] for r in done["results"]] == [False, True]
    assert "sealed" not in rows[0]
    # 販売と同じく、GREEN Ver. は画面では 25thシークレット に寄せる
    green = app_module._build_buyback_done([_buy("カードラッシュ", "ブラックマジシャン", 10000,
                                                 rarity="クォーターセンチュリーシークレットGREEN Ver.")])
    assert green["results"][0]["rarity"] == "25thシークレット"


def test_deck_buy_best_ignores_sealed():
    import app as app_module

    assert app_module._best_buy_item([_buy("カードラッシュ", "ブラックマジシャン", 25000),
                                      _buy("カードラッシュ", "(未開封)ブラックマジシャン", 45000)])["price"] == 25000
    # 未開封しか無いカードは「買取なし」（売れる額を大きく見せない）
    assert app_module._best_buy_item([_buy("カードラッシュ", "(未開封)ブラックマジシャン", 45000)]) is None
    assert app_module._best_buy_item([]) is None


def _yuyu_card(cid, label, limit, sold_out=False):
    return (
        f'<div class="card-product position-relative mt-4{" sold-out" if sold_out else ""}">'
        f'<a href="https://yuyu-tei.jp/sell/ygo/card/qcac/{cid}"><div class="product-img">'
        f'<img alt="QCAC-JP050 SE 灰流うらら" class="card img-fluid" src="https://card.yuyu-tei.jp/ygo/100_140/qcac/{cid}.jpg"/></div></a>'
        '<span class="d-block border border-dark p-1 w-100 text-center my-2">QCAC-JP050</span>'
        f'<a href="https://yuyu-tei.jp/sell/ygo/card/qcac/{cid}"><h4 class="text-primary fw-bold">灰流うらら</h4></a>'
        '<strong class="d-block text-end">500 円</strong>'
        f'<div class="form-check p-0"><label class="form-check-label fw-bold float-start cart_sell_zaiko">在庫 :\n{label}\n</label></div>'
        f'<div class="d-flex counter"><input class="stock" type="text" value="1"/>'
        f'<input class="cart_limit" type="hidden" value="{limit}"/></div></div>'
    )


def test_scrape_yuyu_reads_stock_when_marked_plenty(monkeypatch):
    # 在庫が多いと「在庫 : ◯」で数字を出さない。cart_limit が在庫数（「N点」の商品で一致を確認）
    html = "<html><body>" + _yuyu_card(10251, "◯", 17) + _yuyu_card(10252, "2 点", 2) + \
        _yuyu_card(10253, "×", 0, sold_out=True) + "</body></html>"
    monkeypatch.setattr(scraper, "safe_get", lambda *a, **k: BeautifulSoup(html, "html.parser"))
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)
    rows = scraper.scrape_yuyu("灰流うらら")
    assert [(r["url"].rsplit("/", 1)[1], r["stock"], r["sold_out"]) for r in rows] == [
        ("10251", 17, False), ("10252", 2, False), ("10253", 0, True)]
