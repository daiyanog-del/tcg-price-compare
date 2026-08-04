"""
tests/test_is_target_card.py — is_target_card の名前一致境界判定の回帰テスト

背景:
  共有関数 is_target_card は「検索したカード名」と「店舗の商品名」が同一カードを
  指すかを判定する。全5店(遊々亭/カードラボ/カードラッシュ/トレコロ/カーナベルES)が
  この1関数を共有するため、ここの退行は全店に波及する。

  以前、前後境界判定が「日本語の続き」だけを別カード名の延長とみなしていたため、
  英字接頭辞/接尾辞の付いた別カードを基底名の検索結果に取り込む過剰一致があった
  （例: 「青眼の白龍」検索に「Sin 青眼の白龍」が一致）。これを抑制した修正の回帰を防ぐ。

  2026-08-04: 前方/後方の境界チェックが区切り集合 `_FLEX_SEP_CHARS` と同期していない
  既存バグを修正（末尾判定の6文字ハードコード廃止・厳密括弧 carve-out から 「」『』 を除外）。

テスト方針:
  - ネットワーク・認証情報は不要。is_target_card を直接呼ぶだけ。
  - ケースは実データ（2026-08-03 全5店992ペア／2026-08-04 全9経路8,311ペア）で
    実際に観測された商品名から採用。
"""

import json
import re
import sys
from pathlib import Path

# テスト対象モジュール（リポジトリルートを import パスに追加）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scraper import (
    is_target_card, _is_exactly_bracketed, _has_name_text_outside_brackets,
    _ANNOTATION_BRACKETS, _BOUNDARY_SKIP_CHARS, _FLEX_SEP_ALL, _FLEX_SEP_CHARS,
    _FLEX_SPLIT, normalize_width,
)


# ──────────────────────────────────────────────
# 過剰一致の抑制: 英字接辞付きの別カードは除外されるべき（今回の修正対象）
# ──────────────────────────────────────────────

class TestOverMatchSuppressed:
    """英字の接頭辞/接尾辞を持つ別カードが基底名検索に混ざらないこと"""

    def test_prefix_sin_with_space(self):
        # 「Sin 青眼の白龍」(=Sin Blue-Eyes) は別カード
        assert is_target_card("青眼の白龍", "Sin 青眼の白龍") is False

    def test_prefix_sin_no_space(self):
        assert is_target_card("青眼の白龍", "Sin青眼の白龍") is False

    def test_prefix_sin_fullwidth(self):
        # 全角「Ｓｉｎ」も normalize_width で半角化されて除外される
        assert is_target_card("青眼の白龍", "Ｓｉｎ青眼の白龍") is False
        assert is_target_card("青眼の白龍", "Ｓｉｎ  青眼の白龍") is False

    def test_prefix_s_and_suffix_one(self):
        # 「SNo.39 希望皇ホープONE」(=Number S39) は接頭辞S・接尾辞ONEの別カード
        assert is_target_card("No.39 希望皇ホープ", "SNo.39 希望皇ホープONE") is False
        assert is_target_card("No.39 希望皇ホープ", "SNo.39希望皇ホープONE") is False

    def test_kanabell_dash_variant(self):
        # カーナベルES格納形（長音→ダッシュ正規化後）でも除外されること
        assert is_target_card("No.39 希望皇ホ-プ", "SNo.39 希望皇ホ-プONE") is False


# ──────────────────────────────────────────────
# 正当な同一カードは維持されるべき（締めすぎ＝取りこぼし防止）
# ──────────────────────────────────────────────

