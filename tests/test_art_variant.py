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
    ("青眼の白龍", "カードラッシュ", "青眼の白龍(カラー)", "", av.ART_MANGA_COLOR),  # カードラッシュは漫画風を (カラー)(モノクロ) とだけ書く
    ("青眼の白龍", "カードラッシュ", "青眼の白龍(モノクロ)", "", av.ART_MANGA_MONO),
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


def test_build_done_folds_cardrush_green_into_25th_for_display():
    # カードラッシュの GREEN Ver. は画面に送るときだけ 25thシークレット に寄せ、トレコロの [GREEN Ver] と同じ枠で比べる
    import app as app_module

    rows = [
        {"shop": "カードラッシュ", "name": "ブラックマジシャン", "rarity": "クォーターセンチュリーシークレットGREEN Ver.",
         "price": 19800, "sold_out": False, "url": ""},
        {"shop": "トレコロCB", "name": "ブラック・マジシャン[GREEN Ver]", "rarity": "25thシークレット",
         "price": 21800, "sold_out": False, "url": TC.format("TD01-JP001QSE")},
    ]
    done = app_module._build_done(rows, "", "ブラック・マジシャン")
    assert [(r["rarity"], r["art"]) for r in done["results"]] == [
        ("25thシークレット", av.ART_GREEN), ("25thシークレット", av.ART_GREEN)]
    # 保存に使う元の行は書き換えない
    assert rows[0]["rarity"] == "クォーターセンチュリーシークレットGREEN Ver."


def test_display_rarity_folds_asian_green_too():
    # アジア版の GREEN は art が「アジア版」になるが、レアリティは 25thシークレット に寄せる（日本版の GREEN と同じ枠に）
    row = {"rarity": "クォーターセンチュリーシークレットGREEN Ver.", "art": "アジア版"}
    assert av.display_rarity(row) == "25thシークレット"
    assert av.display_rarity({"rarity": "シークレット", "art": av.ART_BASE}) == "シークレット"


@pytest.mark.parametrize("name,abbr,art,shown", [
    # カーナベルは漫画風をレアリティ側に書き、色は商品名の (カラー版)(モノクロ版) だけ（2026-10-30 ベビードラゴン YAC1-JP075）。
    # 取得時にレアリティは「ウルトラ」などへ寄せ、漫画風の印は manga 欄に残す
    ("ベビードラゴン（カラー版）", "MANGAウル", av.ART_MANGA_COLOR, "ウルトラ"),
    ("ベビードラゴン(モノクロ版)", "MANGAウル", av.ART_MANGA_MONO, "ウルトラ"),
    ("ベビードラゴン（カラー版）", "MANGAシク", av.ART_MANGA_COLOR, "シークレット"),
    ("ベビードラゴン（モノクロ版）", "MANGAシク", av.ART_MANGA_MONO, "シークレット"),
    ("ベビードラゴン", "MANGAアル", "", "アルティメット"),  # 色が書いていない漫画風は判別しない
])
def test_kanabell_manga_rows(name, abbr, art, shown):
    rarity = normalize_rarity(abbr)
    assert rarity == shown
    assert av.classify({**_row("カーナベル", name, rarity=rarity), "manga": True}, "ベビードラゴン")[0] == art
    # 寄せる前に取った検索のキャッシュの行（レアリティが略号のまま・manga 欄なし）も同じ絵になる
    assert av.classify(_row("カーナベル", name, rarity=abbr), "ベビードラゴン")[0] == art


def test_kanabell_plain_color_word_without_manga_is_not_manga():
    # manga の印が無い行の「(カラー版)」は漫画風と決めつけない
    assert av.classify(_row("カーナベル", "ベビードラゴン（カラー版）", rarity="シークレット"), "ベビードラゴン")[0] == ""


