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
