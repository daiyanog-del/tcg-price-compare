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
from pathlib import Path

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
    """require_game_tag=False で旧挙動（他TCGも拾う）に戻せる — 新旧比較の検証用"""
    containers = _clabo_container("【DM】宿命の決闘【VR】26EX2 30/89")
    assert _parse_clabo_items("宿命の決闘", containers) == []
    assert len(_parse_clabo_items("宿命の決闘", containers, require_game_tag=False)) == 1


# ── カーナベル: レアリティ欄にだけ現れる別ゲーム／非紙カード ──

def test_kanabell_excluded_rarities():
    """ラッシュデュエルとステンレス製記念カードをレアリティ欄で弾く"""
    assert "ラッシュ" in _KANABELL_EXCLUDED_RARITIES
    assert "ステンレス" in _KANABELL_EXCLUDED_RARITIES
    # OCGで実在するレアリティを巻き込んでいないこと
    for keep in ("ウルトラ", "シークレット", "ノーマル", "スーパー",
                 "プリシク", "25thシークレット", "ミレニアムウルトラ"):
        assert keep not in _KANABELL_EXCLUDED_RARITIES
