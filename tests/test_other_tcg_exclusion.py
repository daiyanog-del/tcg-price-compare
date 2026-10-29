"""
tests/test_other_tcg_exclusion.py — 他TCG（遊戯王以外のカードゲーム）商品の除外

背景:
  2026-08-03 の棚卸しで、複数TCGを扱う店（トレコロCB・カードラボ）が、カード名の
  一致した **別ゲームの商品** を遊戯王カードの価格系列に混ぜていることが実測で判明した。
    - トレコロCB: 352行 / 5カード（ペガサス・真実の名・赤き竜・師弟の絆・宿命の決闘）
    - カードラボ: 4行 / 1カード（デュエマの「宿命の決闘」）
  レアリティ名のブラックリストでは根治できない。混入商品は遊戯王と共通のレアリティ名
  （ノーマル・スーパー・レア）でも入っており、「宿命の決闘」に至ってはデュエマ側と
  商品名が完全一致するため名前照合でも分離できないため。

除外キー（いずれも店側が持つゲーム種別の情報を使う）:
  - トレコロCB: 検索URLの `ct2`（販売 1010＝遊戯王 / 買取 2010＝遊戯王）
                URLに元からある `category=` は別軸で、1010 を入れると0件になる
  - カードラボ: 商品名の先頭ゲームタグ（【遊戯】/【DM】/【WS】/【SV】/【VG】/【LO】）
                《未開封》等が前置される商品があるため「最初に現れる【…】」で判定する
  - カーナベル: rarity_abbreviation の「ラッシュ」（ラッシュデュエル）と
                「ステンレス」（金属製記念カード）。商品名には印が出ない

実測（実装コードそのものでの新旧比較）:
  - トレコロ販売 84カード: 679→723件。消失52件は全て他TCG、遊戯王の消失0件。
    新規96件は他TCGに枠を食われていた遊戯王の取りこぼし解消
  - トレコロ買取 40カード: 303→390件。消失3件は全て他TCG
  - カードラボ販売 40カード: 308→304件。消失4件は全て他TCG（DM/SV/VG）
  - カードラボ買取 40カード: 92→91件。消失1件はデュエマ。《未開封》【遊戯】の巻き込み0件
"""

import sys
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
from scraper import (
    _TORECOLO_YGO_CT2,
    _TORECOLO_YGO_CT2_BUY,
    _KANABELL_EXCLUDED_RARITIES,
    _is_clabo_ygo,
    _parse_clabo_items,
    scrape_torecolo,
    scrape_torecolo_buy,
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda *_a, **_k: None)


# ── トレコロCB: 検索URLのカテゴリ絞り込み ──

def _capture_urls(monkeypatch):
    """safe_get をモックして、要求されたURLの列を集める"""
    urls = []

    def fake_get(url, *a, **k):
        urls.append(url)
        return None  # 1ページ目で打ち切り（URL生成だけを見る）

    monkeypatch.setattr(scraper, "safe_get", fake_get)
    return urls


def test_torecolo_sale_requests_ygo_category(monkeypatch):
    """販売は ct2=1010（遊戯王）を付けて検索する"""
    urls = _capture_urls(monkeypatch)
    scrape_torecolo("青眼の白龍")
    assert urls, "1ページ目のURLが組み立てられていない"
    assert f"&ct2={_TORECOLO_YGO_CT2}" in urls[0]
    assert _TORECOLO_YGO_CT2 == "1010"


def test_torecolo_buy_requests_ygo_buy_category(monkeypatch):
    """買取は販売と別体系の ct2=2010 を使う（1010 だと結果が丸ごと0件になる）"""
    urls = _capture_urls(monkeypatch)
    scrape_torecolo_buy("青眼の白龍")
    assert urls
    assert f"&ct2={_TORECOLO_YGO_CT2_BUY}" in urls[0]
    assert _TORECOLO_YGO_CT2_BUY == "2010"
    assert _TORECOLO_YGO_CT2_BUY != _TORECOLO_YGO_CT2


