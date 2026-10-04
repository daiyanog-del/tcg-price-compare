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


@pytest.mark.parametrize("scrape", [scraper.scrape_kanabell, scraper.scrape_kanabell_buy])
def test_kanabell_sale_and_buy_drop_overrush(monkeypatch, scrape):
    """問題の商品は全状態で除外し、同名のOCGは販売・買取とも残す。"""
    hits = []
    for card_id, rarity in [("100283847", "Oラッシュ"), ("ocg", "ウルトラ")]:
        source = {"id": card_id, "name": "青眼の白龍", "rarity_abbreviation": rarity,
                  "sa_buying_price": 100, "sa_limit_flag": True}
        for rank, _ in scraper._KANABELL_CONDITIONS:
            source[f"{rank}_selling_price"] = 200
            source[f"{rank}_stock"] = 1
        hits.append({"_source": source})
    monkeypatch.setattr(scraper, "_KANABELL_CLOUD_ID", "test")
    monkeypatch.setattr(scraper, "_KANABELL_API_KEY", "test")
    monkeypatch.setattr(scraper, "_KANABELL_ES_URL", "https://example.invalid")
    response = SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                               json=lambda: {"hits": {"hits": hits}})
    monkeypatch.setattr(scraper.requests, "post", lambda *args, **kwargs: response)
    rows = scrape("青眼の白龍")
    assert len(rows) == (4 if scrape is scraper.scrape_kanabell else 1)
    assert all("id=ocg" in row["url"] for row in rows)


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
