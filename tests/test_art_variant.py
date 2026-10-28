"""art_variant（絵と言語版の印）のテスト。商品名・リンクは 2026-10-28 に各店から取れた実際の値。"""
import pytest

import art_variant as av


def _row(shop, name, rarity="シークレット", url=""):
    return {"shop": shop, "name": name, "rarity": rarity, "url": url}


@pytest.mark.parametrize("card,shop,name,url,art", [
    # 遊々亭: 無印・既存＝元、イラスト違い版・海外版＝違う（全角括弧もある）
    ("灰流うらら", "遊々亭", "灰流うらら", "", av.ART_BASE),
    ("灰流うらら", "遊々亭", "灰流うらら(既存イラスト)", "", av.ART_BASE),
    ("灰流うらら", "遊々亭", "灰流うらら（イラスト違い版）", "", av.ART_ALT),
    ("灰流うらら", "遊々亭", "灰流うらら(海外版イラスト)", "", av.ART_ALT),
    ("ブラック・マジシャン", "遊々亭", "ブラック・マジシャン(カラー/MANGA-STYLE)", "", av.ART_MANGA_COLOR),
    # カードラボ
    ("灰流うらら", "カードラボ", "灰流うらら(通常イラスト)", "", av.ART_BASE),
    ("灰流うらら", "カードラボ", "灰流うらら(新イラスト)", "", av.ART_ALT),
    ("ブラック・マジシャン", "カードラボ", "ブラック・マジシャン〔漫画/モノクロ版〕", "", av.ART_MANGA_MONO),
    ("ブラック・マジシャン", "カードラボ", "ブラック・マジシャン(イラスト違い(5)/SJMP版)", "", ""),  # 3種類以上の絵の番号つきは判別しない
    # トレコロ: 商品番号の S の決まり（名前の言葉より優先）
    ("灰流うらら", "トレコロCB", "灰流うらら[両腕開き]", "https://www.torecolo.jp/shop/g/gQCAC-JP050SE/", av.ART_BASE),
    ("灰流うらら", "トレコロCB", "灰流うらら[両手閉じてる]", "https://www.torecolo.jp/shop/g/gQCAC-JP050SSE/", av.ART_ALT),
    ("灰流うらら", "トレコロCB", "灰流うらら[両手閉じてる]", "https://www.torecolo.jp/shop/g/gQCAC-JP050SQSE/", av.ART_ALT),
    ("灰流うらら", "トレコロCB", "灰流うらら[新イラスト]", "https://www.torecolo.jp/shop/g/gRC03-JP010SPGR-K/", av.ART_ALT),
    ("灰流うらら", "トレコロCB", "灰流うらら", "https://www.torecolo.jp/shop/g/gRC04-JP009HR/", av.ART_BASE),
    ("灰流うらら", "トレコロCB", "灰流うらら", "https://www.torecolo.jp/shop/g/gPAC1-JP016SR/", av.ART_BASE),  # SR はスーパーレア（S＋R ではない）
    # カードラッシュ: カードごとの表
    ("灰流うらら", "カードラッシュ", "灰流うらら(立ち)", "", av.ART_BASE),
    ("灰流うらら", "カードラッシュ", "灰流うらら(座り)", "", av.ART_ALT),
    ("I：Pマスカレーナ", "カードラッシュ", "IPマスカレーナ(バイク有)", "", av.ART_ALT),
    ("ドロール＆ロックバード", "カードラッシュ", "ドロールロックバード(鳥右上)", "", av.ART_BASE),
    ("灰流うらら", "カードラッシュ", "灰流うらら", "", av.ART_BASE),
    ("アクセスコード・トーカー", "カードラッシュ", "アクセスコードトーカー(接敵/赤大地)", "", ""),  # 表に無いカードの描写は判別しない
    ("ブラック・マジシャン", "カードラッシュ", "ブラックマジシャン(未開封)", "", av.ART_BASE),  # 状態の印は絵の言葉に含めない
    # まんぞく屋は絵を書き分けない
    ("灰流うらら", "まんぞく屋", "灰流うらら", "", ""),
])
def test_classify_art(card, shop, name, url, art):
    assert av.classify(_row(shop, name, url=url), card)[0] == art


def test_green_ver_from_rarity_or_name():
    # カードラッシュはレアリティ側、トレコロは名前側に書く
    assert av.classify(_row("カードラッシュ", "ブラックマジシャン", rarity="クォーターセンチュリーシークレットGREEN Ver."),
                       "ブラック・マジシャン")[0] == av.ART_GREEN
    assert av.classify(_row("トレコロCB", "ブラック・マジシャン[GREEN Ver]", url="https://www.torecolo.jp/shop/g/gTD01-JP001QSE/"),
                       "ブラック・マジシャン")[0] == av.ART_GREEN


def test_edition():
    art, edition = av.classify(_row("カードラッシュ", "☆アジア☆灰流うらら(座り)"), "灰流うらら")
    assert (art, edition) == (av.ART_ALT, "アジア版")
    assert av.classify(_row("カードラッシュ", "☆旧アジア☆青眼の白龍(1stEdition)"), "青眼の白龍")[1] == "アジア版"
    assert av.classify(_row("カードラッシュ", "☆英語版☆青眼の白龍"), "青眼の白龍")[1] == "英語版"
    assert av.classify(_row("カードラッシュ", "☆バンダイ☆青眼の白龍"), "青眼の白龍")[1] == ""


def test_with_edition_is_idempotent():
    assert av.with_edition("シークレット", "アジア版") == "シークレット（アジア版）"
    assert av.with_edition("シークレット（アジア版）", "アジア版") == "シークレット（アジア版）"
    assert av.with_edition("シークレット", "") == "シークレット"


def test_build_done_annotates_and_splits_edition():
    import app as app_module

    rows = [
        {"shop": "遊々亭", "name": "灰流うらら(イラスト違い版)", "rarity": "シークレット", "price": 980, "sold_out": False, "url": ""},
        {"shop": "カードラッシュ", "name": "☆アジア☆灰流うらら(立ち)", "rarity": "シークレット", "price": 280, "sold_out": False, "url": ""},
        {"shop": "カードラッシュ", "name": "ブラックマジシャン", "rarity": "クォーターセンチュリーシークレットGREEN Ver.",
         "price": 19800, "sold_out": False, "url": ""},
    ]
    done = app_module._build_done(rows, "", "灰流うらら")
    got = [(r["rarity"], r["art"], r["edition"]) for r in done["results"]]
    assert got == [
        ("シークレット", av.ART_ALT, ""),
        ("シークレット（アジア版）", av.ART_BASE, "アジア版"),
        ("25thシークレット", av.ART_GREEN, ""),
    ]
    # アジア版は日本版と同じ枠の最安に入れない
    assert done["by_rarity"]["シークレット"]["price"] == 980
