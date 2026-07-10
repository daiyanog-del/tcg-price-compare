"""
tests/test_name_normalize.py -- name_normalize.canonicalize_card_name の単体テスト
（フェーズ3 P1、docs/design-phase3-generation-side.md 「P1: 書き込み時名寄せゼロ」）

canonicalize_card_name は tracked_cards への登録経路（app._correct_cardname /
collect_prices.canonicalize_tracked_name）が共通で使う名寄せロジック本体。
4分岐（完全一致 / fuzzy一意一致 / fuzzy衝突 / 未知）を検証する。
ネットワーク・実ファイル（cardnames_ja.json）は一切使わない。

呼び出し側契約（matched の意味）:
  matched=True（完全一致・fuzzy一意一致・fuzzy衝突の3系統）では、
  canonicalize_card_name が既に最終判定を下しているため、呼び出し側は
  読み仮名照合等の追加フォールバックを試みてはならない。特にfuzzy衝突は
  「誤って別カードへ合流させない」安全側の補正見送りなので、ここで別ルートの
  補正を試みると安全策を迂回してしまう。matched=False（fuzzy_keyがインデックスに
  存在しない＝未知のカード名）のときのみ、追加フォールバックを試みてよい
  （app._correct_cardname の読み仮名分岐はこの契約に基づく）。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from name_normalize import canonicalize_card_name, fuzzy_key


class TestExactMatch:
    def test_exact_match_passthrough(self):
        resolved, matched = canonicalize_card_name(
            "青眼の白龍", {"青眼の白龍"}, {}
        )
        assert resolved == "青眼の白龍"
        assert matched is True

    def test_empty_cardnames_set_short_circuits(self):
        # cardnames_ja.json 未ロード時は補正材料がないためそのまま返す（matched=True扱い）
        resolved, matched = canonicalize_card_name("何か", set(), {})
        assert resolved == "何か"
        assert matched is True


class TestUniqueFuzzyMatch:
    def test_unique_fuzzy_match_resolves_to_formal_name(self):
        key = fuzzy_key("ブラック・マジシャン")  # 中黒を除去したキー
        resolved, matched = canonicalize_card_name(
            "ブラックマジシャン",
            {"ブラック・マジシャン"},
            {key: ["ブラック・マジシャン"]},
        )
        assert resolved == "ブラック・マジシャン"
        assert matched is True


class TestFuzzyCollision:
    def test_collision_returns_input_unchanged(self):
        # E・HERO と E-HERO のように記号除去で同一キーに衝突する実在別カード群を模擬
        resolved, matched = canonicalize_card_name(
            "EHERO",
            {"E・HERO", "E-HERO"},
            {"ehero": ["E・HERO", "E-HERO"]},
        )
        # 衝突キーでしか一致しない入力はそのまま返る（誤ったカードへの合流を避ける）
        assert resolved == "EHERO"
        assert matched is True

    def test_collision_calls_warn_callback(self):
        warnings = []
        canonicalize_card_name(
            "EHERO",
            {"E・HERO", "E-HERO"},
            {"ehero": ["E・HERO", "E-HERO"]},
            warn=warnings.append,
            context="test",
        )
        assert len(warnings) == 1
        assert "fuzzy_key衝突" in warnings[0]
        assert "context='test'" in warnings[0]

    def test_collision_without_warn_callback_does_not_raise(self):
        # warn未指定でも例外を出さない（呼び出し側が省略できる）
        resolved, matched = canonicalize_card_name(
            "EHERO", {"E・HERO", "E-HERO"}, {"ehero": ["E・HERO", "E-HERO"]},
        )
        assert resolved == "EHERO"


class TestUnknown:
    def test_unknown_name_passthrough_and_not_matched(self):
        # fuzzy_key がインデックスに存在しない未知のカード名。normalize済みのままそのまま返す
        resolved, matched = canonicalize_card_name(
            "未知のカードXYZ", {"青眼の白龍"}, {}
        )
        assert resolved == "未知のカードXYZ"
        assert matched is False


class TestMatchedContract:
    """matched の呼び出し側契約を固定するテスト:
    matched=True の3系統（完全一致・fuzzy一意・fuzzy衝突）では、呼び出し側は追加
    フォールバック（読み仮名照合等）を試みてはならない。特に衝突時に matched=True
    であることは、呼び出し側が誤って別ルートの補正を試みないための最重要の契約点なので
    ここで明示的にアサートする。matched=False は fuzzy_key未登録（未知）のときのみ。
    """

    def test_exact_match_is_matched_true(self):
        _, matched = canonicalize_card_name("青眼の白龍", {"青眼の白龍"}, {})
        assert matched is True

    def test_unique_fuzzy_match_is_matched_true(self):
        key = fuzzy_key("ブラック・マジシャン")
        _, matched = canonicalize_card_name(
            "ブラックマジシャン", {"ブラック・マジシャン"}, {key: ["ブラック・マジシャン"]}
        )
        assert matched is True

    def test_fuzzy_collision_is_matched_true_despite_no_correction(self):
        # 衝突時は補正されない（resolved==入力）が、matched=Trueなので
        # 呼び出し側は読み仮名フォールバック等を試みてはならない
        resolved, matched = canonicalize_card_name(
            "EHERO", {"E・HERO", "E-HERO"}, {"ehero": ["E・HERO", "E-HERO"]}
        )
        assert resolved == "EHERO"
        assert matched is True

    def test_unknown_is_matched_false(self):
        _, matched = canonicalize_card_name("未知のカードXYZ", {"青眼の白龍"}, {})
        assert matched is False
