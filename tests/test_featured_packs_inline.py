"""/featured に、/api/packs が最後に返した一覧を埋め込むことの検証（2026-10-22 LCP）。DB・外部サイトは待たない。"""
import time


def test_featured_embeds_only_recent_packs(monkeypatch):
    import app as app_module
    client = app_module.app.test_client()
    # 覚えていなければ埋め込まない（従来どおり packs.js が /api/packs を取る）
    monkeypatch.setitem(app_module._packs_payload, "packs", None)
    assert 'id="packsInitial"' not in client.get("/featured").get_data(as_text=True)
    packs = [{"name": "<新弾>", "wiki_page": "w", "tcg_name": "", "date": "2026-11-21", "featured": True, "card_count": 80}]
    monkeypatch.setitem(app_module._packs_payload, "packs", packs)
    monkeypatch.setitem(app_module._packs_payload, "at", time.time())
    html = client.get("/featured").get_data(as_text=True)
    assert 'id="packsInitial"' in html
    # JSON は < > をエスケープして埋め込む（日本語も \uXXXX の形になる）
    assert "<新弾>" not in html and r"\u003c" in html
    # トップには埋め込まない（新商品のタブは /featured 以外では後から開く）
    assert 'id="packsInitial"' not in client.get("/").get_data(as_text=True)
    # 10分を過ぎたら埋め込まない
    monkeypatch.setitem(app_module._packs_payload, "at", time.time() - app_module._PACKS_PAYLOAD_TTL_SEC - 1)
    assert 'id="packsInitial"' not in client.get("/featured").get_data(as_text=True)