class TestLegitimateKept:
    """同一カードの正当な表記は採用され続けること"""

    def test_exact_name(self):
        assert is_target_card("青眼の白龍", "青眼の白龍") is True

    def test_code_suffix_in_paren(self):
        assert is_target_card("青眼の白龍", "青眼の白龍 (25TH-JP001)") is True

    def test_rarity_suffix_in_bracket(self):
        assert is_target_card("青眼の白龍", "青眼の白龍【ウルトラ】") is True

    def test_english_name_with_japanese_in_paren(self):
        # 「BLUE EYES WHITE DRAGON(青眼の白龍)」= 基底名がぴったり括弧で包まれた併記注記。
        # 厳密括弧 carve-out により前方チェックを免除し、同一カードとして維持する。
        assert is_target_card("青眼の白龍", "BLUE EYES WHITE DRAGON(青眼の白龍)") is True

    def test_ascii_leading_legitimate_name(self):
        # カード名自体が英字始まり（TG=テックジーナス）。先頭一致のため前方チェックは走らない。
        assert is_target_card("TG ハイパー・ライブラリアン", "TG ハイパー・ライブラリアン") is True

    def test_no39_exact(self):
        assert is_target_card("No.39 希望皇ホープ", "No.39 希望皇ホープ") is True


# ──────────────────────────────────────────────
# 別カードの除外維持（修正前から除外されており、過剰許容で復活しないこと）
# ──────────────────────────────────────────────

class TestOtherCardsStillExcluded:
    """日本語接辞や末尾[カナ読み]を持つ別カードが復活しないこと（厳密括弧 carve-out の副作用回帰防止）"""

    def test_toon_black_magician(self):
        # 末尾 [トゥーンブラックマジシャン] のカナ読み括弧に基底名が出現するが別カード
        name = ("トゥーンブラックマジシャン【シークレット】{RV01-JP010}"
                "《モンスター》[トゥーンブラックマジシャン]")
        assert is_target_card("ブラック・マジシャン", name) is False

    def test_phaohs_servant_black_magician(self):
        name = ("王のしもべブラックマジシャン【ウルトラ】{LOCH-JP001}"
                "《モンスター》[ファラオノシモベブラックマジシャン]")
        assert is_target_card("ブラック・マジシャン", name) is False

    def test_japanese_prefix_compound(self):
        # 「トゥーン・ブラック・マジシャン」= 日本語接頭辞の別カード
        assert is_target_card("ブラック・マジシャン", "トゥーン・ブラック・マジシャン") is False

    def test_japanese_suffix_compound(self):
        # 「No.39 希望皇ホープ・ライトニング」= 日本語接尾辞の別カード
        assert is_target_card("No.39 希望皇ホープ", "No.39 希望皇ホープ・ライトニング") is False


# ──────────────────────────────────────────────
# 店舗が商品名から記号を落とすケース（2026-08-03 追加）
# ──────────────────────────────────────────────

