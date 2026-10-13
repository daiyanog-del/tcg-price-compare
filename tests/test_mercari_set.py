"""
tests/test_mercari_set.py — 同じカードの2〜3枚組（セット）の1枚あたり価格

セットは単品の統計（count/min/median）に混ぜず、レアリティ別に
set={unit_min, unit_min_qty, count} として別に持つ。実通信はしない。
題名のうち「（実題名）」と付けたものは tests/fixtures/mercari の実データにある題名。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import mercari

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "mercari"
KNOWN_NAMES = json.loads((REPO_ROOT / "data" / "cardnames_ja.json").read_text(encoding="utf-8"))
REAL_TITLES = {
    it["name"]
    for f in FIXTURE_DIR.glob("*.json")
    for it in json.loads(f.read_text(encoding="utf-8"))["response"]["items"]
}


def _item(name, price=1000, **kw):
    it = {"id": "m1", "name": name, "price": str(price), "status": "ITEM_STATUS_ON_SALE",
          "categoryId": 1152, "auction": None, "itemType": "ITEM_TYPE_MERCARI"}
    it.update(kw)
    return it


def _eval(card, title):
    it = _item(title)
    matcher = mercari.build_name_matcher(card, [it], KNOWN_NAMES)
    return mercari.evaluate_listing(card, it, matcher)


def test_実題名の前提():
    """このファイルが「実題名」とした題名がfixtureに存在する（取得し直しで消えたら気づけるように）。"""
    for t in REAL.values():
        assert t in REAL_TITLES, t


REAL = {
    "g3": "遊戯王 灰流うらら 3枚セット　ノーマル",
    "g3b": "【初版】遊戯王 灰流うらら スーパー 3枚セットMACR",
    "g3c": "遊戯王　灰流うらら　ウルトラレア3枚 QCAC",
    "g3d": "灰流うらら　スーパーレア　3枚",
    "g2": "遊戯王 灰流うらら 2枚セット　ノーマル",
    "g2b": "遊戯王 灰流うらら エクストラシークレット 2枚",
    "g3zen": "遊戯王 灰流うらら ウルトラレア　３枚",
    "n4": "遊戯王 灰流うらら 4枚セット シークレット",
    "n16": "遊戯王　灰流うらら　16枚",
    "n10": "遊戯王　灰流うらら　ノーマル10枚",
    "other": "遊戯王　灰流うらら　ノーマル　２枚　増殖するg　1枚",
    "mix": "灰流うらら　スーパー2枚　増殖するG スーパー　1枚",
    "tai": "神の宣告 ウルパラ　3体",
    "setonly": "遊戯王　灰流うららセット",
    "sleeve": "遊戯王 灰流うらら スリーブ 100枚",
    "asia": "灰流うらら　英語　ウルトラ　3枚",
}


@pytest.mark.parametrize("key,qty", [
    ("g3", 3), ("g3b", 3), ("g3c", 3), ("g3d", 3), ("g2", 2), ("g2b", 2), ("g3zen", 3),
])
def test_実題名_同一カードの2_3枚組を拾う(key, qty):
    assert _eval("灰流うらら", REAL[key]) == (None, qty)


@pytest.mark.parametrize("key,reason", [
    ("other", "name_mismatch"),           # 他カード名の混在
    ("mix", "name_mismatch"),
    ("setonly", "multi"),                 # 「セット」だけで枚数が読めない
    ("sleeve", "multi"), ("asia", "overseas"),   # 海外版は3枚でも除外
])
def test_実題名_拾わない(key, reason):
    assert _eval("灰流うらら", REAL[key])[0] == reason


def test_実題名_3体は拾わない():
    assert _eval("神の宣告", REAL["tai"]) == ("multi", 1)


@pytest.mark.parametrize("title,qty", [
    ("遊戯王 灰流うらら 3枚", 3),
    ("遊戯王 灰流うらら 3枚セット", 3),
    ("遊戯王 灰流うらら 3枚組", 3),
    ("遊戯王 灰流うらら ×3", 3),
    ("遊戯王 灰流うらら x3", 3),
    ("遊戯王 灰流うらら X3", 3),
    ("遊戯王 灰流うらら X 3", 3),
    ("遊戯王 灰流うらら ✖️3", 3),
    ("遊戯王 灰流うらら ３枚", 3),            # 全角数字
    ("遊戯王 灰流うらら ×２", 2),
    ("遊戯王 灰流うらら 三枚", 3),            # 漢数字
    ("遊戯王 灰流うらら 二枚セット", 2),
    ("遊戯王 灰流うらら 2枚組", 2),
    ("遊戯王 灰流うらら 3枚 ×3", 3),          # 同じ枚数の重複表記は食い違いではない
])
def test_枚数の表記ゆれを読む(title, qty):
    assert _eval("灰流うらら", title) == (None, qty)


@pytest.mark.parametrize("title", [
    "遊戯王 灰流うらら 4枚セット",
    "遊戯王 灰流うらら ×4",
    "遊戯王 灰流うらら 十枚",
    "遊戯王 灰流うらら ノーマル各3枚",
    "遊戯王 灰流うらら 3枚 計6枚",
    "遊戯王 灰流うらら まとめ 3枚",
    "遊戯王 灰流うらら 他 3枚",
    "遊戯王 灰流うらら 関連 3枚",
    "遊戯王 灰流うらら デッキ 3枚",
    "遊戯王 灰流うらら パーツ 3枚",
    "遊戯王 灰流うらら 詰め合わせ 3枚",
    "遊戯王 灰流うらら セット",
    "遊戯王 灰流うらら 2枚 3枚",              # 枚数表記が食い違う
    "遊戯王 灰流うらら 3枚 ×2",
    "遊戯王 灰流うらら 3体",
    "遊戯王 灰流うらら 3枚 3体",
])
def test_拾わない題名は従来どおり複数枚として除外(title):
    assert _eval("灰流うらら", title) == ("multi", 1)


@pytest.mark.parametrize("title,reason", [
    ("遊戯王 灰流うらら 3枚 無限泡影", "name_mismatch"),
    ("遊戯王 灰流うらら 3枚 スリーブ付き", None),        # 付属表記は除外しない
    ("遊戯王 灰流うらら 3枚 デュエリストカードプロテクター", "supply"),
    ("遊戯王 灰流うらら 3枚 アジア版", "overseas"),
    ("遊戯王 灰流うらら 3枚 PSA10", "graded"),
    ("遊戯王 灰流うらら 3枚 ○○様専用", "dedicated"),
    ("遊戯王 灰流うらら 3枚 バンダイ版", "non_ocg"),
])
def test_セットでも他の除外理由は優先される(title, reason):
    got, qty = _eval("灰流うらら", title)
    assert got == reason
    if reason is None:
        assert qty == 3


def test_1枚や枚数なしは単品():
    assert _eval("灰流うらら", "遊戯王 灰流うらら 1枚") == (None, 1)
    assert _eval("灰流うらら", "遊戯王 灰流うらら ウルトラ") == (None, 1)
    assert _eval("灰流うらら", "遊戯王 灰流うらら ×1") == (None, 1)


def test_exclusion_reasonは従来どおりセットをmultiで除外する():
    it = _item("遊戯王 灰流うらら 3枚セット")
    assert mercari.exclusion_reason("灰流うらら", it, mercari.build_name_matcher("灰流うらら", [it], KNOWN_NAMES)) == "multi"


# ── 集計 ──

def _agg(items, card="灰流うらら"):
    return mercari.aggregate(card, items, KNOWN_NAMES)


def test_1枚あたり価格は四捨五入():
    a = _agg([_item("灰流うらら ウルトラ 3枚セット", 1000), _item("灰流うらら ウルトラ 2枚", 1001)])
    s = a["by_rarity"]["ウルトラ"]["set"]
    assert s == {"unit_min": 333, "unit_min_qty": 3, "count": 2}    # 1000/3=333.3 と 1001/2=500.5→501


def test_半端の四捨五入は切り上げ():
    a = _agg([_item("灰流うらら ウルトラ 2枚", 1001)])
    assert a["by_rarity"]["ウルトラ"]["set"]["unit_min"] == 501


def test_1枚あたりが同額なら3枚組を優先():
    a = _agg([_item("灰流うらら ウルトラ 2枚", 600), _item("灰流うらら ウルトラ 3枚", 900)])
    assert a["by_rarity"]["ウルトラ"]["set"] == {"unit_min": 300, "unit_min_qty": 3, "count": 2}
    a = _agg([_item("灰流うらら ウルトラ 3枚", 900), _item("灰流うらら ウルトラ 2枚", 600)])   # 並びを替えても同じ
    assert a["by_rarity"]["ウルトラ"]["set"]["unit_min_qty"] == 3


def test_安い方のunit_minを採る():
    a = _agg([_item("灰流うらら ウルトラ 2枚", 800), _item("灰流うらら ウルトラ 3枚", 1500)])
    assert a["by_rarity"]["ウルトラ"]["set"]["unit_min"] == 400
    assert a["by_rarity"]["ウルトラ"]["set"]["unit_min_qty"] == 2


def test_単品の統計にセットを混ぜない():
    items = [_item("灰流うらら ウルトラ", 1000), _item("灰流うらら ウルトラ", 1200), _item("灰流うらら ウルトラ", 1400),
             _item("灰流うらら ウルトラ 3枚セット", 300)]
    row = _agg(items)["by_rarity"]["ウルトラ"]
    assert row["count"] == 3 and row["min"] == 1000 and row["median"] == 1200
    assert row["min_is_floor"] is False
    assert row["set"] == {"unit_min": 100, "unit_min_qty": 3, "count": 1}


def test_セットしか無いレアリティも行として出す():
    row = _agg([_item("灰流うらら スーパー 3枚", 900)])["by_rarity"]["スーパー"]
    assert row["count"] == 0 and row["min"] is None and row["median"] is None
    assert row["min_is_floor"] is False
    assert row["set"]["unit_min"] == 300


def test_セットが無ければsetはnull():
    assert _agg([_item("灰流うらら ウルトラ", 1000)])["by_rarity"]["ウルトラ"]["set"] is None


def test_レアリティ判定は単品と同じ_複数レアリティ語は判定できず():
    a = _agg([_item("灰流うらら 3枚 シークレット ノーマル スーパー", 900)])
    assert a["by_rarity"] == {}
    assert a["unclassified_set"] == 1 and a["unclassified_single"] == 0


def test_判定できずは単品とセットの内訳を持つ():
    a = _agg([_item("灰流うらら", 500), _item("灰流うらら", 600), _item("灰流うらら 3枚セット", 900),
              _item("灰流うらら ウルトラ", 700), _item("灰流うらら ウルトラ 2枚", 800)])
    assert a["unclassified_single"] == 2 and a["unclassified_set"] == 1
    assert a["unclassified_count"] == 3
    assert a["total_considered"] == 5


def test_セットの価格に最低出品価格の印は付けない():
    a = _agg([_item("灰流うらら ウルトラ 3枚", 900)])     # 1枚あたり300円でも min_is_floor は単品のみ
    assert a["by_rarity"]["ウルトラ"]["min_is_floor"] is False


def test_by_rarityは単品とセットを合わせたレアリティ順():
    a = _agg([_item("灰流うらら ノーマル", 100), _item("灰流うらら シークレット 3枚", 900),
              _item("灰流うらら ウルトラ", 100)])
    assert list(a["by_rarity"]) == ["シークレット", "ウルトラ", "ノーマル"]