def test_torecolo_category_param_is_not_reused(monkeypatch):
    """既存の `category=` に値を入れてはいけない（別軸のパラメータで0件になる）"""
    urls = _capture_urls(monkeypatch)
    scrape_torecolo("青眼の白龍")
    assert "&category=&" in urls[0]


def test_torecolo_empty_ct2_restores_old_behaviour(monkeypatch):
    """ct2='' で旧挙動（全TCG横断）に戻せる — 新旧比較の検証用"""
    urls = _capture_urls(monkeypatch)
    scrape_torecolo("青眼の白龍", ct2="")
    assert "&ct2=" not in urls[0]


# ── カードラボ: 商品名のゲームタグ ──

@pytest.mark.parametrize("raw_name, expected", [
    # 遊戯王
    ("【遊戯】赤き竜【ウルトラ/☆12】DUNE-JP038", True),
    ("【遊戯】赤き竜 ケッツァーコアトル【シークレット/☆12】LOCR-JP007", True),
    # 状態が前置される商品（買取サイトに実在）。先頭一致だと落としてしまう
    ("《未開封》【遊戯】青眼の白龍【プレミアムゴールド/通常】LGB1-JPS02", True),
    ("《開封済み》【遊戯】青眼の白龍【ウルトラ/通常】SCB1-JPP01", True),
    # 他TCG
    ("【DM】宿命の決闘【VR】26EX2 30/89", False),          # デュエル・マスターズ
    ("【WS】赤き竜 ビィ【TD】GBF/S134-T01", False),         # ヴァイスシュヴァルツ
    ("【SV】ブランクカード(トークン)【その他】", False),         # シャドウバース
    ("【VG】デリヴァー・ペガサス【RR】DZ-BT07/043", False),   # ヴァンガード
    ("【LO】ペガサス組の級長 アリアンナ・ハートベル【KR】LO-6143-K", False),  # ロルカナ
    # タグが無い商品は遊戯王と断定できないので落とす
    ("宿命の決闘", False),
    ("", False),
])
def test_is_clabo_ygo(raw_name, expected):
    assert _is_clabo_ygo(raw_name) is expected


def _clabo_container(raw_name, price="80"):
    html = f"""
    <li><div class="inner_item_data">
      <span class="goods_name">{raw_name}</span>
      <span class="figure">{price}円</span>
      <p class="stock">在庫数3枚</p>
    </div></li>
    """
    return BeautifulSoup(html, "html.parser").select("li")


def test_clabo_parser_drops_other_tcg():
    """パーサが他TCGの同名カードを捨て、遊戯王だけを残す"""
    containers = (_clabo_container("【DM】宿命の決闘【VR】26EX2 30/89")
                  + _clabo_container("【遊戯】宿命の決闘【ノーマル/通常】DP24-JP014"))
    results = _parse_clabo_items("宿命の決闘", containers)
    assert len(results) == 1
    assert results[0]["code"] == "DP24-JP014"


def test_clabo_parser_old_behaviour_keeps_other_tcg():
    """require_game_tag=False で旧挙動（他TCGも拾う）に戻せる — 新旧比較の検証用

    2026-08-04 更新: 元の商品名「【DM】宿命の決闘【VR】26EX2 30/89」は、
    is_target_card の末尾境界チェックを区切り集合と同期させた修正により、
    タグ判定を切っても「26EX2…」が別カード名の続きとみなされて落ちるようになった。
    そのためタグ判定**だけ**が効く商品名（タグ除去後がカード名そのもの）に差し替える。
    """
    tag_only = _clabo_container("【DM】宿命の決闘【VR】")
    assert _parse_clabo_items("宿命の決闘", tag_only) == []
    assert len(_parse_clabo_items("宿命の決闘", tag_only, require_game_tag=False)) == 1

    # 型番付きの表記は require_game_tag に関係なく落ちる（末尾境界チェックの効果）
    with_code = _clabo_container("【DM】宿命の決闘【VR】26EX2 30/89")
    assert _parse_clabo_items("宿命の決闘", with_code) == []
    assert _parse_clabo_items("宿命の決闘", with_code, require_game_tag=False) == []


