"""
tests/test_search_query.py — 店舗別の検索語生成の回帰テスト

背景:
  店舗にあるはずのカードが検索で0件になる不具合を2026-08-02に調査した結果、
  「TCGYM側が検索語を半角へ正規化した結果、店舗側の実表記と食い違って外れる」
  ことが原因と判明した。店舗ごとに正しい検索語の形が異なるため、実測で確定した
  仕様をここで固定する。

  - トレコロCB: 商品名は全角登録が主流。検索エンジンは商品名側の全角/半角は
    吸収するが、検索語側は区別する → 検索語は全角へ寄せる
  - カーナベル: ESの card_name に半角格納と全角格納が混在する
    → wildcard を半角版・全角版の両方投げる

テスト方針:
  - ネットワーク・認証情報は不要。純粋な文字列生成のみを検証する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import (
    to_fullwidth_alnum,
    _squeeze_spaces,
    _torecolo_search_query,
    _cardrush_search_query,
    _normalize_search_query,
    _kanabell_wildcard_value,
    _kanabell_wildcard_values,
)


class TestNormalizeSearchQuery:
    """共通検索語（遊々亭・カードラボの販売/買取、まんぞく屋販売の5経路が共有）

    2026-08-03: 空白圧縮とコロン除去を追加した。いずれも影響カードを全経路で
    旧新比較し、減少ゼロを確認した上での変更（詳細は scraper.py の docstring）。
    """

    def test_trailing_space_is_stripped(self):
        # 本丸: 記号を落とした結果の末尾スペースで0件になっていた
        # （'聖なるバリア ミラーフォース ' は0件、圧縮後はカードラボ販売で4件）
        assert _normalize_search_query("聖なるバリア －ミラーフォース－") == "聖なるバリア ミラーフォース"

    def test_consecutive_separators_collapse_to_one_space(self):
        # 「区切り＋ダッシュ」が連続すると空白が二重になる
        assert _normalize_search_query("カオス・ソルジャー －開闢の使者－") == "カオス ソルジャー 開闢の使者"
        assert _normalize_search_query("King of Destruction - Xexex") == "King of Destruction Xexex"

    def test_colon_becomes_space(self):
        # 本丸: コロンを残すと遊々亭・カードラッシュの買取が0件になる
        assert _normalize_search_query("I：Pマスカレーナ") == "I Pマスカレーナ"
        assert _normalize_search_query("S:Pリトルナイト") == "S Pリトルナイト"

    def test_no_double_or_edge_spaces_for_any_shape(self):
        for name in ("めぐり－Ai－", "アルトメギア・メセナ－覚醒－", "Re：EX", "冀望郷－バリアン－"):
            q = _normalize_search_query(name)
            assert "  " not in q
            assert q == q.strip()

    def test_plain_name_is_unchanged(self):
        # 記号を含まないカード名は従来どおり素通し（退行検知）
        for name in ("灰流うらら", "増殖するG", "ブラック・マジシャン"):
            assert _normalize_search_query(name) == name.replace("・", " ")


class TestSqueezeSpaces:
    """検索語生成の共通後処理"""

    def test_collapses_and_strips(self):
        assert _squeeze_spaces("  a   b  ") == "a b"

    def test_idempotent(self):
        assert _squeeze_spaces(_squeeze_spaces(" a  b ")) == _squeeze_spaces(" a  b ")

    def test_empty(self):
        assert _squeeze_spaces("   ") == ""


class TestCardrushSearchQuery:
    """カードラッシュ検索語（販売・買取で共通）

    カードラッシュは商品名を記号なしで登録しており、検索語に記号が残るとヒットしない。
    """

    def test_kagi_kakko_is_removed(self):
        # '炎舞 「天枢」' では0件、'炎舞 天枢' なら2件（2026-08-03 実測）
        assert _cardrush_search_query("炎舞－「天枢」") == "炎舞 天枢"

    def test_star_and_quotes_are_removed(self):
        assert _cardrush_search_query("Live☆Twin リィラ") == "Live Twin リィラ"
        assert _cardrush_search_query("セリオンズ“キング”レギュラス") == "セリオンズ キング レギュラス"

    def test_ampersand_is_removed(self):
        assert _cardrush_search_query("ドロール＆ロックバード") == "ドロール ロックバード"

    def test_no_double_spaces_or_edge_spaces(self):
        q = _cardrush_search_query("星遺物－『星杯』")
        assert "  " not in q
        assert q == q.strip()

    def test_greek_letters_are_kept(self):
        # 識別子は検索語からも落とさない
        assert "α" in _cardrush_search_query("磁石の戦士α")
        assert "γ" in _cardrush_search_query("PSYフレームギア・γ")

    def test_plain_name_matches_shared_normalizer(self):
        # 記号を含まないカード名は共通の正規化と同じ結果になること（退行検知）
        for name in ("灰流うらら", "増殖するG", "ブラック・マジシャン"):
            assert _cardrush_search_query(name) == _normalize_search_query(name)

    def test_colon_is_removed_via_shared_normalizer(self):
        # 買取はコロンを残すと0件になる（'I：Pマスカレーナ' 0→17件）。
        # 除去は共通の _normalize_search_query 側で行うため、ここでは結果だけ固定する
        assert _cardrush_search_query("I：Pマスカレーナ") == "I Pマスカレーナ"
        assert ":" not in _cardrush_search_query("W：Pファンシーボール")


class TestToFullwidthAlnum:
    """英数字のみの全角化（カーナベルES用）"""

    def test_alnum(self):
        assert to_fullwidth_alnum("DHERO") == "ＤＨＥＲＯ"

    def test_symbols_are_not_converted(self):
        # 本丸: 記号まで全角化すると、ピリオドだけ半角の混在表記レコードに当たらなくなる
        # （カーナベル実データ「Ｎｏ.１０４ 仮面魔踏士シャイニングＶ」）
        assert to_fullwidth_alnum("No.104 仮面魔踏士シャイニングV") == "Ｎｏ.１０４ 仮面魔踏士シャイニングＶ"
        assert to_fullwidth_alnum("W:Pファンシーボール") == "Ｗ:Ｐファンシーボール"

    def test_hyphen_is_not_converted(self):
        assert to_fullwidth_alnum("D-HERO") == "Ｄ-ＨＥＲＯ"

    def test_wildcard_metachars_are_not_converted(self):
        # 半角版と全角版で wildcard の意味を揃えるため、メタ文字は変換しない
        assert to_fullwidth_alnum("*abc?*") == "*ａｂｃ?*"

    def test_space_is_kept(self):
        assert to_fullwidth_alnum("VS ラゼン") == "ＶＳ ラゼン"

    def test_japanese_unchanged(self):
        assert to_fullwidth_alnum("灰流うらら") == "灰流うらら"


class TestTorecoloSearchQuery:
    """トレコロ検索語（販売・買取で共通）

    トレコロの検索がヒットする条件は「ダッシュが U+FF0D であること」のみ。
    以下は 2026-08-03 に実サイトで条件を振って確認した事実を固定する。
    """

    def test_halfwidth_hyphen_becomes_fullwidth(self):
        # 本丸: 半角ハイフン(U+002D)のままだと店舗検索が0件になる
        q = _torecolo_search_query("D-HERO デスドグマガイ")
        assert q == "D－HERO デスドグマガイ"
        assert "－" in q
        assert "-" not in q

    def test_all_dash_variants_are_unified(self):
        # U+2010/U+2011/U+2012/U+2013/U+2014/U+2015/U+2212 も実測で0件だった
        for dash in "-‐‑‒–—―−":
            q = _torecolo_search_query(f"D{dash}HERO")
            assert q == "D－HERO", f"{dash!r} が U+FF0D に統一されていない"

    def test_chouon_is_not_converted(self):
        # 長音「ー」(U+30FC)はカード名の一部。ダッシュとして潰すと別語になる
        assert _torecolo_search_query("BF－そよ風のブリーズ") == "BF－そよ風のブリーズ"
        assert "ブリーズ" in _torecolo_search_query("BF－そよ風のブリーズ")

    def test_alnum_width_is_untouched(self):
        # 英数字の幅は結果に影響しないので変換しない（全角化は不要な副作用）
        assert _torecolo_search_query("No.39 希望皇ホープ") == "No.39 希望皇ホープ"
        assert _torecolo_search_query("超重神童ワカ－U４") == "超重神童ワカ－U４"

    def test_roman_numerals_are_kept(self):
        # NFKCを通すとⅩ→Xに潰れ、トレコロ側の登録表記と食い違って取りこぼす
        assert _torecolo_search_query("アルカナフォースⅩⅩⅠ－ＴＨＥ ＷＯＲＬＤ").startswith("アルカナフォースⅩⅩⅠ－")

    def test_nakaguro_is_preserved(self):
        # 中黒はトレコロでは残さないとヒットしない（除去も置換もしない）
        assert "・" in _torecolo_search_query("ブラック・マジシャン")

    def test_ideographic_space_becomes_halfwidth(self):
        assert _torecolo_search_query("VS　ラゼン") == "VS ラゼン"


class TestKanabellWildcardValues:
    """カーナベル wildcard（半角版＋全角版）"""

    def test_halfwidth_and_fullwidth_are_both_returned(self):
        vals = _kanabell_wildcard_values("D-HERO デスドグマガイ")
        assert "*D*HERO*デスドグマガイ*" in vals
        assert "*Ｄ*ＨＥＲＯ*デスドグマガイ*" in vals
        assert len(vals) == 2

    def test_mixed_width_record_is_covered(self):
        # カーナベル実データ「Ｎｏ.１０４ 仮面魔踏士シャイニングＶ」（ピリオドだけ半角）に
        # 一致する値が含まれること。記号まで全角化すると当たらなくなる
        vals = _kanabell_wildcard_values("No.104 仮面魔踏士シャイニングV")
        assert "*Ｎｏ.１０４*仮面魔踏士シャイニングＶ*" in vals
        for v in vals:
            assert "．" not in v, "ピリオドを全角化すると混在表記レコードに当たらない"

    def test_japanese_only_returns_single_value(self):
        # 英数字を含まないカード名は半角版と全角版が同じなので1つだけ返す
        vals = _kanabell_wildcard_values("灰流うらら")
        assert vals == ["*灰流うらら*"]

    def test_single_value_matches_legacy_behaviour(self):
        # 従来の挙動（半角版）は必ず含まれ、既存のヒットが減らないこと
        for name in ("D-HERO ディアボリックガイ", "灰流うらら", "閃刀姫-レイ"):
            assert _kanabell_wildcard_value(name) in _kanabell_wildcard_values(name)

    def test_fullwidth_value_has_no_fullwidth_asterisk(self):
        # 区切りの '*' まで全角化してしまうとクエリが壊れる
        vals = _kanabell_wildcard_values("W:Pファンシーボール")
        for v in vals:
            assert "＊" not in v
            assert v.startswith("*") and v.endswith("*")
