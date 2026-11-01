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
    # 2026-10-30 に画像で確かめて足した言葉
    ("エフェクト・ヴェーラー", "カードラッシュ", "エフェクトヴェーラー(オーブ無)", "", av.ART_BASE),
    ("エフェクト・ヴェーラー", "カードラッシュ", "エフェクトヴェーラー(オーブ有)", "", av.ART_ALT),
    ("死者蘇生", "カードラッシュ", "死者蘇生(日本版)", "", av.ART_BASE),
    ("死者蘇生", "カードラッシュ", "死者蘇生(海外版)", "", av.ART_ALT),
    ("死者蘇生", "カードラッシュ", "死者蘇生(浮世絵/ロゴ)", "", av.ART_ALT),
    ("青眼の究極竜", "カードラッシュ", "青眼の究極竜(ロゴ)", "", av.ART_BASE),  # 印・刷りの違いはどのカードでも元
    ("大嵐", "カードラッシュ", "大嵐(復刻版)", "", av.ART_BASE),
    ("エフェクト・ヴェーラー", "カードラッシュ", "(封筒付き未開封)エフェクトヴェーラー(YCSJロゴ)", "", av.ART_BASE),
    ("灰流うらら", "遊々亭", "灰流うらら(ロゴ)", "", ""),  # カードラッシュ以外の店には当てない
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
    # ブラック・マジシャンは絵の台帳のカードなので、GREEN Ver. は台帳の「東京ドーム」（TD01）にまとまる（2026-11-01）
    assert [(r["rarity"], r["art"]) for r in done["results"]] == [
        ("25thシークレット", "東京ドーム"), ("25thシークレット", "東京ドーム")]
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
    av._art_table.cache_clear()
    try:
        assert av._art_prints() == {}
    finally:
        av._art_table.cache_clear()


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


# ── 絵の台帳（data/art_ledger.json・2026-11-01）── 商品名・リンクは 2026-11-01 に本番の検索で取れた値

def test_ledger_names_art_by_code():
    # 型番の中に絵が1つの版は、店の書き方に関係なく型番で絵が決まる。QCDB の 25thシークは「元のイラスト」ではなく劇場版の絵
    rows = [
        _coded("遊々亭", "ブラック・マジシャン", "QCDB-JP007", "25thシークレット"),
        _coded("カーナベル", "ブラック・マジシャン", "QCCU-JP001", "25thシークレット"),
        _coded("まんぞく屋", "ブラック・マジシャン", "QCDB-JP007", "25thシークレット"),  # 絵を書き分けない店も型番で決まる
    ]
    av.annotate(rows, "ブラック・マジシャン")
    assert [(r["art"], r["art_no"]) for r in rows] == [("劇場版（DSOD）", 7), ("オリジナル", 1), ("劇場版（DSOD）", 7)]


@pytest.mark.parametrize("shop,name,url,art", [
    # 1つの型番に絵が7種類ある QCAC-JP018 は、店ごとの言葉で決める
    ("カーナベル", "ブラック・マジシャン(パンドラ)", "", "パンドラ"),
    ("カードラボ", "ブラック・マジシャン(イラスト違い(5)/SJMP版)", "", "緑"),
    ("カードラッシュ", "ブラックマジシャン(7右向杖構え/虹背景)", "", "宇宙"),
    ("トレコロCB", "ブラック・マジシャン[目隠れ]", TC.format("QCAC-JP018SQSE4"), "原作絵"),
    ("遊々亭", "ブラック・マジシャン", "", "石板"),  # 無印＝その型番のふつうの絵
    ("遊々亭", "ブラック・マジシャン(イラスト違い版)", "", ""),  # 違う絵が6種類あるので決まらない
])
def test_ledger_words_split_multi_art_code(shop, name, url, art):
    rows = [_coded(shop, name, "QCAC-JP018", "25thシークレット", url)]
    av.annotate(rows, "ブラック・マジシャン")
    assert rows[0]["art"] == art