class TestSymbolsDroppedByShop:
    """カードラッシュ等は商品名から記号を落として登録する。
    「あってもなくても同一カード」として一致させること。
    商品名はすべて2026-08-03にカードラッシュの実データから採取した。
    """

    def test_kagi_kakko(self):
        assert is_target_card(
            "炎舞－「天枢」",
            "炎舞天枢【ノーマル】{CBLZ-JP058}《魔法》[エンブテンスウ]") is True
        assert is_target_card(
            "召喚魔術－「剣」",
            "召喚魔術剣【プリズマティックシークレット】{CORI-JP053}《魔法》[ショウカンマジュツツルギ]") is True

    def test_nijuu_kagi_kakko(self):
        assert is_target_card(
            "星遺物－『星杯』",
            "星遺物星杯【レア】{COTD-JP023}《モンスター》[セイイブツセイハイ]") is True

    def test_star_marks(self):
        assert is_target_card(
            "Live☆Twin リィラ",
            "LiveTwinリィラ【ノーマル】{DBGI-JP014}《モンスター》[ＬｉｖｅＴｗｉｎリィラ]") is True
        assert is_target_card(
            "ヤミー★スナッチー",
            "ヤミースナッチー【ウルトラ】{DBJH-JP022}《リンク》[ヤミースナッチー]") is True

    def test_double_quotes(self):
        assert is_target_card(
            "セリオンズ“キング”レギュラス",
            "セリオンズキングレギュラス【ノーマル】{TT01-JPA07}《モンスター》[セリオンズキングレギュラス]") is True

    def test_ampersand(self):
        assert is_target_card(
            "ドロール＆ロックバード",
            "ドロールロックバード(鳥右上)【ウルトラ】{QCAC-JP070}《モンスター》[ドロールロックバード]") is True

    def test_period(self):
        assert is_target_card(
            "P.U.N.K.JAM FEVER！",
            "PUNKJAMFEVER(ロゴ)【スーパー】{QCTB-JP020}《エクシーズ》[PUNKJAMFEVER]") is True

    def test_multiplication_sign(self):
        assert is_target_card(
            "ふわんだりぃず×いぐるん",
            "ふわんだりぃずいぐるん【ノーマル】{BODE-JP014}《モンスター》[フワンダリィズイグルン]") is True

    def test_angle_brackets(self):
        assert is_target_card(
            "M∀LICE＜C＞GWC－０６",
            "M∀LICECGWC06【ノーマル】{DBCB-JP023}《罠》[Ｍ∀LICECGWC06]") is True

    def test_apostrophes(self):
        # 同一店舗内で ’(U+2019) あり / '(U+0027) あり / 記号なし の3表記が混在する
        assert is_target_card(
            "Evil★Twin’s トラブル・サニー",
            "EvilTwinsトラブルサニー【ウルトラ】{DBGI-JP045}《リンク》") is True
        assert is_target_card(
            "Evil★Twin’s トラブル・サニー",
            "EvilTwin'sトラブルサニー【シークレット】{DBGI-JP045}《リンク》") is True
        assert is_target_card(
            "糾罪巧α’－「orgIA」",
            "糾罪巧αorgIA【ノーマル】{TEST-JP001}《罠》") is True

    def test_apostrophe_kept_by_shop_still_matches(self):
        # 記号を残して登録している商品も従来どおり一致すること
        assert is_target_card(
            "竜輝巧－ファフμβ’",
            "竜輝巧－ファフμβ’【ウルトラ】{QCCP-JP179}") is True

    def test_wave_dash(self):
        assert is_target_card(
            "エルフェンノーツ〜狂奏のラプソディア〜",
            "エルフェンノーツ狂奏のラプソディア【ノーマル】{BPRO-JP072}《罠》[エルフェンノーツ]") is True

    def test_symbols_kept_by_shop_still_match(self):
        # 記号を残して登録している店舗（トレコロ等）の表記も従来どおり一致すること
        assert is_target_card("召喚魔術－「剣」", "召喚魔術「剣」【レア】") is True
        assert is_target_card("召喚魔術－「剣」", "召喚魔術－「剣」【レア】") is True
        assert is_target_card("ヴィサス＝サンサーラ", "ヴィサス＝サンサーラ【ウルトラ】{LOCH-JP031}") is True


class TestIdentifierSymbolsAreNotSeparators:
    """ギリシャ文字などカードを識別する文字は区切り扱いしないこと。
    区切りにすると「磁石の戦士α」と「磁石の戦士β」が同一カードになってしまう。
    """

    def test_greek_letters_distinguish_cards(self):
        assert is_target_card("磁石の戦士α", "磁石の戦士β") is False
        assert is_target_card("磁石の戦士β", "磁石の戦士γ") is False
        assert is_target_card("竜輝巧－アルζ", "竜輝巧－エルγ") is False
        assert is_target_card("PSYフレームギア・γ", "PSYフレームギア・α") is False

    def test_plus_minus_distinguish_cards(self):
        assert is_target_card("磁石の戦士Σ＋", "磁石の戦士Σ－") is False

    def test_trailing_dash_does_not_absorb_plus_variant(self):
        """末尾の「－」で縮退したパターンが Σ+ の商品を拾わないこと（2026-08-04 修正）。

        「磁石の戦士Σ－」は末尾が区切り記号のためパターンが「磁石の戦士Σ」に縮退する。
        末尾境界チェックが「区切りを読み飛ばした先の文字種」を見るようになったため、
        直後の「+」（識別子であって区切りではない）で別カードとして弾ける。
        逆方向（Σ＋で検索してΣ－の商品）は修正前から除外されていた。
        """
        assert is_target_card("磁石の戦士Σ－", "磁石の戦士Σ+【ノーマル】{BPRO-JP005}") is False
        assert is_target_card("磁石の戦士Σ＋", "磁石の戦士Σ-【ノーマル】{BPRO-JP004}") is False

    def test_trailing_dash_card_still_matches_itself(self):
        # 縮退しても自分自身（記号あり・なしの両表記）は拾えること
        assert is_target_card("磁石の戦士Σ－", "磁石の戦士Σ-【ノーマル】{BPRO-JP004}") is True
        assert is_target_card("磁石の戦士Σ－", "磁石の戦士Σ【ノーマル】{BPRO-JP004}") is True

    def test_greek_letter_card_matches_itself(self):
        assert is_target_card("磁石の戦士α", "磁石の戦士α【ノーマル】{SR3-JP001}") is True
        assert is_target_card("竜輝巧－アルζ", "竜輝巧アルζ【ウルトラ】{QCCP-JP178}") is True


