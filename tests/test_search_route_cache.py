"""
tests/test_search_route_cache.py — /api/search ルートの店舗束キャッシュ回帰テスト

背景:
  店舗束キャッシュ（test_cache_coverage.py 参照）の目的の半分は
  「取得失敗した店舗を15分間0件に固定しない」ことで、その判定は
  app.py 側の `if error is None and fetch_errors == 0:` に集約されている。
  キャッシュ層のテストだけではこの4行（販売/買取×検索/デッキ）を通らないため、
  ルートレベルで退行を検出する（2026-08-03 reviewer指摘 中-1）。

テスト方針:
  Flask test_client + 偽スクレイパーでネットワーク不使用。SHOPS を差し替え、
  CACHE_DIR を一時ディレクトリへ向ける。confirmed=true でカード名DB検査を
  バイパスし、レートリミットはバケツを都度クリアして回避する。
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
import app as app_module


def _row(shop, price=100):
    return {"shop": shop, "name": "テストカード", "rarity": "レア", "code": "",
            "condition": "-", "price": price, "stock": 1, "sold_out": False,
            "url": "", "image": ""}


def _parse_sse(data: bytes) -> list[dict]:
    events = []
    for line in data.decode("utf-8").splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _scrape_ok(card_name):
    return [_row("店A", 100)]


def _scrape_fetch_error(card_name):
    """0件かつ取得エラーあり（=在庫なしではなく取得失敗）"""
    scraper._note_fetch_error()
    return []


def _scrape_boom(card_name):
    raise RuntimeError("店C死亡")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "sell")
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR", tmp_path / "buy")
    monkeypatch.setattr(app_module, "_supabase_client", None)
    app_module._load_cardnames()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _search(client, shops):
    app_module._reset_rate_limits()  # レートリミット回避（バケット追加に追随するため一括で消す）
    q = "&".join(["q=テストカード", "confirmed=true"] + [f"shops={s}" for s in shops])
    resp = client.get("/api/search?" + q)
    assert resp.status_code == 200
    return _parse_sse(resp.get_data())


def _cache_shops():
    fp = scraper.CACHE_DIR / f"{scraper._cache_key('テストカード')}.json"
    if not fp.exists():
        return None
    return json.loads(fp.read_text(encoding="utf-8"))["shops"]


def test_fetch_error_shop_not_cached(client, monkeypatch):
    """取得失敗（fetch_errors>0）の店はキャッシュされず、成功店だけ書かれる"""
    monkeypatch.setattr(app_module, "SHOPS", [("店A", _scrape_ok), ("店B", _scrape_fetch_error)])
    _search(client, ["店A", "店B"])
    shops = _cache_shops()
    assert set(shops) == {"店A"}, "取得失敗の店Bがキャッシュに書かれている"

    # 2回目: 店Aは命中、店Bは再スクレイプされる（cachedフラグで判別）
    events = _search(client, ["店A", "店B"])
    done_flags = {e["shop"]: e.get("cached", False)
                  for e in events if e["type"] == "shop_done"}
    assert done_flags == {"店A": True, "店B": False}


def test_exception_shop_not_cached_and_done_arrives(client, monkeypatch):
    """例外を投げる店は shop_error を出しつつ非キャッシュ、done は必ず届く"""
    monkeypatch.setattr(app_module, "SHOPS", [("店A", _scrape_ok), ("店C", _scrape_boom)])
    events = _search(client, ["店A", "店C"])
    types = [e["type"] for e in events]
    assert "shop_error" in types
    assert types[-1] == "done"
    assert set(_cache_shops()) == {"店A"}


def test_mixed_stream_union_without_duplicates(client, monkeypatch):
    """店舗を絞った検索の後に別店舗を足しても、キャッシュ命中分＋新規取得分の
    和集合が重複なく返る（キャッシュ汚染バグ本体の回帰テスト）"""
    def scrape_b(card_name):
        return [_row("店B", 200)]
    monkeypatch.setattr(app_module, "SHOPS", [("店A", _scrape_ok), ("店B", scrape_b)])

    # 店Aだけ検索 → キャッシュは店Aのみ
    _search(client, ["店A"])
    assert set(_cache_shops()) == {"店A"}

    # 両店指定 → 店Aは cached:true（results付き）、店Bは新規取得
    events = _search(client, ["店A", "店B"])
    shop_done = {e["shop"]: e for e in events if e["type"] == "shop_done"}
    assert shop_done["店A"]["cached"] is True
    assert shop_done["店A"]["count"] == 1
    assert [r["shop"] for r in shop_done["店A"]["results"]] == ["店A"]
    assert shop_done["店B"].get("cached", False) is False

    done = [e for e in events if e["type"] == "done"][0]
    assert sorted(r["shop"] for r in done["results"]) == ["店A", "店B"]

    # 以後は全店キャッシュ命中
    events = _search(client, ["店A", "店B"])
    assert all(e.get("cached") for e in events if e["type"] == "shop_done")
