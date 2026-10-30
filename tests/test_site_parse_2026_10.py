"""2026-10 の相手サイト改修への追従（TCG PORTAL のデッキ解説・大会レシピ、トレコロの在庫数）。

見本の HTML は 2026-10-28 に取得した実ページの行を、要素の形を変えずに縮めたもの。
"""
from bs4 import BeautifulSoup

import meta_scraper
import scraper


def _usage_row(name, adoption, avg):
    return (
        '<li><div class="cursor-pointer" data-quote-row="usage"><div class="grid">'
        '<span><img src="https://images.tcg-portal.jp/c/thumb.webp" width="28"/></span>'
        f'<div><button type="button"><span class="block font-medium">{name}</span></button></div>'
        f'<span><b class="text-[13px]">{adoption}%</b><span><i style="width:100%"></i></span>'
        f'<span class="md:hidden">平均<!-- -->{avg}<!-- -->枚</span></span>'
        f'<span class="hidden md:block"><b>{avg}</b><small>枚</small></span>'
        '<span><b>¥92<!-- -->〜</b></span></div></div></li>'
    )


# サイドデッキ採用傾向の欄（旧構造に似た /yugioh/cards/ リンク）。主要カードとして拾わないこと
_SIDE_ITEM = (
    '<li class="flex"><img src="https://images.tcg-portal.jp/s/thumb.webp"/><div>'
    '<a href="/yugioh/cards/%E5%AE%87">宇宙的ハリケーン</a>'
    '<span>100.0<!-- -->%<span>·</span>平均<!-- -->2.0<!-- -->枚</span></div></li>'
)

_DECK_PAGE = (
    '<html><body><div>環境での評価 三幻魔 直近1ヶ月 Tier 3 使用率 7.8% 順位 4 位</div>'
    '<ul>' + _usage_row("三幻魔の殉教者", 100, "3.0") + _usage_row("No.39 希望皇ホープ", 50, "1.5") + '</ul>'
    '<ul>' + _SIDE_ITEM + '</ul>'
    '<a href="/yugioh/tournament-results/cmvhfxp81002mjz083rzlvvnl">デッキレシピ →</a>'
    '</body></html>'
)


def _deck_row(qty, name):
    return (
        '<li><div class="cursor-pointer" data-quote-row="deck"><div class="grid">'
        f'<span class="text-right" data-deck-qty="true">{qty}</span>'
        '<span><img src="x.webp"/></span>'
        f'<div><button type="button"><span class="block">{name}</span></button></div>'
        '<span>¥92</span><span><b>¥276</b></span></div></div></li>'
    )


_RECIPE_PAGE = (
    '<html><body><h3>ランキングデュエル 優勝 【三幻魔】のデッキ価格</h3>'
    '<h3>メインデッキ（4枚）</h3><ul>' + _deck_row(3, "三幻魔の殉教者") + _deck_row(1, "神鳴り") + '</ul>'
    '<h3>EXデッキ（1枚）</h3><ul>' + _deck_row(1, "混沌の召喚神") + '</ul>'
    '<h3>サイドデッキ（2枚）</h3><ul>' + _deck_row(2, "幽鬼うさぎ") + '</ul>'
    '<h3>関連デッキ解説</h3></body></html>'
)


def _fake_soup(pages):
    def fetch(url, *args, **kwargs):
        for key, html in pages.items():
            if key in url:
                return BeautifulSoup(html, "html.parser")
        return None
    return fetch