class TestTailBoundaryFollowsSeparatorSet:
    """末尾境界チェックが区切り集合 `_FLEX_SEP_CHARS` と同期していること（2026-08-04 修正）。

    修正前は末尾判定が `nc in "・－-ー、,&"` の6文字ハードコードで、区切り集合に
    後から追加した記号（／＠＝「」☆ など）を見ていなかった。このため
    「区切り記号を挟んだより長い別カード名」を弾けなかった。
    断りのないケースはカード名マスタに実在する別カードの組（実店舗の商品名から採取）。
    """

    def test_slash_buster(self):
        # 「／バスター」は元カードとは別のカード（バスターモード関連）
        assert is_target_card("スターダスト・ドラゴン", "スターダスト・ドラゴン／バスター【ウルトラ】") is False
        assert is_target_card("ブラック・ローズ・ドラゴン", "ブラック・ローズ・ドラゴン／バスター") is False
        assert is_target_card("TG ハルバード・キャノン", "TG ハルバード・キャノン／バスター") is False

    def test_dash_plus_kagikakko(self):
        # 「召喚魔術」と「召喚魔術－「剣」」は別カード（どちらもマスタに実在）
        assert is_target_card("召喚魔術", "召喚魔術－「剣」【レア】") is False
        assert is_target_card("召喚魔術", "召喚魔術剣【プリズマティックシークレット】{CORI-JP053}") is False
        # 「炎舞」単体はマスタに無いが、機構（区切りを挟んだ続きを見る）の回帰用に残す
        assert is_target_card("炎舞", "炎舞－「天枢」【ノーマル】{CBLZ-JP058}") is False

    def test_digit_suffix_is_another_card(self):
        # 実データで最も多かった取り込み。「ハーピィ・レディ」と「ハーピィ・レディ１」は別カード
        assert is_target_card(
            "ハーピィ・レディ",
            "ハーピィレディ1【ノーマル】{RDS-JP017}《モンスター》[ハーピィレディワン]") is False
        assert is_target_card("ハーピィ・レディ", "ハーピィ・レディ３") is False
        assert is_target_card("ハネクリボー", "ハネクリボー ＬＶ６") is False
        assert is_target_card("水陸両用バグロス", "水陸両用バグロス Mk-3") is False

    def test_space_then_another_name_is_excluded(self):
        # 空白区切りで別カード名が続くケース（トレコロ・遊々亭の表記）
        assert is_target_card("赤き竜", "赤き竜 ケッツァーコアトル") is False
        assert is_target_card("鉄の騎士", "鉄の騎士　ギア・フリード") is False
        assert is_target_card("カオス・ソルジャー", "カオス・ソルジャー　－開闢の使者－") is False
        assert is_target_card(
            "究極宝玉神 レインボー・ドラゴン",
            "究極宝玉神 レインボー・ドラゴン オーバー・ドライブ") is False

    def test_trailing_separator_only_is_kept(self):
        # 区切りだけで終わる商品名は「続きなし」として採用する（旧実装より緩くなる唯一の方向）
        assert is_target_card("青眼の白龍", "青眼の白龍・") is True
        assert is_target_card("青眼の白龍", "青眼の白龍 ") is True
        assert is_target_card("青眼の白龍", "青眼の白龍／") is True

    def test_closing_bracket_only_is_kept(self):
        # 注記の閉じ括弧だけが残る場合（併記注記の内側で一致したケース）
        assert is_target_card("青眼の白龍", "BLUE EYES WHITE DRAGON(青眼の白龍)") is True
        assert is_target_card("青眼の白龍", "青眼の白龍)") is True

    def test_double_corner_bracket_is_a_shop_annotation(self):
        """『』はカードラッシュが商品属性に使うため注記扱い（区切りにしない）。

        「青眼の白龍『25thANNIVERSARYULTIMATEKAIBASET』」は本物の青眼の白龍
        （シークレット・8,780〜24,800円で実在）。ここを別カード扱いにすると
        正当な出品を落として min_price が上がる。
        カード名側の『』（星遺物－『星杯』等27件）は基底名が実在しないため衝突しない。
        """
        assert is_target_card(
            "青眼の白龍",
            "青眼の白龍『25thANNIVERSARYULTIMATEKAIBASET』【シークレット】{-}《モンスター》") is True
        # 一方 「」で包んだ商品名の後ろに別物が続く場合は従来どおり弾く
        # （トレコロ「青眼の白龍」20th ANNIVERSARY GOLD EDITION = 5,980,000円の純金製）
        assert is_target_card("青眼の白龍", "「青眼の白龍」20th ANNIVERSARY GOLD EDITION") is False

    def test_at_mark_and_equals(self):
        assert is_target_card("リンクスレイヤー", "リンクスレイヤー＠イグニスター") is False
        assert is_target_card("新世壊", "新世壊＝アムリターラ") is False

    def test_star_mark(self):
        # ☆★ を区切りに入れた以上、☆で続く別カード名も弾けること
        # （「Live☆Twin」単体はマスタに無い。機構の回帰用）
        assert is_target_card("Live☆Twin", "Live☆Twin リィラ【ノーマル】{DBGI-JP014}") is False

    def test_annotation_brackets_are_not_name_continuation(self):
        # 店舗の注記（レアリティ・型番・種類・カナ読み・状態・補足）で終わる商品名は維持する
        for product in (
            "青眼の白龍【ウルトラ】{25TH-JP001}《モンスター》[ブルーアイズホワイトドラゴン]",
            "青眼の白龍(25TH-JP001)",
            "青眼の白龍〔状態A〕",
            "青眼の白龍《モンスター》",
            "青眼の白龍[ブルーアイズホワイトドラゴン]",
        ):
            assert is_target_card("青眼の白龍", product) is True, product


