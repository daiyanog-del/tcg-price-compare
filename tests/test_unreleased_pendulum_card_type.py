"""1回目の種別から「ペンデュラム」が抜けたとき、スケールがあれば種別に補う正規化のテスト。

評価（発売済み28枚）で 5/28 がエクシーズ・儀式・融合のペンデュラムを「エクシーズモンスター」等と返した。
補った後は自動承認の pendulum_type_mismatch で手動に回らない。
"""
import pytest

import unreleased_auto_approve as aa
import unreleased_extractor as ex


@pytest.mark.parametrize("before, after", [
    ("エクシーズモンスター", "エクシーズ・ペンデュラムモンスター"),
    ("儀式モンスター", "儀式・ペンデュラムモンスター"),
    ("融合モンスター", "融合・ペンデュラムモンスター"),
    ("シンクロモンスター", "シンクロ・ペンデュラムモンスター"),
    ("エクシーズ・効果モンスター", "エクシーズ・ペンデュラム・効果モンスター"),
    ("効果モンスター", "ペンデュラム・効果モンスター"),
    ("通常モンスター", "ペンデュラム・通常モンスター"),
    ("チューナー・効果モンスター", "ペンデュラム・チューナー・効果モンスター"),
    ("モンスター", "ペンデュラムモンスター"),
])
def test_add_pendulum_to_card_type(before, after):
    assert ex.add_pendulum_to_card_type(before) == after


@pytest.mark.parametrize("card_type", [
    "融合ペンデュラムモンスター",  # 既にある（本番の承認済みの表記）
    "ペンデュラム・効果モンスター",
    "リンクモンスター",  # ペンデュラムのリンクは無い
    "通常魔法", "永続罠", "",
    "効果モンスター/チューナー",  # 形が読めない表記は触らない
])
def test_add_pendulum_keeps_other_types(card_type):
    assert ex.add_pendulum_to_card_type(card_type) == card_type


def _card(**over):
    ExtractedCard, _ = ex._get_pydantic_models()
    base = {"name": "テスト", "card_type": "エクシーズモンスター", "rank": 4, "pendulum_scale": 3,
            "pendulum_effect": "①：P効果。", "effect_text": "①：効果。", "confidence": "high"}
    base.update(over)
    return ExtractedCard(**base)


def test_validate_and_fix_adds_pendulum_when_scale_present():
    row = ex._validate_and_fix(_card(), "https://example.com/")
    assert row["card_type"] == "エクシーズ・ペンデュラムモンスター"
    assert not aa.pendulum_type_mismatch(row)


def test_validate_and_fix_scale_zero_counts_as_pendulum():
    row = ex._validate_and_fix(_card(card_type="儀式モンスター", pendulum_scale=0), "https://example.com/")
    assert row["card_type"] == "儀式・ペンデュラムモンスター"


def test_validate_and_fix_without_scale_keeps_type():
    # スケールが無くP効果だけのときは補わない（読み違いの恐れ。自動承認は mismatch で止める）
    row = ex._validate_and_fix(_card(pendulum_scale=None), "https://example.com/")
    assert row["card_type"] == "エクシーズモンスター"
    assert aa.pendulum_type_mismatch(row)


def test_extract_cards_records_first_pass_type(monkeypatch):
    _, Result = ex._get_pydantic_models()
    card = {"name": "テスト", "card_type": "エクシーズモンスター", "rank": 4, "pendulum_scale": 3,
            "pendulum_effect": "①：P効果。", "effect_text": "①：効果。", "confidence": "high",
            "image_url": "", "uncertain_fields": []}
    import unreleased_quality as uq
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(uq, "structured_request", lambda *a, **k: ({"cards": [card]}, "{}", {}))
    monkeypatch.setattr(uq, "enrich_rows", lambda rows, **k: rows)
    rows = ex._extract_cards("本文", [], "https://example.com/")
    assert rows[0]["card_type"] == "エクシーズ・ペンデュラムモンスター"
    assert rows[0]["extraction_raw"]["card_type_first_pass"] == "エクシーズモンスター"
