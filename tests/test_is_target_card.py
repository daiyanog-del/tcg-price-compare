"""
tests/test_is_target_card.py — is_target_card の名前一致境界判定の回帰テスト

背景:
  共有関数 is_target_card は「検索したカード名」と「店舗の商品名」が同一カードを
  指すかを判定する。全5店(遊々亭/カードラボ/カードラッシュ/トレコロ/カーナベルES)が
  この1関数を共有するため、ここの退行は全店に波及する。

  以前、前後境界判定が「日本語の続き」だけを別カード名の延長とみなしていたため、
  英字接頭辞/接尾辞の付いた別カードを基底名の検索結果に取り込む過剰一致があった
  （例: 「青眼の白龍」検索に「Sin 青眼の白龍」が一致）。これを抑制した修正の回帰を防ぐ。

テスト方針:
  - ネットワーク・認証情報は不要。is_target_card を直接呼ぶだけ。
  - ケースは全5店の実データ992ペアでの検証時に実際に観測された商品名から採用。
"""

import sys
from pathlib import Path

# テスト対象モジュール（リポジトリルートを import パスに追加）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scraper import is_target_card, _is_exactly_bracketed, _has_name_text_outside_brackets


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

    def test_trailing_dash_is_a_known_gap(self):
        """既知の不具合（今回の変更による回帰ではない）。

        末尾の「－」は従来から区切り扱いのため「磁石の戦士Σ－」のパターンが
        「磁石の戦士Σ」に縮退し、Σ+ の商品まで拾ってしまう。逆方向（Σ＋で検索）は
        末尾に記号が残るため正しく除外される、という非対称がある。
        根治は末尾境界チェックを区切り集合と同期させること（TASKS.md 参照）。
        現状を固定しておき、直したときにこのテストが落ちて気づけるようにする。
        """
        assert is_target_card("磁石の戦士Σ－", "磁石の戦士Σ+【ノーマル】{BPRO-JP005}") is True

    def test_greek_letter_card_matches_itself(self):
        assert is_target_card("磁石の戦士α", "磁石の戦士α【ノーマル】{SR3-JP001}") is True
        assert is_target_card("竜輝巧－アルζ", "竜輝巧アルζ【ウルトラ】{QCCP-JP178}") is True


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