class TestExactBracketCarveOutExcludesCornerBrackets:
    """厳密括弧 carve-out（前方チェックの免除）に 「」『』 を含めないこと（2026-08-04 修正）。

    この2種はカード名の一部として実在するため、併記注記の括弧として扱うと
    別カードやグッズを同一カード扱いしてしまう。商品名はすべて実店舗から採取。
    """

    def test_card_type_field_does_not_absorb_same_named_card(self):
        """カードラッシュの種別欄《融合》がカード名「融合」を全融合モンスターに一致させていた。

        実測（2026-08-04・実店舗9経路）: カード名「融合」で通っていた506件のうち500件が
        この経路による別カード。厳密括弧 carve-out から《》を外して解消した。
        """
        assert is_target_card(
            "融合",
            "ファントムオブユベル【ウルトラ】{VX04-JP002}《融合》[ファントムオブユベル]") is False
        assert is_target_card(
            "融合", "クインテットマジシャン【ウルトラ】{VB20-JP001}《融合》[クインテットマジシャン]") is False
        # 「融合」本体の商品は従来どおり拾う
        assert is_target_card("融合", "融合【ノーマル】{SD46-JP026}《魔法》[ユウゴウ]") is True

    def test_magic_card_bracket_is_another_card(self):
        # 「マジックカード「死者蘇生」」はトレコロ・カードラボが扱う実在の別カード
        assert is_target_card("死者蘇生", "マジックカード「死者蘇生」") is False
        assert is_target_card("死者蘇生", "マジックカード「死者蘇生」[赤文字]") is False
        assert is_target_card("クロス・ソウル", "マジックカード「クロス・ソウル」[銀文字]") is False

    def test_goods_in_double_corner_brackets_excluded(self):
        # カードラッシュのグッズ表記。カード本体ではない
        assert is_target_card(
            "天霆號アーゼウス",
            "(未開封)サイコロ『天霆號アーゼウス』【-】{-}《その他》") is False
        assert is_target_card(
            "青眼の白龍", "金属製カード『青眼の白龍』(未開封)【-】{-}《その他》") is False

    def test_paren_carve_out_still_works(self):
        # 丸括弧の併記注記は従来どおり許容する（carve-out の本来の用途）
        assert is_target_card("青眼の白龍", "BLUE EYES WHITE DRAGON(青眼の白龍)") is True