def test_ledger_reads_torecolo_code_from_link():
    # トレコロは検索の結果に型番が入らないことが多い。商品番号（型番＋レアリティの略号）の頭で台帳を引く
    rows = [_coded("トレコロCB", "真紅眼の黒竜[口上向き]", "", "シークレット", TC.format("QCAC-JP022SSE1")),
            _coded("トレコロCB", "青眼の白龍", "", "25thシークレット", TC.format("QCDB-JP009QSE"))]
    av.annotate(rows[:1], "真紅眼の黒竜")
    av.annotate(rows[1:], "青眼の白龍")
    assert [r["art"] for r in rows] == ["赤", "劇場版（DSOD）"]


def test_ledger_code_prefix_needs_boundary():
    # 商品番号の頭が型番と一致しても、続きが数字なら別の型番（LB-05 と LB-050 のような取り違えをしない）
    led = {"codes": {"LB-05": (1, [])}}
    assert av._ledger_code({"shop": "トレコロCB", "url": TC.format("LB-050UR")}, led) == ""
    assert av._ledger_code({"shop": "トレコロCB", "url": TC.format("LB-05UR")}, led) == "LB-05"


def test_ledger_codeless_rows_use_star_words_only():
    # 型番の無い古い版は「*」の言葉だけで決める。言葉も無ければ判別できない（元のイラストにしない）
    rows = [_coded("カーナベル", "ブラック・マジシャン(初期イラスト版)", "", "ウルトラ"),
            _coded("カーナベル", "ブラック・マジシャン", "", "ウルトラ")]
    av.annotate(rows, "ブラック・マジシャン")
    assert [r["art"] for r in rows] == ["オリジナル", ""]


def test_ledger_keeps_editions_and_manga_and_folds_green():
    rows = [_coded("カードラッシュ", "☆アジア☆青眼の白龍(2右向)", "", "シークレット"),
            _coded("カードラッシュ", "青眼の白龍(カラー)", "", "シークレット"),
            _coded("カーナベル", "青眼の白龍(TOKYO DOME GREEN Ver.)(未開封)", "TD02-JP001", "25thシークレット"),
            _coded("遊々亭", "青眼の白龍", "TD02-JP001", "25thシークレット")]
    av.annotate(rows, "青眼の白龍")
    assert [r["art"] for r in rows] == ["アジア版", av.ART_MANGA_COLOR, "東京ドーム", "東京ドーム"]


def test_cards_outside_ledger_keep_two_way_split():
    rows = [_coded("遊々亭", "灰流うらら(イラスト違い版)", "PAC1-JP016", "シークレット")]
    av.annotate(rows, "灰流うらら")
    assert rows[0]["art"] == av.ART_ALT and "art_no" not in rows[0]


def test_missing_ledger_falls_back_to_two_way(monkeypatch, tmp_path):
    monkeypatch.setattr(av, "_ART_LEDGER_PATH", tmp_path / "none.json")
    av._art_ledger.cache_clear()
    try:
        rows = [_coded("遊々亭", "ブラック・マジシャン", "QCDB-JP007", "25thシークレット")]
        av.annotate(rows, "ブラック・マジシャン")
        assert rows[0]["art"] == av.ART_BASE
    finally:
        av._art_ledger.cache_clear()


def test_ledger_file_is_consistent():
    # 台帳の型番・言葉・色の番号は、どれもその台帳の絵の番号の中にある（名前の付け忘れ・番号の打ち間違いを止める）
    import json
    raw = json.loads(av._ART_LEDGER_PATH.read_text(encoding="utf-8"))
    for name, v in raw.items():
        if name.startswith("_"):
            continue
        nums = {int(n) for n in v["arts"]}
        assert int(v["default"]) in nums, name
        for code, e in v["codes"].items():
            assert {n for n in [e.get("base"), *e.get("alts", [])] if n is not None} <= nums, (name, code)
        for code, ws in v.get("words", {}).items():
            allowed = nums if code == "*" else {n for n in [v["codes"][code].get("base"), *v["codes"][code].get("alts", [])] if n is not None}
            assert {w["art"] for w in ws} <= allowed, (name, code)
        assert set(v.get("colors", {}).values()) <= nums, name
    assert av.ledger_info("ブラック・マジシャン") == {"order": [raw["ブラック・マジシャン"]["arts"][n] for n in sorted(raw["ブラック・マジシャン"]["arts"], key=int)], "default": "石板"}