def test_build_done_puts_kanabell_manga_beside_other_shops():
    # カーナベルの漫画風は、遊々亭の「シークレット」の中の漫画カラーと同じ枠で比べる（キャッシュに残る略号の行も）
    import app as app_module

    rows = [
        {"shop": "カーナベル", "name": "ベビードラゴン（カラー版）", "rarity": "シークレット", "manga": True,
         "price": 1180, "sold_out": False, "url": ""},
        {"shop": "カーナベル", "name": "ベビードラゴン（モノクロ版）", "rarity": "MANGAシク",
         "price": 580, "sold_out": False, "url": ""},
        {"shop": "遊々亭", "name": "ベビードラゴン(カラー/MANGA-STYLE)", "rarity": "シークレット",
         "price": 980, "sold_out": False, "url": ""},
    ]
    done = app_module._build_done(rows, "", "ベビードラゴン")
    assert [(r["rarity"], r["art"]) for r in done["results"]] == [
        ("シークレット", av.ART_MANGA_COLOR), ("シークレット", av.ART_MANGA_MONO), ("シークレット", av.ART_MANGA_COLOR)]


def _coded(shop, name, code, rarity="スーパー", url=""):
    return {"shop": shop, "name": name, "rarity": rarity, "code": code, "url": url}


def test_reprint_code_with_new_art_is_alt_for_every_shop():
    # 三戦の号 CH01-JP030 は QCAC-JP096 と違う絵の再録。カーナベルだけが (イラスト違い) と書き、ほかの店は無印（2026-10-29 本番の値）。
    # 無印の店の行も違う絵にそろえ、スーパー（CH01 にしか無い）に「元のイラスト」の候補を出さない
    rows = [
        _coded("カーナベル", "三戦の号(イラスト違い)", "CH01-JP030"),
        _coded("カーナベル", "三戦の号(イラスト違い)", "CH01-JP030", "シークレット"),
        _coded("遊々亭", "三戦の号", "CH01-JP030"),
        _coded("カードラッシュ", "三戦の号", "CH01-JP030"),
        _coded("トレコロCB", "三戦の号", "CH01-JP030", url=TC.format("CH01-JP030SR")),
        _coded("まんぞく屋", "三戦の号", "CH01-JP030"),
        _coded("遊々亭", "三戦の号", "QCAC-JP096", "シークレット"),
        _coded("カーナベル", "三戦の号", "QCAC-JP096", "シークレット"),
        {**_coded("カードラッシュ", "☆アジア☆三戦の号", "", "シークレット")},
    ]
    av.annotate(rows, "三戦の号")
    assert [r["art"] for r in rows] == [
        av.ART_ALT, av.ART_ALT, av.ART_ALT, av.ART_ALT, av.ART_ALT,
        "",  # 絵を書き分けない店は判別しないまま
        av.ART_BASE, av.ART_BASE,  # 別の型番の元の絵は変えない
        "アジア版",
    ]


def test_code_with_both_arts_keeps_plain_rows_as_base():
    # 灰流うらら PAC1-JP016 のように1つの型番に絵が2種類あるときは、同じ店に無印と (イラスト違い) が並ぶので無印は元のまま
    rows = [
        _coded("遊々亭", "灰流うらら", "PAC1-JP016", "シークレット"),
        _coded("遊々亭", "灰流うらら(イラスト違い版)", "PAC1-JP016", "シークレット"),
        _coded("カードラッシュ", "灰流うらら", "PAC1-JP016", "シークレット"),
    ]
    av.annotate(rows, "灰流うらら")
    assert [r["art"] for r in rows] == [av.ART_BASE, av.ART_ALT, av.ART_BASE]


def test_code_with_explicit_base_word_is_not_folded():
    # ある店が「既存イラスト」と書いた型番は、別の店が無印でも違う絵に寄せない
    rows = [
        _coded("カードラボ", "灰流うらら(新イラスト)", "PAC1-JP016", "シークレット"),
        _coded("遊々亭", "灰流うらら(既存イラスト)", "PAC1-JP016", "シークレット"),
        _coded("カードラッシュ", "灰流うらら", "PAC1-JP016", "シークレット"),
    ]
    av.annotate(rows, "灰流うらら")
    assert [r["art"] for r in rows] == [av.ART_ALT, av.ART_BASE, av.ART_BASE]


