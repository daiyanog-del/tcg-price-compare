"""全角ハイフン入りの未発売カードが、半角ハイフンの名前で引かれても画像・カード情報を返すことを検証する（2026-10-25）。

不具合: 未発売カード一覧からカードを開くと、/api/validate がダッシュを半角に統一した名前
（太陽神の処刑人-マキュラ）を返し、画面がその名前で /api/card-image・/api/card-info を引いていた。
表示の入口 resolve_card_display は未発売カードを完全一致でしか引かず、全角ハイフン入りの
カード（太陽神の処刑人－マキュラ）だけ画像も効果テキストも出なかった。
"""
from test_tcg_preview_display import Database, _unreleased_row  # 同じ偽の DB と行の形を使う

import pytest

import card_display as display

FULL = "太陽神の処刑人－マキュラ"   # 登録上の正式名（全角ハイフン）
HALF = "太陽神の処刑人-マキュラ"    # 検索・URL 経由で半角に統一された名前


@pytest.fixture
def database(monkeypatch):
    db = Database()
    db.rows["tcg_preview_cards"] = []
    db.rows["unreleased_cards"] = [_unreleased_row(FULL)]
    db.rows["official_card_images"] = [{
        "unreleased_card_id": 7, "public_url": "https://example/maku.png", "thumb_url": None,
        "hidden": False, "deleted_at": None}]
    display.invalidate_cache()
    monkeypatch.setattr(display, "_supabase_client", db)
    monkeypatch.setattr(display, "_released_resolver", lambda name: None)
    yield db
    display.invalidate_cache()


@pytest.fixture
def client(database):
    import app as app_module
    return app_module.app.test_client()


def test_半角ハイフンの名前でも未発売カードの画像を返す(database):
    r = display.resolve_card_display(HALF)
    assert r == {"kind": "image", "url": "https://example/maku.png", "source": "official_sample"}


def test_画像が無ければ半角ハイフンの名前でも_proxy(database):
    database.rows["official_card_images"] = []
    display.invalidate_cache()
    r = display.resolve_card_display(HALF)
    assert r["kind"] == "proxy" and r["proxy"]["name"] == FULL


def test_まとめて解決でも半角ハイフンを吸収する(database):
    got = display.resolve_card_displays([HALF, FULL, "存在しない"])
    assert got[HALF] == got[FULL] == display.resolve_card_display(FULL)
    assert got["存在しない"] == {"kind": "none"}


def test_validate_は登録上の正式名を返す(client):
    got = client.get("/api/validate", query_string={"q": FULL}).get_json()
    assert got == {"valid": True, "name": FULL, "unreleased": True}
    got = client.get("/api/validate", query_string={"q": HALF}).get_json()
    assert got == {"valid": True, "name": FULL, "unreleased": True}


def test_半角ハイフンの名前でも_card_image_と_card_info_が中身を返す(client):
    img = client.get("/api/card-image", query_string={"name": HALF}).get_json()
    assert img["kind"] == "image" and img["url"] == "https://example/maku.png"
    info = client.get("/api/card-info", query_string={"name": HALF}).get_json()
    assert info["kind"] == "proxy" and info["proxy"]["effect_text"] == "日本語の未発売の文"
    imgs = client.post("/api/card-images", json={"names": [HALF]}).get_json()["images"]
    assert imgs[HALF]["kind"] == "image"
