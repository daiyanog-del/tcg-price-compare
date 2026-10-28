"""art_variant（絵と言語版の印）のテスト。商品名・リンクは 2026-10-28 に各店から取れた実際の値。"""
import pytest

import art_variant as av
from rarity import normalize_rarity

TC = "https://www.torecolo.jp/shop/g/g{}/"


def _row(shop, name, rarity="シークレット", url=""):
    return {"shop": shop, "name": name, "rarity": rarity, "url": url}


@pytest.mark.parametrize("card,shop,name,url,art", [
    # 遊々亭: 無印・既存＝元、イラスト違い版・海外版＝違う（全角括弧もある）
    ("灰流うらら", "遊々亭", "灰流うらら", "", av.ART_BASE),
    ("灰流うらら", "遊々亭", "灰流うらら(既存イラスト)", "", av.ART_BASE),
    ("灰流うらら", "遊々亭", "灰流うらら（イラスト違い版）", "", av.ART_ALT),
    ("灰流うらら", "遊々亭", "灰流うらら(海外版イラスト)", "", av.ART_ALT),
    ("ブラック・マジシャン", "遊々亭", "ブラック・マジシャン(カラー/MANGA-STYLE)", "", av.ART_MANGA_COLOR),
    ("ブラック・マジシャン", "遊々亭", "ブラック・マジシャン(モノクロ/MANGA-STYLE)", "", av.ART_MANGA_MONO),
    ("ブラック・マジシャン", "遊々亭", "ブラック・マジシャン(MANGA-STYLE)", "", ""),  # カラーかモノクロか書いていない
    # カードラボ
    ("灰流うらら", "カードラボ", "灰流うらら(通常イラスト)", "", av.ART_BASE),
    ("灰流うらら", "カードラボ", "灰流うらら(新イラスト)", "", av.ART_ALT),
    ("ブラック・マジシャン", "カードラボ", "ブラック・マジシャン〔漫画/モノクロ版〕", "", av.ART_MANGA_MONO),
    ("ブラック・マジシャン", "カードラボ", "ブラック・マジシャン(イラスト違い(5)/SJMP版)", "", ""),  # 3種類以上の絵の番号つきは判別しない
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


@pytest.mark.parametrize("name,pid,rarity,art", [
    # そのレアリティのいつもの末尾＝元、S＋いつもの末尾＝違う（商品名の言葉より商品番号を見る）
    ("灰流うらら[両腕開き]", "QCAC-JP050SE", "シークレット", av.ART_BASE),
    ("灰流うらら[両手閉じてる]", "QCAC-JP050SSE", "シークレット", av.ART_ALT),
    ("灰流うらら[両手閉じてる]", "QCAC-JP050SQSE", "25thシークレット", av.ART_ALT),
    ("灰流うらら[新イラスト]", "RC03-JP010SPGR-K", "プレミアムゴールド", av.ART_ALT),
    ("灰流うらら", "RC04-JP009HR", "ホログラフィック", av.ART_BASE),
    ("灰流うらら", "PAC1-JP016SR", "スーパー", av.ART_BASE),
    ("ブラック・マジシャン[漫画カラー]", "YAC1-JP071S", "ウルトラ", av.ART_MANGA_COLOR),
    ("灰流うらら", "RC04-JP009S", "ウルトラ", av.ART_ALT),  # ウルトラはいつもの末尾が「なし」で、違う絵は S
    # 判別しないもの
    ("灰流うらら[新イラスト]", "RC04-JP009HR", "ホログラフィック", ""),  # 商品名の言葉と食い違う
    ("灰流うらら", "PAC1-JP016SR", "レア", ""),  # レアの S＋R はスーパーのいつもの末尾 SR と同じ形で、どちらとも言えない
    ("青眼の白龍[顔右側]", "QCAC-JP021SSE1", "シークレット", ""),  # 番号つき＝絵が3種類以上のカードの何番目か
    ("灰流うらら", "QCAC-JP050SHR", "ホログラフィック", av.ART_ALT),
    ("灰流うらら", "QCAC-JP050PA", "ホログラフィック", ""),  # いつもの末尾でも S＋いつもの末尾でもない
    ("灰流うらら", "14558127", "ノーマル", ""),  # 商品番号が数字だけで読めない
    ("灰流うらら", "QCAC-JP050SE", "ノーマルパラレル", ""),  # 末尾の表に無いレアリティ
])
def test_classify_torecolo(name, pid, rarity, art):
    card = "ブラック・マジシャン" if "マジシャン" in name else ("青眼の白龍" if "青眼" in name else "灰流うらら")
    assert av.classify(_row("トレコロCB", name, rarity=rarity, url=TC.format(pid)), card)[0] == art


def test_green_ver_survives_scrape_time_normalization():
    # 各店の取得処理は取得した時点でレアリティを正準化する（scraper._parse_cardrush_items）。
    # GREEN Ver. の表記は正準化で消えず、別のレアリティの枠のまま art にも残ること
    rarity = normalize_rarity("クォーターセンチュリーシークレットGREEN Ver.")
    assert rarity == "クォーターセンチュリーシークレットGREEN Ver."
    assert av.classify(_row("カードラッシュ", "ブラックマジシャン", rarity=rarity), "ブラック・マジシャン")[0] == av.ART_GREEN
    # トレコロは名前側に書く
    assert av.classify(_row("トレコロCB", "ブラック・マジシャン[GREEN Ver]", rarity="25thシークレット",
                            url=TC.format("TD01-JP001QSE")), "ブラック・マジシャン")[0] == av.ART_GREEN


def test_edition_becomes_art():
    # 言語版は絵より先に、art を言語版の名前にする（日本版の元のイラストと混ぜない）
    assert av.classify(_row("カードラッシュ", "☆アジア☆灰流うらら(座り)"), "灰流うらら") == ("アジア版", "アジア版")
    assert av.classify(_row("カードラッシュ", "☆旧アジア☆青眼の白龍(1stEdition)"), "青眼の白龍")[1] == "アジア版"
    assert av.classify(_row("カードラッシュ", "☆英語版☆青眼の白龍"), "青眼の白龍")[1] == "英語版"
    assert av.classify(_row("カードラッシュ", "☆バンダイ☆青眼の白龍"), "青眼の白龍")[1] == ""


def test_build_done_annotates_without_renaming_or_mutating():
    import app as app_module

    rows = [
        {"shop": "遊々亭", "name": "灰流うらら(イラスト違い版)", "rarity": "シークレット", "price": 980, "sold_out": False, "url": ""},
        {"shop": "カードラッシュ", "name": "☆アジア☆灰流うらら(立ち)", "rarity": "シークレット", "price": 280, "sold_out": False, "url": ""},
        {"shop": "遊々亭", "name": "灰流うらら", "rarity": "シークレット", "price": 500, "sold_out": False, "url": ""},
    ]
    before = [dict(r) for r in rows]
    done = app_module._build_done(rows, "", "灰流うらら")
    got = [(r["rarity"], r["art"], r["edition"]) for r in done["results"]]
    assert got == [
        ("シークレット", av.ART_ALT, ""),
        ("シークレット", "アジア版", "アジア版"),  # レアリティ名は変えない（購入候補・買取・相場はレアリティ名で引く）
        ("シークレット", av.ART_BASE, ""),
    ]
    # 渡した行は書き換えない（同じ dict を価格の記録の裏スレッドにも渡している）
    assert rows == before