def _with_table(monkeypatch, table):
    monkeypatch.setattr(av, "_art_prints", lambda: {av._key(k): frozenset(v) for k, v in table.items()})


def test_table_folds_code_without_carnaval(monkeypatch):
    # カーナベルが取れない日（ほかの店は無印だけ）でも、Yugipedia の表にある型番は違う絵にそろえる
    _with_table(monkeypatch, {"三戦の号": ["CH01-JP030"]})
    rows = [
        _coded("遊々亭", "三戦の号", "CH01-JP030"),
        _coded("トレコロCB", "三戦の号", "CH01-JP030", url=TC.format("CH01-JP030SR")),
        _coded("まんぞく屋", "三戦の号", "CH01-JP030"),
        _coded("遊々亭", "三戦の号", "QCAC-JP096", "シークレット"),
    ]
    av.annotate(rows, "三戦の号")
    assert [r["art"] for r in rows] == [av.ART_ALT, av.ART_ALT, "", av.ART_BASE]


def test_table_yields_to_shop_with_both_arts(monkeypatch):
    # 表にあっても、同じ店に元と違うが並ぶ型番（型番の中に絵が2つ）は無印を元のままにする
    _with_table(monkeypatch, {"灰流うらら": ["PAC1-JP016"]})
    rows = [
        _coded("遊々亭", "灰流うらら", "PAC1-JP016", "シークレット"),
        _coded("遊々亭", "灰流うらら(イラスト違い版)", "PAC1-JP016", "シークレット"),
    ]
    av.annotate(rows, "灰流うらら")
    assert [r["art"] for r in rows] == [av.ART_BASE, av.ART_ALT]


def test_missing_table_falls_back_to_shop_words(monkeypatch, tmp_path):
    # 表が無いときは警告を出し、店の手がかりだけで判定する
    monkeypatch.setattr(av, "_ART_PRINTS_PATH", tmp_path / "none.json")
    av._art_prints.cache_clear()
    try:
        assert av._art_prints() == {}
    finally:
        av._art_prints.cache_clear()


def test_table_code_folds_unreadable_label(monkeypatch):
    # 表にある型番（絵が1種類）は、店の言葉で判別できない行も違う絵にする（カーナベルの「(パワプロコラボ)」。2026-10-29 本番の値）。
    # 絵を書き分けない店・漫画風（色が書いていない）・言語版・表に無い型番は変えない
    _with_table(monkeypatch, {"増援": ["PPC2-JP001"]})
    rows = [
        _coded("カーナベル", "増援(パワプロコラボ)", "PPC2-JP001", "ノーマル"),
        _coded("カーナベル", "増援(PPC2-JP001)(パワプロコラボ)", "PPC2-JP001", "ノーマル"),
        _coded("遊々亭", "増援", "PPC2-JP001", "ノーマル"),
        _coded("まんぞく屋", "増援", "PPC2-JP001", "ノーマル"),
        _coded("遊々亭", "増援(MANGA-STYLE)", "PPC2-JP001", "ノーマル"),
        _coded("カードラッシュ", "☆アジア☆増援", "PPC2-JP001", "ノーマル"),
        _coded("カーナベル", "増援(パワプロコラボ)", "SD99-JP001", "ノーマル"),
    ]
    av.annotate(rows, "増援")
    assert [r["art"] for r in rows] == [av.ART_ALT, av.ART_ALT, av.ART_ALT, "", "", "アジア版", ""]


def test_unreadable_label_is_kept_for_shop_inferred_code(monkeypatch):
    # 表に無く、カーナベルの印だけで決めた型番では、判別できない行はそのまま（手がかりが弱い）
    _with_table(monkeypatch, {})
    rows = [
        _coded("カーナベル", "三戦の号(イラスト違い)", "CH01-JP030"),
        _coded("カードラボ", "三戦の号(なにか)", "CH01-JP030"),
    ]
    av.annotate(rows, "三戦の号")
    assert [r["art"] for r in rows] == [av.ART_ALT, ""]