# ── カーナベル: レアリティ欄にだけ現れる別ゲーム／非紙カード ──

def test_kanabell_excluded_rarities():
    """ラッシュデュエルとステンレス製記念カードをレアリティ欄で弾く"""
    assert "ラッシュ" in _KANABELL_EXCLUDED_RARITIES
    assert "Oラッシュ" in _KANABELL_EXCLUDED_RARITIES
    assert "ステンレス" in _KANABELL_EXCLUDED_RARITIES
    # OCGで実在するレアリティを巻き込んでいないこと
    for keep in ("ウルトラ", "シークレット", "ノーマル", "スーパー",
                 "プリシク", "25thシークレット", "ミレニアムウルトラ"):
        assert keep not in _KANABELL_EXCLUDED_RARITIES


@pytest.mark.parametrize("fields, excluded", [
    ({"rarity_abbreviation": "Oラッシュ"}, True),
    ({"rarity_abbreviation": " Ｏラッシュ "}, True),
    ({"rarity_abbreviation": "ラッシュ"}, True),
    ({"rarity_abbreviation": "ステンレス"}, True),
    ({"rarity_abbreviation": "ウルトラ", "category2_abbr": "ラッシュデュエル"}, True),
    ({"rarity_abbreviation": "ウルトラ", "category3_abbr": "RD/KP01-JP001"}, True),
    ({"rarity_abbreviation": "ウルトラ", "category2_id": 999}, False),
    ({"rarity_abbreviation": "オーバーフレーム"}, False),
    ({"rarity_abbreviation": "グランドマスター"}, False),
    ({"rarity_abbreviation": "新規未登録レアリティ"}, False),
    ({"name": "ラッシュ・ウォリアー", "rarity_abbreviation": "ノーマル"}, False),
])
def test_kanabell_game_and_rarity_filter(fields, excluded):
    """明示的な別ゲーム情報だけを除外し、未知分類や正規OCGを巻き込まない。"""
    assert scraper._is_kanabell_excluded(fields) is excluded


def _kanabell_source(product_id, rarity, *, name="青眼の白龍", stock=1, slots=1, rarity_name=""):
    """新サイト（2026-10-24〜）の ES の商品の形。"""
    source = {
        "inventory_product_id": product_id, "product_name": name,
        "rank_s_purchase_price": 100, "acquisition_num_limit": slots,
        "shop_shelf_inventory_plus_before_storing_shop_shelf_inventory_num": 0,
        "shop_shelf_temporary_inventory_num": 0,
        "yg_card": {"serial_number": "LB-01", "image1_url": "https://yg-card-images.ka-nabell.com/card/card1_1.jpg",
                    "rarity": {"yg_rarity_abbreviation": rarity, "yg_rarity_name": rarity_name}},
    }
    for rank, _ in scraper._KANABELL_CONDITIONS:
        source[f"rank_{rank}_inventory"] = {"selling_price": 200, "inventory_num": stock}
    return source


def _patch_kanabell_es(monkeypatch, sources):
    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "https://example.invalid")
    monkeypatch.setattr(scraper, "_KANABELL_ES_API_KEY", "test")
    sent = []
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"hits": {"hits": [{"_source": s} for s in sources]}})
    monkeypatch.setattr(scraper.requests, "post", lambda *args, **kwargs: sent.append(kwargs) or response)
    return sent


@pytest.mark.parametrize("scrape", [scraper.scrape_kanabell, scraper.scrape_kanabell_buy])
def test_kanabell_sale_and_buy_drop_overrush(monkeypatch, scrape):
    """問題の商品は全状態で除外し、同名のOCGは販売・買取とも残す。"""
    _patch_kanabell_es(monkeypatch, [_kanabell_source("rush", "Oラッシュ"),
                                     _kanabell_source("tin", "ステンレス"),
                                     _kanabell_source("ocg", "ウルトラ")])
    rows = scrape("青眼の白龍")
    assert len(rows) == (5 if scrape is scraper.scrape_kanabell else 1)
    assert all(row["url"].endswith("/single/ocg") for row in rows)
    assert all(row["rarity"] == "ウルトラ" and row["code"] == "LB-01" for row in rows)


