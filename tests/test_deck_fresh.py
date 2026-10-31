"""
tests/test_deck_fresh.py — /api/deck の fresh（購入候補の「価格を更新」）の検証（2026-10-31）

fresh=1 のときは:
  - トレコロ・カーナベルも1ページ目に絞らない（全ページの取得関数をそのまま使う）
  - 1ページ目だけのキャッシュ（partial）を命中にしない
  - 結果をその日の行に上書きする（ignore_duplicates=False。/api/search と同じ）
  - 書き終えてから card_done を返す（画面が終わったら DB を読み直すため。Thread に回さない）
  - カード辞書に無い名前は書かない
ふだんの一括検索（fresh なし）は従来どおり（test_deck_ignore_duplicates.py）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module
from test_deck_ignore_duplicates import _FakeSupabase


def _setup(monkeypatch, shop_name="トレコロCB"):
    calls = {"scraped": [], "include_partial": [], "partial_shops": [], "threads": 0}

    def fake_scrape(card_name, *a, **k):
        calls["scraped"].append((card_name, k))
        return [{
            "shop": shop_name, "name": card_name, "rarity": "レア", "code": "",
            "condition": "-", "price": 1200, "stock": 1, "sold_out": False,
            "url": "", "image": "",
        }]

    def fake_cache_get(name, shops, include_partial=False):
        calls["include_partial"].append(include_partial)
        return {}, list(shops)

    def fake_cache_store(name, shop_results, partial_shops=frozenset()):
        calls["partial_shops"].append(set(partial_shops))

    class CountingThread:
        def __init__(self, target=None, args=(), kwargs=None, daemon=None):
            self._target, self._args, self._kwargs = target, args, kwargs or {}

        def start(self):
            if self._target is app_module._persist_scrape_async:
                calls["threads"] += 1
            self._target(*self._args, **self._kwargs)

    monkeypatch.setattr(app_module, "SHOPS", [(shop_name, fake_scrape)], raising=False)
    monkeypatch.setattr(app_module, "cache_get_shops", fake_cache_get)
    monkeypatch.setattr(app_module, "cache_store_shops", fake_cache_store)
    monkeypatch.setattr(app_module, "Thread", CountingThread, raising=False)
    monkeypatch.setattr(app_module, "_last_deck", {}, raising=False)
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)
    app_module._load_cardnames()
    app_module.app.config.update(TESTING=True)
    return calls, sink


def _known_card():
    app_module._load_cardnames()
    assert app_module._cardnames_set, "カード辞書が読めない"
    return "青眼の白龍" if "青眼の白龍" in app_module._cardnames_set else sorted(app_module._cardnames_set)[0]


def _post(data):
    client = app_module.app.test_client()
    resp = client.post("/api/deck", data=data)
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_fresh_uses_full_pages_and_overwrites(monkeypatch):
    calls, sink = _setup(monkeypatch)
    name = _known_card()
    body = _post({"cards": name, "shops": ["トレコロCB"], "fresh": "1", "include_per_shop": "1"})

    assert '"card_done"' in body
    # 全ページの取得関数（max_pages=1 を渡さない）をそのまま使う
    assert calls["scraped"] and all("max_pages" not in k for _, k in calls["scraped"])
    # 1ページ目だけのキャッシュは命中にしない・全ページの結果として覚える
    assert calls["include_partial"] == [False]
    assert calls["partial_shops"] == [set()]
    # その日の行に上書きし、Thread に回さず書き終えてから返す
    assert sink and sink[-1]["p_ignore_duplicates"] is False
    assert sink[-1]["p_rows"][0]["min_price"] == 1200
    assert calls["threads"] == 0


def test_fresh_does_not_write_unknown_names(monkeypatch):
    calls, sink = _setup(monkeypatch, shop_name="テスト店")
    _post({"cards": "辞書に無いカード名ぜったい", "shops": ["テスト店"], "fresh": "1"})
    assert calls["scraped"], "検索自体はする"
    assert sink == []


def test_without_fresh_keeps_first_page_and_no_overwrite(monkeypatch):
    calls, sink = _setup(monkeypatch)
    import scraper
    fast = []
    monkeypatch.setattr(scraper, "scrape_torecolo",
                        lambda card_name, max_pages=None, **k: fast.append(max_pages) or [])
    name = _known_card()
    _post({"cards": name, "shops": ["トレコロCB"]})
    # ふだんの一括検索はトレコロを1ページ目に絞る（スタブではなく partial(scrape_torecolo, max_pages=1) が使われる）
    assert calls["scraped"] == []
    assert fast == [1]
    assert calls["include_partial"] == [True]


def _item(shop, name, price):
    return {"shop": shop, "name": name, "rarity": "レア", "code": "", "condition": "-",
            "price": price, "stock": 1, "sold_out": False, "url": "", "image": ""}


def _two_shops(monkeypatch, cached=None, fail_shop=None):
    """店A・店Bの2店。cached={店: 結果} はキャッシュの命中分。fail_shop は取得の一部に失敗した店"""
    def scrape_a(card_name, *a, **k):
        return [_item("店A", card_name, 1100)]

    def scrape_b(card_name, *a, **k):
        return [_item("店B", card_name, 900)]

    cached = cached or {}

    def fake_cache_get(name, shops, include_partial=False):
        hit = {s: cached[s] for s in shops if s in cached}
        return hit, [s for s in shops if s not in hit]

    real_result = app_module._shop_fetch_result

    def fake_result(future):
        items, errors = real_result(future)
        if fail_shop and any(r.get("shop") == fail_shop for r in items):
            return items, 1
        return items, errors

    monkeypatch.setattr(app_module, "SHOPS", [("店A", scrape_a), ("店B", scrape_b)], raising=False)
    monkeypatch.setattr(app_module, "cache_get_shops", fake_cache_get)
    monkeypatch.setattr(app_module, "cache_store_shops", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "_shop_fetch_result", fake_result)
    monkeypatch.setattr(app_module, "_last_deck", {}, raising=False)
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)
    app_module.app.config.update(TESTING=True)
    return sink


def _written_shops(sink):
    return sorted({r["shop"] for call in sink for r in call["p_rows"]})


def test_fresh_writes_cache_hits_too(monkeypatch):
    """全店キャッシュ命中でも書き戻す（15分以内に誰かが全ページを調べた値は朝の値より新しい。reviewer 高1）"""
    name = _known_card()
    sink = _two_shops(monkeypatch, cached={"店A": [_item("店A", name, 1000)], "店B": [_item("店B", name, 800)]})
    _post({"cards": name, "shops": ["店A", "店B"], "fresh": "1"})
    assert _written_shops(sink) == ["店A", "店B"]
    assert all(call["p_ignore_duplicates"] is False for call in sink)


def test_fresh_writes_partial_hit_and_scraped(monkeypatch):
    name = _known_card()
    sink = _two_shops(monkeypatch, cached={"店A": [_item("店A", name, 1000)]})
    _post({"cards": name, "shops": ["店A", "店B"], "fresh": "1"})
    assert _written_shops(sink) == ["店A", "店B"]


def test_fresh_skips_shop_with_fetch_errors(monkeypatch):
    """取得に一部失敗した店は取れた分だけの値で朝の値を壊さないよう書かない（reviewer 中2）"""
    name = _known_card()
    sink = _two_shops(monkeypatch, fail_shop="店B")
    body = _post({"cards": name, "shops": ["店A", "店B"], "fresh": "1"})
    assert _written_shops(sink) == ["店A"]
    assert "店B" in body  # failed_shops に載る（画面は「前の値のまま」と出す）


def test_fresh_one_stream_per_client(monkeypatch):
    """同じ接続元から調べ直しを同時に2本は流せない。終われば次を流せる（reviewer 中3）"""
    _two_shops(monkeypatch)
    ip = "203.0.113.9"
    monkeypatch.setattr(app_module, "_client_ip", lambda: ip)
    release = app_module._fresh_deck_open(ip)
    assert release is not None
    client = app_module.app.test_client()
    resp = client.post("/api/deck", data={"cards": _known_card(), "shops": ["店A"], "fresh": "1"})
    assert resp.status_code == 429
    release()
    monkeypatch.setattr(app_module, "_last_deck", {}, raising=False)
    resp = client.post("/api/deck", data={"cards": _known_card(), "shops": ["店A"], "fresh": "1"})
    assert resp.status_code == 200
    resp.get_data()
    resp.close()
    assert ip not in app_module._fresh_deck_active, "流し終えたら枠を返す"
