"""海外先行カード一覧（/tcg-preview・2026-10-23）のサーバー側を検証する。

- card_display.get_tcg_preview_cards: 表示対象（active かつ非表示でない）だけ・画像表示の設定に従う・取得失敗は例外
- /api/tcg-preview-cards: 正常は no-store、取得失敗は 503（/api/new-cards と同じ）
- /tcg-preview のページ（題名・印のクラス・戻るリンク）、未発売カード一覧だけに入口、サイトマップ
"""
import pytest

import card_display as display
from tests.test_tcg_preview_display import Database, _tcg_row  # noqa: F401（同じ偽の DB を使う）


@pytest.fixture
def database(monkeypatch):
    db = Database()
    display.invalidate_cache()
    monkeypatch.setattr(display, "_supabase_client", db)
    monkeypatch.setattr(display, "_released_resolver", lambda name: None)
    yield db
    display.invalidate_cache()


@pytest.fixture
def client(database):
    import app as app_module
    return app_module.app.test_client()


def test_一覧は表示対象だけを公式DBの番号順に返す(database):
    got = display.get_tcg_preview_cards()["cards"]
    assert [c["name"] for c in got] == ["Anotherverse Gluttonia", "Distrust Paranoia", "Angelechy Destrier", "Pending Card"]
    first = next(c for c in got if c["name"] == "Angelechy Destrier")
    assert first == {"name": "Angelechy Destrier", "product_name": "CHAOS ORIGINS", "release_date": "2026-07-03",
                     "image_url": first["image_url"]}
    assert first["image_url"].endswith("/tcg_preview/23307.png")
    assert next(c for c in got if c["name"] == "Distrust Paranoia")["image_url"] is None


def test_画像表示の設定が無効なら画像を出さない(database):
    database.rows["app_settings"] = [{"key": "OFFICIAL_IMAGE_DISPLAY", "value": {"enabled": False}}]
    assert all(c["image_url"] is None for c in display.get_tcg_preview_cards()["cards"])


def test_取得失敗は例外(database):
    database.fail.add("tcg_preview_cards")
    with pytest.raises(display.DisplayDataUnavailable):
        display.get_tcg_preview_cards()


def test_API(client, database):
    resp = client.get("/api/tcg-preview-cards")
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    assert len(resp.get_json()["cards"]) == 4
    display.invalidate_cache()
    database.fail.add("tcg_preview_cards")
    assert client.get("/api/tcg-preview-cards").status_code == 503


def test_ページと入口(client):
    html = client.get("/tcg-preview").get_data(as_text=True)
    assert "<title>海外先行カード一覧 | 強欲なラボ</title>" in html
    assert 'class="new-cards-page tcg-preview-page"' in html
    assert '<h2 id="newCardsHeading">海外先行カード一覧</h2>' in html  # JS を待たない見出し（検索エンジン向け）
    # 未発売カード一覧との行き来は「新弾情報」の切り替えの列で（2026-10-30。前の「← 未発売カード一覧へ」「海外先行カードを見る →」は廃止）
    assert '<a href="/tcg-preview" class="is-on" aria-current="page">海外先行カード</a>' in html
    assert '<a href="/new-cards">未発売カード</a>' in html
    nc = client.get("/new-cards").get_data(as_text=True)
    assert '<a href="/new-cards" class="is-on" aria-current="page">未発売カード</a>' in nc
    assert '<a href="/tcg-preview">海外先行カード</a>' in nc


def test_サイトマップに載せる(client):
    assert "/tcg-preview</loc>" in client.get("/sitemap.xml").get_data(as_text=True)


def test_設定の取得失敗も例外にする(database):
    # 失敗した空の設定で「画像を出す」側に倒れない（get_new_cards と同じ。2026-10-23 reviewer 指摘）
    database.fail.add("app_settings")
    with pytest.raises(display.DisplayDataUnavailable):
        display.get_tcg_preview_cards()


def test_説明文は両方置いてCSSで出し分ける(client):
    from pathlib import Path
    # 同じ HTML のままトップ等と行き来するので、どのページの HTML にも両方の文言がある（2026-10-23 reviewer 指摘）
    for path in ("/", "/new-cards", "/tcg-preview"):
        html = client.get(path).get_data(as_text=True)
        assert 'class="new-cards-help for-default"' in html and 'class="new-cards-help for-tcg"' in html, path
    css = (Path(__file__).resolve().parent.parent / "static/css/new-cards.css").read_text(encoding="utf-8")
    assert "#mode-search:not(.tcg-preview-page) .new-cards .for-tcg,#mode-search.tcg-preview-page .new-cards .for-default{display:none}" in css


def test_一覧ページは新弾情報のタブを光らせる(client):
    # 未発売カード一覧・海外先行カード一覧は「新弾情報」の中のページ（2026-10-30）。JS を待たずに HTML の時点で光らせる
    from pathlib import Path
    for path in ("/new-cards", "/tcg-preview"):
        html = client.get(path).get_data(as_text=True)
        assert 'class="mode-tab active" data-mode="packs" aria-current="page"' in html, path
        assert 'class="mode-tab" data-mode="search" onclick' in html, path
    top = client.get("/").get_data(as_text=True)
    assert 'class="mode-tab active" data-mode="search" aria-current="page"' in top
    assert '新商品</button>' not in top and '>新弾情報</button>' in top
    # トップの「新着の未発売カード」の欄には切り替えの列を出さない（新弾情報のタブの中の列はある）
    assert top.count('class="newset-subnav"') == 1
    assert '<a href="/#packs" class="is-on" aria-current="page">パック一覧</a>' in top
    css = (Path(__file__).resolve().parent.parent / "static/css/newset-subnav.css").read_text(encoding="utf-8")
    assert "#mode-search:not(.new-cards-page) .newset-subnav{display:none}" in css


def test_単独の移動リンクは共通のボタン(client):
    # 2026-10-31 ユーザー「少数派の文字リンクをまとめて統一」: トップの「すべて見る →」・環境デッキ／パックの全面の詳細の「一覧へ」は .nav-pill
    from pathlib import Path
    html = client.get("/").get_data(as_text=True)
    assert 'class="nav-pill new-cards-head-link"' in html and 'class="nav-pill new-cards-all"' in html
    assert html.count('class="nav-pill meta-back"') == 2
    root = Path(__file__).resolve().parent.parent
    theme = (root / "static/css/site-theme.css").read_text(encoding="utf-8")
    assert ".nav-pill{" in theme and ".nav-pill[hidden]{display:none}" in theme
    # スマホの購入候補はロゴの縦の中心（上の余白15px＋高さ46pxの中心 38px − 高さ36pxの半分）
    layout = (root / "static/css/price-layout.css").read_text(encoding="utf-8")
    assert ".nav-wishlist{position:absolute;top:20px;" in layout
