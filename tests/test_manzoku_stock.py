from bs4 import BeautifulSoup

import scraper


def _manzoku_item(pid, stock_html):
    return (
        '<li class="ec-shelfGrid__item">'
        f'<a href="https://shopmanzokuya.com/products/detail/{pid}">'
        f'<p class="ec-shelfGrid__item-text">〈UR〉QCAC-JP050《灰流うらら》(炎)</p>'
        '<p class="price02-default">￥180(税込)</p></a>'
        f'<form><p class="productStock">{stock_html}</p></form></li>'
    )


def test_scrape_manzoku_marks_hidden_count_as_four_plus(monkeypatch):
    # まんぞく屋は1〜3枚だけ数を出し、それ以上は「◯」で伏せる
    html = "<html><body><ul>" + _manzoku_item(1, "在庫: <span>◯</span>") + \
        _manzoku_item(2, "在庫: <span>2</span>") + "</ul></body></html>"
    monkeypatch.setattr(scraper, "safe_get", lambda *a, **k: BeautifulSoup(html, "html.parser"))
    monkeypatch.setattr(scraper, "dump_html", lambda *a, **k: None)
    rows = scraper.scrape_manzoku("灰流うらら")
    by_url = {r["url"].rsplit("/", 1)[-1]: r for r in rows}
    assert by_url["1"]["stock"] == 4 and by_url["1"]["stock_plus"] is True
    assert by_url["2"]["stock"] == 2 and "stock_plus" not in by_url["2"]
    assert not by_url["1"]["sold_out"] and not by_url["2"]["sold_out"]
