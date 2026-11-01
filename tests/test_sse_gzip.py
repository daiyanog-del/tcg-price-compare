"""
tests/test_sse_gzip.py — 検索の SSE の gzip（app._gzip_sse・_sse_response）

背景: 検索1回の通信が約590KB（灰流うらら）あり、圧縮されずにスマホへ送られていた（2026-11-01）。
flask-compress の流れる応答の圧縮はイベントごとに区切らないので、進み具合や順番待ちの合図が届かなくなる。

確かめること:
  - イベントごとに区切られ、届いたぶんだけで1件ずつ読める（途中のイベントが圧縮器の中に溜まらない）
  - 全体を戻すと圧縮しないときと同じイベントになる（/api/search の実ルート）
  - gzip を受け取れない要求は今までどおり圧縮しない
  - 途中で閉じたら元の生成器も閉じる（切断の後始末・検索の枠の返却を元の生成器に任せている）
"""

import json
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
import app as app_module


def _events():
    for i in range(3):
        yield app_module._sse({"type": "progress", "shop": f"店{i}", "pad": "あ" * 500})


def test_each_event_is_readable_as_soon_as_it_arrives():
    d = zlib.decompressobj(zlib.MAX_WBITS | 16)
    expected = list(_events())
    chunks = list(app_module._gzip_sse(_events()))
    for i, event in enumerate(expected):
        assert d.decompress(chunks[i]).decode("utf-8") == event
    assert d.decompress(chunks[-1]) == b""
    assert d.eof


def test_closing_early_closes_the_inner_stream():
    closed = []

    def inner():
        try:
            yield app_module._sse({"type": "progress"})
            yield app_module._sse({"type": "done"})
        finally:
            closed.append(True)

    gen = app_module._gzip_sse(inner())
    next(gen)
    gen.close()
    assert closed == [True]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "sell")
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR", tmp_path / "buy")
    monkeypatch.setattr(app_module, "_supabase_client", None)
    row = {"shop": "店A", "name": "テストカード", "rarity": "レア", "code": "", "condition": "-",
           "price": 100, "stock": 1, "sold_out": False, "url": "", "image": ""}
    monkeypatch.setattr(app_module, "SHOPS", [("店A", lambda card_name: [row])])
    app_module._load_cardnames()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _search(client, headers):
    app_module._reset_rate_limits()
    return client.get("/api/search?q=テストカード&confirmed=true&shops=店A", headers=headers)


def _parse(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.mark.parametrize("cached", [False, True])
def test_search_route_gzip_matches_plain(client, monkeypatch, cached):
    if cached:
        _search(client, {}).get_data()  # 読み切ると店の結果がキャッシュに入り、以後は命中の経路（cached_stream）
    else:
        monkeypatch.setattr(scraper, "CACHE_ENABLED", False)  # 毎回店へ取りに行く経路（generate）
    gz = _search(client, {"Accept-Encoding": "gzip, deflate, br"})
    assert gz.headers["Content-Encoding"] == "gzip"
    assert "Accept-Encoding" in gz.headers["Vary"]
    events = _parse(zlib.decompress(gz.get_data(), zlib.MAX_WBITS | 16).decode("utf-8"))
    plain = _search(client, {})
    assert "Content-Encoding" not in plain.headers
    plain_events = _parse(plain.get_data().decode("utf-8"))
    assert [e["type"] for e in events] == [e["type"] for e in plain_events]
    assert events[-1]["type"] == "done" and events[-1]["total"] == 1
    assert events[-1]["results"] == plain_events[-1]["results"]
    assert gz.headers["Cache-Control"] == "no-cache, no-transform"


@pytest.mark.parametrize("read_first", [True, False])
def test_closing_gzip_search_returns_the_slot(client, monkeypatch, read_first):
    """gzip を挟んでも、途中で閉じた（1件も読まずに閉じた）検索の枠が返る（reviewer 低5a）"""
    monkeypatch.setattr(scraper, "CACHE_ENABLED", False)
    app_module._reset_rate_limits()
    resp = client.get("/api/search?q=テストカード&confirmed=true&shops=店A",
                      headers={"Accept-Encoding": "gzip"}, buffered=False)
    assert resp.headers["Content-Encoding"] == "gzip"
    if read_first:
        next(iter(resp.response))
    resp.close()
    assert app_module._search_stream_slots.in_use() == {"single": 0, "bulk": 0}


def test_gzip_switch_and_refusal(client, monkeypatch):
    monkeypatch.setattr(app_module, "_SSE_GZIP_ENABLED", False)
    assert "Content-Encoding" not in _search(client, {"Accept-Encoding": "gzip"}).headers
    monkeypatch.setattr(app_module, "_SSE_GZIP_ENABLED", True)
    assert "Content-Encoding" not in _search(client, {"Accept-Encoding": "gzip;q=0, br"}).headers
