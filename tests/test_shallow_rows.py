"""浅い取り方の結果のうち記録に書く行（price_persist.shallow_rows_to_write）と、保存するカード名のそろえ（2026-11-01）。"""
import app as app_module
from price_persist import shallow_rows_to_write

TODAY, YDAY = "2026-11-01", "2026-10-31"


def _row(shop, rarity, mp, mpa=None, day=TODAY, code="", url=None):
    return {"card_name": "C", "shop": shop, "rarity": rarity, "min_price": mp,
            "min_price_any": mpa if mpa is not None else mp, "code": code, "url": url, "recorded_at": day}


def test_new_card_writes_all():
    new = [_row("トレコロCB", "レア", 500), _row("トレコロCB", "ウルトラ", 900)]
    assert shallow_rows_to_write(new, [], TODAY) == (new, [])


def test_only_yesterday_writes_nothing():
    new = [_row("トレコロCB", "レア", 100)]
    assert shallow_rows_to_write(new, [_row("トレコロCB", "OFプリシク", 24800, day=YDAY)], TODAY) == ([], [])


def test_today_adds_missing_rarity_and_lowers_only():
    existing = [_row("カードラボ", "レア", 500, 450, code="A", url="u1"),
                _row("カードラボ", "ウルトラ", 900)]
    new = [_row("カードラボ", "レア", 600, 400, code="B", url="u2"),   # 状態を問わない最安だけ下がった
           _row("カードラボ", "ウルトラ", 950),                          # 高い → 書かない
           _row("カードラボ", "シークレット", 3000)]                     # 当日に無い → 足す
    adds, lowers = shallow_rows_to_write(new, existing, TODAY)
    assert [r["rarity"] for r in adds] == ["シークレット"], "当日に無いレアリティは足す（上書きしない書き方）"
    assert len(lowers) == 1
    rare = lowers[0]
    assert (rare["min_price"], rare["code"], rare["min_price_any"], rare["url"]) == (500, "A", 400, "u2")


def test_other_shop_records_do_not_count():
    new = [_row("トレコロCB", "レア", 500)]
    assert shallow_rows_to_write(new, [_row("カードラボ", "レア", 800, day=YDAY)], TODAY) == (new, [])


def test_persist_card_name_matches_collection():
    app_module._load_cardnames()
    assert app_module._persist_card_name("Ｎｏ.９６ ブラック・マリオネッター") == "No.96 ブラック・マリオネッター"
    # 正式名が全角の数字を持つカードは正式名に戻る（収集の canonicalize_tracked_name と同じ）
    assert app_module._persist_card_name("K9－17号 イヅナ") == "K９－１７号 イヅナ"


def test_null_existing_prices_are_lowered():
    existing = [{**_row("カードラボ", "レア", 500), "min_price_any": None}]
    adds, lowers = shallow_rows_to_write([_row("カードラボ", "レア", 600)], existing, TODAY)
    assert adds == [] and lowers[0]["min_price"] == 500 and lowers[0]["min_price_any"] == 600


def test_persist_card_name_keeps_official_names():
    """正式名の一覧そのものの名前は変えない（一覧に全角・半角だけ違う重複がある。reviewer 中1）"""
    app_module._load_cardnames()
    official = [n for n in app_module._cardnames_set if any("０" <= ch <= "９" or "Ａ" <= ch <= "Ｚ" for ch in n)]
    assert official
    assert all(app_module._persist_card_name(n) == n for n in official)