def test_kanabell_sale_conditions_and_sold_out(monkeypatch):
    """価格のある状態はすべて行にし、在庫0の状態は売切の行にする（旧サイトと同じ出し方）。"""
    partial = _kanabell_source("p", "ウルトラ", stock=0)
    partial["rank_b_inventory"] = {"selling_price": 150, "inventory_num": 2}
    partial["rank_s_inventory"] = {"selling_price": 0, "inventory_num": 0}  # 価格の無い状態は出さない
    _patch_kanabell_es(monkeypatch, [_kanabell_source("a", "ウルトラ"), partial])
    rows = scraper.scrape_kanabell("青眼の白龍")
    in_stock = [r for r in rows if r["url"].endswith("/single/a")]
    assert [r["condition"] for r in in_stock] == ["状態:S", "状態:A", "状態:B", "状態:C", "状態:D"]
    assert all(r["url"] == "https://www.ka-nabell.com/ec/sell/yugioh/single/a" and not r["sold_out"]
               for r in in_stock)
    got = [(r["condition"], r["sold_out"], r["stock"]) for r in rows if r["url"].endswith("/single/p")]
    assert got == [("状態:A", True, 0), ("状態:B", False, 2), ("状態:C", True, 0), ("状態:D", True, 0)]


# 新サイトのレアリティ一覧（/ec/sell/api/yugioh/rarities・2026-10-29 取得）の全組と、取得時の正規化結果。
# 正規名の無いもの（ウルシク・レアパラ・MANGA系）は旧サイトと同じく生の略号のまま。ステンレスは除外対象
_KANABELL_RARITY_TABLE = [
    ('オバフレプリシク', 'オーバーフレームプリズマティックシークレットレア', 'OFプリシク'),
    ('オバフレシク', 'オーバーフレームシークレットレア', 'OFシークレット'),
    ('オバフレウル', 'オーバーフレームウルトラレア', 'OFウルトラ'),
    ('QCシク', 'クォーターセンチュリーシークレットレア', '25thシークレット'),
    ('プリシク', 'プリズマティックシークレットレア', 'プリシク'),
    ('10000シク', '10000シークレットレア', '10000シークレット'),
    ('20thシク', '20thシークレットレア', '20thシークレット'),
    ('EXシク', 'エクストラシークレットレア', 'EXシークレット'),
    ('ウルシク', 'ウルトラシークレットレア', 'ウルシク'),
    ('シク', 'シークレットレア', 'シークレット'),
    ('ミレニアムシク', 'ミレニアム仕様シークレットレア', 'ミレニアムシークレット'),
    ('ミレニアムゴールド', 'ミレニアム仕様ゴールドレア', 'ミレニアムゴールド'),
    ('ミレニアムウルトラ', 'ミレニアム仕様ウルトラレア', 'ミレニアムウルトラ'),
    ('ミレニアムスーパー', 'ミレニアム仕様スーパーレア', 'ミレニアムスーパー'),
    ('ミレニアムレア', 'ミレニアム仕様', 'ミレニアム'),
    ('KCウル', 'KC仕様ウルトラレア', 'KCウルトラ'),
    ('KCレア', 'KC仕様レア', 'KCレア'),
    ('KCノーマル', 'KC仕様ノーマル', 'KCノーマル'),
    ('EXシクパラ', 'パラレル仕様エクストラシークレットレア', 'EXシークレットパラレル'),
    ('シクパラ', 'パラレル仕様シークレットレア', 'シークレットパラレル'),
    ('ホロパラ', 'パラレル仕様ホログラフィックレア', 'ホログラフィック'),
    ('ウルパラ', 'パラレル仕様ウルトラレア', 'ウルトラパラレル'),
    ('スーパラ', 'パラレル仕様スーパーレア', 'スーパーパラレル'),
    ('レアパラ', 'パラレル仕様レア', 'レアパラ'),
    ('ノーパラ', 'パラレル仕様ノーマル', 'ノーマルパラレル'),
    ('ホロ', 'ホログラフィックレア', 'ホログラフィック'),
    ('プレゴル', 'プレミアムゴールドレア', 'プレミアムゴールド'),
    ('ゴルシク', 'ゴールドシークレットレア', 'ゴールドシークレット'),
    ('ゴールド', 'ゴールドレア', 'ゴールド'),
    ('コレレア', 'コレクターズレア', 'コレクターズ'),
    ('アルティメット', 'アルティメットレア', 'アルティメット'),
    ('ウルトラ', 'ウルトラレア', 'ウルトラ'),
    ('スーパー', 'スーパーレア', 'スーパー'),
    ('レア', 'レア', 'レア'),
    ('－', 'ノーマルレア', 'ノーマルレア'),
    ('ノーマル', 'ノーマル', 'ノーマル'),
    ('グラマス', 'グランドマスターレア', 'グランドマスター'),
    ('MANGAシク', 'MANGA-STYLEシークレットレア', 'MANGAシク'),
    ('MANGAアル', 'MANGA-STYLEアルティメットレア', 'MANGAアル'),
    ('MANGAウル', 'MANGA-STYLEウルトラレア', 'MANGAウル'),
    ('ステンレス', 'ステンレス仕様', 'ステンレス'),
]


