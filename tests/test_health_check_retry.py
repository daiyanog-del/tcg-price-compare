"""店舗ヘルスチェックの取り直しと、買取の確認先（2026-10-29）のテスト。

2026-10 に買取の収集でカードラッシュが9日、丸ごと当日スキップされていた。
買取はラッシュメディア（cardrush.media）から取るのに、販売の www.cardrush.jp のトップを
5秒で1回だけ確かめ、タイムアウトしたらその日は取らない作りだったため。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import collect_prices as cp  # noqa: E402
import meta_scraper  # noqa: E402


def _ok(status=200):
    return SimpleNamespace(status_code=status, close=lambda: None)


def test_timeout_once_then_retry_passes(monkeypatch):
    calls = []

    def fake_get(url, **k):
        calls.append(k["timeout"])
        if len(calls) == 1:
            raise requests.exceptions.ReadTimeout("slow")
        return _ok()

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    assert cp.check_shop_availability(["カードラッシュ"]) == ["カードラッシュ"]
    assert calls == [5, 15]  # 取り直しは実際の取得と同じだけ待つ


def test_timeout_twice_skips(monkeypatch, capsys):
    def fake_get(url, **k):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    assert cp.check_shop_availability(["カードラッシュ"]) == []
    assert "当日スキップ（ReadTimeout）" in capsys.readouterr().out


def test_connection_error_then_retry_passes(monkeypatch):
    seq = iter([requests.exceptions.ConnectionError("reset"), _ok()])

    def fake_get(url, **k):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    assert cp.check_shop_availability(["カードラッシュ"]) == ["カードラッシュ"]


def test_timeout_then_5xx_then_ok(monkeypatch):
    # 例外の取り直しのあとに 5xx が返っても、5xx の取り直しへ進む
    seq = iter([requests.exceptions.ReadTimeout("slow"), _ok(503), _ok(200)])

    def fake_get(url, **k):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(cp.time, "sleep", lambda s: None)
    assert cp.check_shop_availability(["カードラッシュ"]) == ["カードラッシュ"]


def test_buyback_checks_rush_media(monkeypatch):
    import collect_buyback as cb
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(url) or _ok())
    assert cp.check_shop_availability(["カードラッシュ", "トレコロCB"], cb.BUYBACK_HEALTH_URLS) \
        == ["カードラッシュ", "トレコロCB"]
    assert calls == ["https://cardrush.media/", "https://www.torecolo.jp/"]
    # 販売の確認先は変えない
    assert cp.SHOP_HEALTH_URLS["カードラッシュ"] == "https://www.cardrush.jp/"


def test_deck_without_usage_data_is_not_structure_warning(monkeypatch, tmp_path, capsys):
    from bs4 import BeautifulSoup
    monkeypatch.setattr(meta_scraper, "_CACHE_DIR", tmp_path)
    html = ("<html><body><h2>大会での主要カード</h2><p>カード使用頻度データがありません</p>"
            "<p>Tier 4 使用率 0.9%</p></body></html>")
    monkeypatch.setattr(meta_scraper, "_fetch_soup", lambda url, **k: BeautifulSoup(html, "html.parser"))

    data = meta_scraper.fetch_deck_cards("クリストロン", force=True)

    out = capsys.readouterr().out
    assert data["cards"] == []
    assert "構造変更の疑い" not in out
    assert "データがサイトに無い" in out