def test_fetch_deck_cards_reads_usage_rows(monkeypatch, tmp_path):
    monkeypatch.setattr(meta_scraper, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(meta_scraper, "_fetch_soup", _fake_soup({
        "/deck-guides/": _DECK_PAGE,
        "/tournament-results/": _RECIPE_PAGE,
    }))

    data = meta_scraper.fetch_deck_cards("三幻魔", force=True)

    assert data["tier"] == 3
    assert data["share"] == 7.8
    assert data["cards"] == [
        {"name": "三幻魔の殉教者", "adoption": 100.0, "avg": 3.0,
         "image": "https://images.tcg-portal.jp/c/thumb.webp"},
        {"name": "No.39 希望皇ホープ", "adoption": 50.0, "avg": 1.5,
         "image": "https://images.tcg-portal.jp/c/thumb.webp"},
    ]
    assert [r["url"] for r in data["recipes"]] == [
        "https://tcg-portal.jp/yugioh/tournament-results/cmvhfxp81002mjz083rzlvvnl"]
    # サイドは入れず、メインとEXだけ
    assert data["full_deck"] == [
        {"name": "三幻魔の殉教者", "qty": 3, "is_ex": False},
        {"name": "神鳴り", "qty": 1, "is_ex": False},
        {"name": "混沌の召喚神", "qty": 1, "is_ex": True},
    ]


def test_fetch_deck_cards_warns_when_nothing_parsed(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(meta_scraper, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(meta_scraper, "_fetch_soup", _fake_soup({
        "/deck-guides/": "<html><body><ul>" + _SIDE_ITEM + "</ul></body></html>",
    }))

    data = meta_scraper.fetch_deck_cards("三幻魔", force=True)

    assert data["cards"] == []
    assert "構造変更の疑い" in capsys.readouterr().out


def _toreco_item(code, *, stock_count, sold_out, rarity="ホログラフィックレア"):
    button = ('<span class="btn-sold-out btn btn-add-cart">売切れ</span>' if sold_out else
              f'<a class="block-products--product-sale-cart-button" href="/shop/cart/cart.aspx?goods={code}">カゴへ</a>')
    return (
        '<dl class="block-thumbnail-t--goods js-enhanced-ecommerce-item">'
        f'<dt><a href="/shop/g/g{code}/"><figure><img data-src="/img/goods/L/{code}.jpg" src="/img/usr/lazyloading.png"/></figure></a></dt>'
        f'<dd><div class="block-thumbnail-t--goods-category">{rarity}</div>'
        f'<div><a class="js-enhanced-ecommerce-goods-name" href="/shop/g/g{code}/">灰流うらら</a></div>'
        '<div class="block-thumbnail-t--price price"> 3,980円（税込）</div>'
        '<div class="block-thumbnail-t--stock-items">'
        f'<select class="block-products--product-sale-cart-quantity-select" data-stock-count="{stock_count}" name="qty"></select>'
        f'<p class="block-products--product-stock">在庫 <span>{stock_count}</span></p></div>'
        f'<div class="block-thumbnail-t--add-cart">{button}</div></dd></dl>'
    )


def test_scrape_torecolo_reads_stock_count(monkeypatch):
    html = ("<html><body>"
            + _toreco_item("RC04-JP009HR", stock_count=3, sold_out=False)
            + _toreco_item("20TH-JPC8520SE", stock_count=0, sold_out=True)
            + "</body></html>")
    monkeypatch.setattr(scraper, "safe_get", lambda url, *a, **k: BeautifulSoup(html, "html.parser"))
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)

    rows = scraper.scrape_torecolo("灰流うらら", max_pages=1)

    assert [(r["code"], r["stock"], r["sold_out"]) for r in rows] == [
        ("RC04-JP009HR", 3, False),
        ("20TH-JPC8520SE", 0, True),
    ]
    # 一覧の src は遅延読み込みの仮画像。本物の data-src を読む
    assert [r["image"] for r in rows] == [
        "https://www.torecolo.jp/img/goods/L/RC04-JP009HR.jpg",
        "https://www.torecolo.jp/img/goods/L/20TH-JPC8520SE.jpg",
    ]


def test_scrape_torecolo_skips_stainless_rarity(monkeypatch):
    # トレコロは「ステンレス製」を商品名ではなくレアリティの欄に書く（2026-10-30 OCG-252 が記録に入った）
    html = ("<html><body>"
            + _toreco_item("RC04-JP009HR", stock_count=3, sold_out=False)
            + _toreco_item("OCG-252", stock_count=1, sold_out=False, rarity="ステンレス製")
            + "</body></html>")
    monkeypatch.setattr(scraper, "safe_get", lambda url, *a, **k: BeautifulSoup(html, "html.parser"))
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)

    rows = scraper.scrape_torecolo("灰流うらら", max_pages=1)

    assert [r["code"] for r in rows] == ["RC04-JP009HR"]


def test_is_supply_rarity():
    assert scraper._is_supply_rarity("ステンレス製")
    assert not scraper._is_supply_rarity("ホログラフィックレア")
    assert not scraper._is_supply_rarity("")


def test_torecolo_image_url_skips_placeholder():
    def img(html):
        return BeautifulSoup(html, "html.parser").img

    assert scraper._torecolo_image_url(img('<img src="/img/usr/lazyloading.png"/>')) == ""
    assert scraper._torecolo_image_url(img('<img src="/img/goods/L/A.jpg"/>')) == "https://www.torecolo.jp/img/goods/L/A.jpg"
    assert scraper._torecolo_image_url(None) == ""