@pytest.mark.parametrize("abbr, name, expected", _KANABELL_RARITY_TABLE)
def test_kanabell_rarity_table(abbr, name, expected):
    assert scraper._kanabell_rarity({"rarity": {"yg_rarity_abbreviation": abbr, "yg_rarity_name": name}}) == expected


def test_kanabell_unknown_abbr_falls_back_to_known_full_name():
    """略号が rarity.py に無くても、正式名が既知なら正規名にする。"""
    card = {"rarity": {"yg_rarity_abbreviation": "未知の略号", "yg_rarity_name": "ミレニアムウルトラレア"}}
    assert scraper._kanabell_rarity(card) == "ミレニアムウルトラ"


def test_kanabell_stainless_full_name_is_excluded():
    assert scraper._is_kanabell_excluded({"rarity_abbreviation": "未知", "rarity_name": "ステンレス仕様"})


def test_kanabell_image_needs_matching_name(monkeypatch):
    """予備の画像は名前が一致する商品だけを使い、注記の無い商品を優先する。"""
    def src(name, n):
        return {"product_name": name, "yg_card": {"image1_url": f"https://yg-card-images.ka-nabell.com/card/card{n}_1.jpg"}}
    _patch_kanabell_es(monkeypatch, [src("灰流うらら(イラスト違い)", 1), src("灰流うらら", 2)])
    assert scraper.kanabell_card_image_url("灰流うらら").endswith("card2_1.jpg")
    _patch_kanabell_es(monkeypatch, [src("増殖するG", 3)])  # 名前の一部が当たっただけの別カード
    assert scraper.kanabell_card_image_url("G") == ""


@pytest.mark.parametrize("scrape", [scraper.scrape_kanabell, scraper.scrape_kanabell_buy])
def test_kanabell_http_error_is_fetch_error(monkeypatch, scrape):
    """ES の失敗は「在庫なし」ではなく取得失敗として数える。"""
    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "https://example.invalid")
    monkeypatch.setattr(scraper, "_KANABELL_ES_API_KEY", "test")

    def boom(*args, **kwargs):
        raise scraper.requests.ConnectionError("down")
    monkeypatch.setattr(scraper.requests, "post", boom)
    errors = []
    monkeypatch.setattr(scraper, "_note_fetch_error", lambda: errors.append(1))
    assert scrape("青眼の白龍") == []
    assert errors == [1]


