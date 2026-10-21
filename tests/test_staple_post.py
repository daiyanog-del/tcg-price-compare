"""汎用カードのリストと値動き投稿（staple_cards.py / staple_post.py）の純ロジック。"""
import os

import pytest

import staple_cards
import staple_post


def _row(name, rarity, prev, today):
    return {"card_name": name, "rarity": rarity, "prev_price": prev, "today_price": today,
            "date_old": "2026-09-25", "date_new": "2026-09-26"}


def test_select_staples_counts_distinct_themes_and_drops_theme_cards():
    recipes = [
        {"theme": "DDD", "cards": [{"name": "霆王の閃光", "is_ex": False}, {"name": "DDD怒涛壊薙王カエサル・ラグナロク", "is_ex": True}]},
        {"theme": "巳剣", "cards": [{"name": "霆王の閃光", "is_ex": False}, {"name": "霆王の閃光", "is_ex": False}]},
        {"theme": "閃刀姫", "cards": [{"name": "霆王の閃光", "is_ex": False}, {"name": "閃刀姫－レイ", "is_ex": False}]},
        {"theme": "閃刀姫", "cards": [{"name": "閃刀姫－レイ", "is_ex": False}]},
        # テーマ名の揺れで別テーマ扱いのレシピに入ったテーマ専用カード
        {"theme": "異解△", "cards": [{"name": "閃刀姫－レイ", "is_ex": False}]},
        {"theme": "レイズ・ムーン", "cards": [{"name": "閃刀姫－レイ", "is_ex": False}]},
    ]
    rows = staple_cards.select_staples(recipes, min_themes=3)
    assert [r["card_name"] for r in rows] == ["霆王の閃光"]
    assert rows[0]["themes"] == 3 and rows[0]["decks"] == 3  # 同じレシピ内の重複は1件


def test_select_staple_movers_thresholds_are_asymmetric():
    staples = {"聖王の粉砕", "ドロール＆ロックバード", "灰流うらら", "増殖するG"}
    rows = [
        _row("聖王の粉砕", "シークレット", 660, 280),        # -380 / -57.6% → 値下がり
        _row("灰流うらら", "ノーマル", 220, 320),            # +100 / +45% → 値上がり基準(+200)に届かない
        _row("ドロール＆ロックバード", "シークレット", 480, 680),  # +200 / +41.7% → 値上がり
        _row("増殖するG", "シークレット", 680, 590),          # -90 → 値下がり基準(-100)に届かない
        _row("天下独歩の大義賊", "シークレット", 9000, 5000),  # 汎用リスト外
    ]
    got = staple_post.select_staple_movers(rows, staples)
    assert [(m["name"], m["direction"]) for m in got] == [
        ("聖王の粉砕", "down"), ("ドロール＆ロックバード", "up")]


def test_select_staple_movers_boundaries_and_fuzzy_name():
    staples = {"ドロール&ロックバード"}  # 半角＆でも照合できる
    rows = [_row("ドロール＆ロックバード", "ウルトラ", 1000, 1200),   # +200 / +20.0% ちょうど
            _row("ドロール＆ロックバード", "スーパー", 1000, 850)]    # -150 / -15.0% ちょうど
    got = {(m["rarity"], m["direction"]) for m in staple_post.select_staple_movers(rows, staples)}
    assert got == {("ウルトラ", "up"), ("スーパー", "down")}


def test_format_post_text():
    m = {"name": "聖王の粉砕", "rarity": "シークレット", "today": 280, "yesterday": 660,
         "diff": -380, "pct": -57.6, "direction": "down", "date_old": "2026-09-25", "date_new": "2026-09-26"}
    lines = staple_post.format_post_text(m).split("\n")
    assert lines[0] == "【値下がり】聖王の粉砕（シークレット）"
    assert lines[1] == "¥280（9/25 ¥660から -380円）"
    assert lines[2].endswith("/card/%E8%81%96%E7%8E%8B%E3%81%AE%E7%B2%89%E7%A0%95")


def test_align_history_uses_judged_prices_for_last_two_days():
    m = {"today": 280, "yesterday": 660, "date_old": "2026-09-25", "date_new": "2026-09-26"}
    hist = [("2026-09-24", 680), ("2026-09-25", 640), ("2026-09-26", 250)]
    assert staple_post.align_history(hist, m) == [
        ("2026-09-24", 680), ("2026-09-25", 660), ("2026-09-26", 280)]


@pytest.mark.skipif(not os.environ.get("STAPLE_FONT_DIR"), reason="STAPLE_FONT_DIR 未設定（フォントが無い）")
def test_render_post_image(tmp_path):
    from PIL import Image
    card = tmp_path / "card.png"
    Image.new("RGB", (256, 372), (120, 40, 90)).save(card)
    m = {"name": "カオス・アンヘル－混沌の双翼－", "rarity": "レア", "today": 780, "yesterday": 1000,
         "diff": -220, "pct": -22.0, "direction": "down", "date_old": "2026-10-19", "date_new": "2026-10-20"}
    hist = [("2026-07-22", 1500), ("2026-08-30", 1000), ("2026-10-19", 1000), ("2026-10-20", 780)]
    out = staple_post.render_post_image(str(card), m, hist, str(tmp_path / "out.png"))
    assert Image.open(out).size == (1200, 675)


@pytest.mark.skipif(not os.environ.get("STAPLE_FONT_DIR"), reason="STAPLE_FONT_DIR 未設定（フォントが無い）")
def test_name_font_falls_back_when_glyph_missing():
    pytest.importorskip("fontTools")
    jp, _num, fallback = staple_post._font_paths()
    assert staple_post.name_font_path("カオス・アンヘル－混沌の双翼－", jp, fallback) == jp
    # M PLUS 2 に無い字だけを予備へ回す（どちらにも無い字はそのまま M PLUS 2）
    missing = [c for c in "𠮟鷗髙﨑" if staple_post._missing_chars(jp, c)]
    for c in missing:
        if fallback and not staple_post._missing_chars(fallback, c):
            assert staple_post.name_font_path("名" + c, jp, fallback) == fallback