class TestSeparatorSetsStayInSync:
    """区切り集合と境界判定の集合がずれないための不変条件（2026-08-04 追加）。

    このテストが落ちたら「片方だけ直した」ということ。過去に2度、同じ集合が
    複数箇所に重複していて片方だけ直され、別カードの取り込みが起きている。
    """

    def test_every_separator_is_skipped_at_the_boundary(self):
        # 注記括弧を除く全ての区切りは、末尾境界で読み飛ばされること
        for ch in _FLEX_SEP_ALL:
            if ch in _ANNOTATION_BRACKETS or ch.isspace():
                continue
            assert ch in _BOUNDARY_SKIP_CHARS, f"{ch!r} が末尾境界の読み飛ばし集合に無い"

    def test_annotation_brackets_are_never_skipped(self):
        # 注記括弧は「ここでカード名は終わり」の目印なので読み飛ばしてはいけない
        assert not (set(_ANNOTATION_BRACKETS) & set(_BOUNDARY_SKIP_CHARS))

    def test_flex_split_covers_the_separator_set(self):
        # パターン生成側（正規表現）と区切り集合が同じ文字を指していること
        for ch in _FLEX_SEP_ALL:
            assert re.fullmatch(_FLEX_SPLIT, ch), f"{ch!r} が _FLEX_SPLIT に含まれない"

    def test_no_char_matched_by_flex_split_is_treated_as_continuation(self):
        """逆方向の不変条件: 正規表現が区切りとして受理する文字は、
        必ず末尾境界でも読み飛ばされる（または注記括弧である）こと。

        上の2件は `_BOUNDARY_SKIP_CHARS` が `_FLEX_SEP_ALL` から導出されている限り
        構成上必ず通るが、このテストは**正規表現側にだけ文字を直書きした場合**に落ちる。
        今回直したバグ（パターン側だけ区切りが増え、境界判定が追随しなかった）と
        同じ形の変更を捕まえるのが目的。
        走査は U+0000〜U+3100（区切り集合と Unicode 空白はすべてこの範囲に収まる）。
        """
        pat = re.compile(_FLEX_SPLIT)
        for cp in range(0x3101):
            ch = chr(cp)
            if not pat.fullmatch(ch):
                continue
            assert (ch.isspace() or ch in _BOUNDARY_SKIP_CHARS
                    or ch in _ANNOTATION_BRACKETS), f"{ch!r}({cp:#x}) が末尾境界で続き扱いされる"

    def test_separator_behaviour_end_to_end(self):
        """定数を見ない振る舞いベースの不変条件（実装の内部構造が変わっても効く）"""
        for ch in "／＠＝☆★「」・-~!?.<>&":
            assert is_target_card("青眼の白龍", f"青眼の白龍{ch}バスター") is False, ch
            assert is_target_card("青眼の白龍", f"青眼の白龍{ch}") is True, ch

    def test_master_has_no_base_name_colliding_with_double_corner_bracket(self):
        """『』を注記括弧側に倒した前提の不変条件。

        「X『Y』」形式のカード名について基底名 X がマスタに実在しないこと。
        実在するようになったら is_target_card('X', 'X『Y』…') が静かに True になる。
        cardnames_ja.json は毎週月曜に update-cardnames.yml が自動更新するが、
        **現状どのワークフローも pytest を実行していない**ため、この検知が働くのは
        手元で pytest を回したときだけ（CI追加は TASKS.md にチケット化済み）。

        比較は区切り記号を全除去した正規形で行う。完全一致だと表記ゆれ
        （'星・遺物' vs '星遺物'）をすり抜けるため。
        """
        names = json.loads((ROOT / "data" / "cardnames_ja.json").read_text(encoding="utf-8"))
        if isinstance(names, dict):
            names = list(names.keys())

        def canon(s: str) -> str:
            s = normalize_width(s)
            for ch in _FLEX_SEP_ALL:
                s = s.replace(ch, "")
            return "".join(s.split())

        canon_all = {canon(n) for n in names}
        collisions = []
        for n in names:
            nn = normalize_width(n)
            i = nn.find("『")
            if i <= 0:
                continue
            base = canon(nn[:i])
            if base and base in canon_all:
                collisions.append((base, n))
        assert not collisions, f"『』の基底名がマスタに実在する: {collisions}"