def test_ledger_rows_record_by_number():
    # 台帳のカードは記録の表に番号で書く（名前を直しても記録の系列が切れない）。決まらない行・言語版は今までどおり
    rows = [_coded("遊々亭", "ブラック・マジシャン", "QCDB-JP007", "25thシークレット"),
            _coded("遊々亭", "ブラック・マジシャン(イラスト違い版)", "QCAC-JP018", "25thシークレット"),
            _coded("カードラッシュ", "☆アジア☆ブラックマジシャン", "", "25thシークレット")]
    av.annotate(rows, "ブラック・マジシャン")
    assert [av.record_art(r, "ブラック・マジシャン") for r in rows] == ["絵#7", av.ART_UNKNOWN, "アジア版"]
    assert av.needs_art_rows({"絵#7", "絵#4", av.ART_UNKNOWN})


def test_ledger_reads_torecolo_20th_secret_code():
    # 20thシークレットの商品番号は末尾が 20SE（数字から始まる）。型番の後ろが略号の形なら引ける（reviewer 指摘）
    led = {"codes": {"20TH-JPBS1": (8, []), "2018-JPP01": (1, [])}}
    assert av._ledger_code({"shop": "トレコロCB", "url": TC.format("20TH-JPBS120SE")}, led) == "20TH-JPBS1"
    assert av._ledger_code({"shop": "トレコロCB", "url": TC.format("2018-JPP0120SE")}, led) == "2018-JPP01"


def test_ledger_unknown_code_is_unknown_and_warned(caplog, monkeypatch):
    av._warned_codes.clear()
    sent = []
    monkeypatch.setattr(av, "_notify_unknown_code", lambda card, code, row: sent.append((card, code, row["shop"])))
    rows = [_coded("カーナベル", "ブラック・マジシャン", "ZZZZ-JP001", "ウルトラ"),
            _coded("遊々亭", "ブラック・マジシャン", "ZZZZ-JP001", "ウルトラ")]
    with caplog.at_level("WARNING"):
        av.annotate(rows, "ブラック・マジシャン")
    assert [r["art"] for r in rows] == ["", ""]
    assert sum("ZZZZ-JP001" in m for m in caplog.messages) == 1  # 型番ごとに1回だけ
    assert sent == [("ブラック・マジシャン", "ZZZZ-JP001", "カーナベル")]  # Discord にも型番ごとに1回だけ


def test_ledger_single_art_rarity_is_not_split():
    # 台帳のカードでも、判別できた絵が1種類のレアリティは別表に書かない（本表の値と同じ）
    assert not av.needs_art_rows({"絵#1"})
    assert not av.needs_art_rows({"絵#1", av.ART_UNKNOWN})
    assert av.needs_art_rows({"絵#1", "絵#4"})


def test_ledger_manga_rows_get_number():
    rows = [_coded("カードラッシュ", "青眼の白龍(カラー)", "", "シークレット")]
    av.annotate(rows, "青眼の白龍")
    assert rows[0]["art"] == av.ART_MANGA_COLOR and av.record_art(rows[0], "青眼の白龍") == "絵#19"


def test_unknown_code_notice_goes_to_discord_without_blocking(monkeypatch):
    # 知らせは裏の糸で送り、メンションを飛ばさない。送信が失敗しても例外を出さない
    import threading
    import discord_notify
    got, done = [], threading.Event()

    def fake(message, *, no_mentions=False):
        got.append((message, no_mentions)); done.set()
        raise RuntimeError("送信失敗")
    monkeypatch.setattr(discord_notify, "send_discord_message", fake)
    av._notify_unknown_code("青眼の白龍", "ZZZZ-JP002", {"shop": "遊々亭", "name": "青眼の白龍", "rarity": "ウルトラ"})
    assert done.wait(5)
    assert got[0][1] is True and "ZZZZ-JP002" in got[0][0] and "data/art_ledger.json" in got[0][0]


def test_unknown_pack_abbreviation_is_not_notified(monkeypatch):
    # カーナベルの弾の略号（型番の形でない）は台帳に無くても知らせない（毎晩の雑音になる）
    av._warned_codes.clear()
    sent = []
    monkeypatch.setattr(av, "_notify_unknown_code", lambda card, code, row: sent.append(code))
    rows = [_coded("カーナベル", "ブラック・マジシャン", "TTP1", "レア")]
    av.annotate(rows, "ブラック・マジシャン")
    assert rows[0]["art"] == "" and sent == []