def test_kanabell_buy_needs_open_slots(monkeypatch):
    """買取枠の残りが0の商品（買取終了）は古い買取価格が残っていても使わない。"""
    full = _kanabell_source("full", "ウルトラ", slots=2)
    full["shop_shelf_inventory_plus_before_storing_shop_shelf_inventory_num"] = 1
    full["shop_shelf_temporary_inventory_num"] = 1
    _patch_kanabell_es(monkeypatch, [full, _kanabell_source("open", "ウルトラ", slots=3)])
    rows = scraper.scrape_kanabell_buy("青眼の白龍")
    assert [r["url"] for r in rows] == ["https://www.ka-nabell.com/ec/buy/yugioh/single/open"]
    assert rows[0]["price"] == 100


def test_kanabell_rarity_dash_falls_back_to_full_name(monkeypatch):
    """略号がダッシュだけ（「－」＝ノーマルレア）の商品は正式名で正規化する。"""
    _patch_kanabell_es(monkeypatch, [_kanabell_source("nr", "－", rarity_name="ノーマルレア")])
    rows = scraper.scrape_kanabell_buy("青眼の白龍")
    assert rows[0]["rarity"] == "ノーマルレア"


def test_kanabell_query_limits_to_published_and_sellable(monkeypatch):
    """販売は公開期間内・購入上限0でない商品だけを、語ごとに AND で探す。"""
    sent = _patch_kanabell_es(monkeypatch, [])
    assert scraper.scrape_kanabell("No.39 希望皇ホープ") == []
    query = sent[0]["json"]["query"]["bool"]
    assert len(query["must"]) == 2  # 「No.39」と「希望皇ホープ」
    assert query["must_not"] == [{"term": {"selling_num_limit": 0}}]
    published = query["filter"][0]["bool"]
    assert published["must"] == [{"range": {"publish_from": {"lte": "now"}}}]
    assert sent[0]["headers"]["Authorization"] == "ApiKey test"


def test_kanabell_missing_config_is_fetch_error(monkeypatch):
    """接続情報が無いときは「在庫なし」ではなく取得失敗として扱う。"""
    monkeypatch.setattr(scraper, "_KANABELL_ES_ENDPOINT", "")
    errors = []
    monkeypatch.setattr(scraper, "_note_fetch_error", lambda: errors.append(1))
    assert scraper.scrape_kanabell("青眼の白龍") == []
    assert scraper.scrape_kanabell_buy("青眼の白龍") == []
    assert len(errors) == 2


@pytest.mark.parametrize("buyback", [False, True])
def test_kanabell_old_cache_drops_overrush(tmp_path, monkeypatch, buyback):
    """期限内の旧キャッシュからの混入も防ぎ、他店とOCGの行を保持する。"""
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR" if buyback else "CACHE_DIR", tmp_path)
    card_name = "青眼の白龍"
    key = scraper._buyback_cache_key(card_name) if buyback else scraper._cache_key(card_name)
    ocg = {"shop": "カーナベル", "name": card_name, "rarity": "ウルトラ"}
    other = {"shop": "他店", "name": card_name, "rarity": "ウルトラ"}
    data = {"shops": {
        "カーナベル": {"timestamp": datetime.now().isoformat(), "results": [
            {"shop": "カーナベル", "name": card_name, "rarity": "Oラッシュ"}, ocg]},
        "他店": {"timestamp": datetime.now().isoformat(), "results": [other]},
    }}
    (tmp_path / f"{key}.json").write_text(json.dumps(data), encoding="utf-8")
    get_shops = scraper.buyback_cache_get_shops if buyback else scraper.cache_get_shops
    get_all = scraper.buyback_cache_get if buyback else scraper.cache_get
    hit, missing = get_shops(card_name, ["カーナベル", "他店"])
    assert not missing
    assert hit == {"カーナベル": [ocg], "他店": [other]}
    assert get_all(card_name) == [ocg, other]
