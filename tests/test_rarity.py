"""
tests/test_rarity.py — normalize_rarity の系列統合テスト（2026-08-03 の棚卸し由来）

背景:
  同一のレアリティを店舗が別表記で書いており、rarity.py の aliases に
  どちらも無いために normalize_rarity が生表記のまま通し、price_history に
  別系列として蓄積されていた。値動きRPCは (card_name, rarity) でグループ化して
  「両日に記録がある共通店舗」を突き合わせ、ガード②で同方向に動いた共通店舗が
  2店以上あることを要求するため、系列が割れていると構造上検出されない（偽陰性）。

  期待値はすべて 2026-08-03 時点の price_history 実データ（distinct rarity × shop）
  から採取した生表記であり、投機的な登録は含まない。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from rarity import (
    RARITIES,
    UNKNOWN_RARITY_LABEL,
    normalize_rarity,
    order_of,
    ordered_canonicals,
    slug_of,
)


class TestMillenniumFamily:
    """ミレニアム系は店舗ごとに3系列（トレコロCB=「〜レア」あり / カードラッシュ・
    カードラボ=なし / 遊々亭=略号）へ割れていた。"""

    @pytest.mark.parametrize("raw", ["ミレニアムウルトラレア", "ミレニアムウルトラ", "M-UR"])
    def test_ultra(self, raw):
        assert normalize_rarity(raw) == "ミレニアムウルトラ"

    @pytest.mark.parametrize("raw", ["ミレニアムスーパーレア", "ミレニアムスーパー", "M-SR"])
    def test_super(self, raw):
        assert normalize_rarity(raw) == "ミレニアムスーパー"

    @pytest.mark.parametrize("raw", ["ミレニアムシークレットレア", "ミレニアムシークレット", "M-SE"])
    def test_secret(self, raw):
        assert normalize_rarity(raw) == "ミレニアムシークレット"

    @pytest.mark.parametrize("raw", ["ミレニアムゴールドレア", "ミレニアムゴールド"])
    def test_gold(self, raw):
        assert normalize_rarity(raw) == "ミレニアムゴールド"

    @pytest.mark.parametrize("raw", ["ミレニアム", "ミレニアムレア", "M", "ML", "ミレ"])
    def test_plain_millennium_is_a_separate_rarity(self, raw):
        # 無印ミレニアム(=ミレニアムレア)は上位4種とは別物。
        # 実データで同一カード・同一店に両方が併存する（死者蘇生 トレコロCB:
        # ミレニアム ¥230 / ミレニアムシークレットレア ¥3,880）。
        assert normalize_rarity(raw) == "ミレニアム"


class TestOverFrameFamily:
    """カーナベルだけが「オバフレ〜」の短縮表記を使っていた。"""

    @pytest.mark.parametrize("raw", ["オバフレプリシク", "O-PSE", "OFプリズマティックシークレットレア"])
    def test_prismatic(self, raw):
        assert normalize_rarity(raw) == "OFプリシク"

    @pytest.mark.parametrize("raw", ["オバフレウル", "O-UR", "オーバーフレームウルトラレア"])
    def test_ultra(self, raw):
        assert normalize_rarity(raw) == "OFウルトラ"

    @pytest.mark.parametrize("raw", ["オバフレシク", "O-SE", "オーバーフレームシークレットレア"])
    def test_secret(self, raw):
        assert normalize_rarity(raw) == "OFシークレット"


class TestNewCanonicals:
    @pytest.mark.parametrize("raw", ["エクストラシークレットパラレルレア", "P-EXSE"])
    def test_exsecret_parallel(self, raw):
        # トレコロCBと遊々亭が同じ4枚を別表記で出していた
        assert normalize_rarity(raw) == "EXシークレットパラレル"

    def test_exsecret_parallel_is_not_exsecret(self):
        assert normalize_rarity("エクストラシークレットレア") == "EXシークレット"

    @pytest.mark.parametrize("raw", ["KCウルトラレア", "KCウルトラ", "KC-UR"])
    def test_kc_ultra(self, raw):
        assert normalize_rarity(raw) == "KCウルトラ"

    def test_kc_stays_separate_from_kc_ultra(self):
        # カードラッシュは同一カード（守護神官マハード・青眼の白龍）で
        # 「KC」と「KCウルトラ」を併記するため別レアリティのまま残す
        assert normalize_rarity("KC") == "KC"


class TestNormalRare:
    """「ノーマルレア」を ノーマル へ畳んでいた既存 alias の誤りを解除した。
    実データでも別物（脆刃の剣: 遊々亭 NR ¥120 / ノーマル ¥50）。"""

    @pytest.mark.parametrize("raw", ["ノーマルレア", "NR", "Nレア"])
    def test_normal_rare(self, raw):
        assert normalize_rarity(raw) == "ノーマルレア"

    @pytest.mark.parametrize("raw", ["ノーマル", "N", "ノー"])
    def test_normal_unaffected(self, raw):
        assert normalize_rarity(raw) == "ノーマル"


class TestPreprocessing:
    """空白・丸括弧の有無だけが違う表記は前処理で畳む。
    カードラッシュは空白あり/なしの2表記、カードラボは括弧付きを出していた。"""

    @pytest.mark.parametrize("raw", [
        "シークレットSPECIALREDVer.",
        "シークレットSPECIAL RED Ver.",
        "シークレット(SPECIAL RED Ver.)",
        "シークレット（SPECIAL　RED　Ver.）",
    ])
    def test_special_red(self, raw):
        assert normalize_rarity(raw) == "シークレットレッド"

    @pytest.mark.parametrize("raw", [
        "シークレットSPECIALBLUEVer.",
        "シークレット(SPECIAL BLUE Ver.)",
        "シークレットBLUEVer.",
    ])
    def test_special_blue(self, raw):
        assert normalize_rarity(raw) == "シークレットブルー"

    def test_preprocessing_does_not_mangle_unknown_values(self):
        # 未知表記は前処理前の文字列をそのまま返す（生表記を勝手に改変しない）
        raw = "クォーターセンチュリーシークレットGREEN Ver."
        assert normalize_rarity(raw) == raw

    def test_strip_still_applies(self):
        assert normalize_rarity("  ウルトラレア  ") == "ウルトラ"


class TestDashOnlyIsIsolated:
    """ダッシュ1文字は「レアリティ無し」の印であってレアリティ名ではない。
    price_history に119行（カードラッシュ/カードラボ）が独立系列で入っていた。"""

    @pytest.mark.parametrize("raw", ["-", "－", "ー", "–", "—", " - "])
    def test_dash_becomes_unknown_label(self, raw):
        assert normalize_rarity(raw) == UNKNOWN_RARITY_LABEL

    def test_dash_inside_a_real_rarity_is_untouched(self):
        # 略号のハイフンは落とさない
        assert normalize_rarity("M-UR") == "ミレニアムウルトラ"
        assert normalize_rarity("P-SE") == "シークレットパラレル"
        assert normalize_rarity("KC-N") == "KCノーマル"


class TestKCFamily:
    """「KC」は接頭辞で実体は KC-N/KC-R/KC-UR。判別できるものはその階層へ、
    判別できないものは canonical「KC」へ寄せる（2026-08-03 の運用判断）。"""

    @pytest.mark.parametrize("raw,expected", [
        ("KC-UR", "KCウルトラ"),
        ("KCウルトラレア", "KCウルトラ"),
        ("KCウルトラ", "KCウルトラ"),
        ("KC-R", "KCレア"),
        ("KCレア", "KCレア"),
        ("KC-N", "KCノーマル"),
        ("KCノーマル", "KCノーマル"),
        ("KC", "KC"),          # 判別不能はそのまま
    ])
    def test_kc(self, raw, expected):
        assert normalize_rarity(raw) == expected


class TestNormalParallelSpec:
    def test_torecolo_parallel_spec_is_merged_provisionally(self):
        # トレコロCBの「ノーマル(パラレル仕様)」は暫定で ノーマルパラレル と同一物扱い
        assert normalize_rarity("ノーマル(パラレル仕様)") == "ノーマルパラレル"
        assert normalize_rarity("ノーマル（パラレル仕様）") == "ノーマルパラレル"
        assert normalize_rarity("ノーマルパラレル") == "ノーマルパラレル"

    def test_plain_normal_is_unaffected(self):
        assert normalize_rarity("ノーマル") == "ノーマル"


class TestRegressionOfExistingAliases:
    """既存の統合が壊れていないこと（抜き取り）。"""

    @pytest.mark.parametrize("raw,expected", [
        ("QCシク", "25thシークレット"),
        ("RE", "アルティメット"),
        ("N-P", "ノーマルパラレル"),
        ("PN", "ノーマルパラレル"),
        ("ゴル", "ゴールド"),
        ("SR-P", "スーパーパラレル"),
        ("UR-P", "ウルトラパラレル"),
        ("シークレットレア", "シークレット"),
        ("ウルトラレア", "ウルトラ"),
        ("字レア", "字レア"),
        ("ラッシュレア", "ラッシュレア"),
    ])
    def test_alias(self, raw, expected):
        assert normalize_rarity(raw) == expected

    def test_empty(self):
        assert normalize_rarity("") == ""


class TestTableIntegrity:
    def test_slugs_are_unique(self):
        slugs = [e["slug"] for e in RARITIES]
        assert len(slugs) == len(set(slugs))

    def test_orders_are_unique(self):
        orders = [e["order"] for e in RARITIES]
        assert len(orders) == len(set(orders))

    def test_slugs_are_ascii_css_safe(self):
        for e in RARITIES:
            assert e["slug"].replace("-", "").isalnum(), e["slug"]
            assert e["slug"].isascii(), e["slug"]

    def test_new_canonicals_registered(self):
        for canon in ("ミレニアムシークレット", "ミレニアムウルトラ", "ミレニアムゴールド",
                      "ミレニアムスーパー", "EXシークレットパラレル", "KCウルトラ",
                      "KCレア", "KCノーマル", "シークレットレッド", "ノーマルレア"):
            assert canon in ordered_canonicals()
            assert slug_of(canon) != "unknown"
            assert order_of(canon) != 9999

    def test_millennium_family_ordered_before_plain_millennium(self):
        # 表示順が無印ミレニアムの直前に固まっていること
        for canon in ("ミレニアムシークレット", "ミレニアムウルトラ",
                      "ミレニアムゴールド", "ミレニアムスーパー"):
            assert order_of(canon) < order_of("ミレニアム")
            assert order_of(canon) > order_of("ホログラフィック")