class TestEmptyPatternGuard:
    """カード名が区切り記号だけの場合に無関係な商品を拾わないこと。

    区切り集合を広げた副作用でパターンが空文字になり、正規表現の空マッチが
    全位置で成立して記号始まりの商品名に一致してしまうため、明示的に弾いている。
    通常のカード名では起こらないが、ユーザー入力が /api/search に直接届く。
    """

    def test_symbol_only_card_name_matches_nothing(self):
        product = "【ノーマル】{ABC-JP001}《魔法》[テスト]"
        for q in ("☆", "「」", "★☆", ".", "~", "!?", "・", "", "   "):
            assert is_target_card(q, product) is False, f"{q!r} が誤って一致した"

    def test_symbol_only_does_not_match_real_card(self):
        assert is_target_card("☆", "灰流うらら【ウルトラ】{TEST}") is False


class TestBaseNameStillExcludedAfterSymbolChange:
    """記号を区切り扱いにしたことで、基底名だけの別カードを拾わないこと"""

    def test_base_card_is_not_matched(self):
        # 「召喚魔術」は「召喚魔術－「剣」」とは別カード
        assert is_target_card(
            "召喚魔術－「剣」",
            "召喚魔術【ウルトラ】{CORI-JPS03}《魔法》[ショウカンマジュツ]") is False

    def test_different_suffix_not_matched(self):
        # 「杯」と「剣」は別カード
        assert is_target_card("召喚魔術－「杯」", "召喚魔術剣【スーパー】{CORI-JP053}") is False


# ──────────────────────────────────────────────
# 境界判定ヘルパの単体テスト
# ──────────────────────────────────────────────

class TestBoundaryHelpers:
    """新規ヘルパ _has_name_text_outside_brackets / _is_exactly_bracketed の挙動"""

    def test_name_text_detects_alpha(self):
        assert _has_name_text_outside_brackets("Sin ") is True

    def test_name_text_detects_japanese(self):
        assert _has_name_text_outside_brackets("メタル化") is True

    def test_name_text_ignores_bracketed(self):
        # 括弧内の英字/日本語は名前延長とみなさない
        assert _has_name_text_outside_brackets("(25TH-JP001)") is False

    def test_exactly_bracketed_true(self):
        # "...(青眼の白龍)" の (青眼の白龍) 部分
        text = "BLUE EYES WHITE DRAGON(青眼の白龍)"
        start = text.index("青眼の白龍")
        end = start + len("青眼の白龍")
        assert _is_exactly_bracketed(text, start, end) is True

    def test_exactly_bracketed_false_when_glued(self):
        # "Sin 青眼の白龍" は括弧で包まれていない
        text = "Sin 青眼の白龍"
        start = text.index("青眼の白龍")
        end = start + len("青眼の白龍")
        assert _is_exactly_bracketed(text, start, end) is False
