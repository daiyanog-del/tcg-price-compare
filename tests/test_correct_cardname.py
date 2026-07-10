"""
tests/test_correct_cardname.py — app._correct_cardname のfuzzy衝突安全化テスト
（フェーズ3 P6a、docs/design-phase3-generation-side.md 「P6a: _correct_cardname の fuzzy 衝突時 [0] 決め打ち」）

背景:
  _cardnames_fuzzy[q_fuzzy] が複数候補を持つ場合（例: E・HERO と E-HERO のように
  記号除去で同一キーに衝突する実在の別カード）、従来は候補リスト[0]を機械採用しており、
  低頻度だが「別カードへの誤合流」という最悪種の汚染を生んでいた。
  衝突時は補正せず入力をそのまま返し、WARNログで衝突を記録する。

テスト方針:
  app モジュールのグローバル辞書 (_cardnames_set / _cardnames_fuzzy / _cardnames_reading /
  _cardnames_reading_fuzzy) を直接差し替えてから _correct_cardname を呼ぶ。
  ネットワーク・Supabase・cardnames_ja.json の実ファイルは一切使わない。
"""

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import app as app_module
from name_normalize import fuzzy_key as _fuzzy_key


def _patch_cardnames(monkeypatch, fuzzy: dict, exact: set | None = None,
                      reading: dict | None = None, reading_fuzzy: dict | None = None):
    monkeypatch.setattr(app_module, "_cardnames_set", exact or set(), raising=False)
    monkeypatch.setattr(app_module, "_cardnames_fuzzy", fuzzy, raising=False)
    monkeypatch.setattr(app_module, "_cardnames_reading", reading or {}, raising=False)
    monkeypatch.setattr(app_module, "_cardnames_reading_fuzzy", reading_fuzzy or {}, raising=False)


class TestNoCollision:
    """衝突なし（候補1件）の場合は従来どおり補正される"""

    def test_single_candidate_is_corrected(self, monkeypatch):
        key = _fuzzy_key("ブラック・マジシャン")  # 中黒を除去したキー（"ブラックマジシャン"と一致）
        _patch_cardnames(
            monkeypatch,
            fuzzy={key: ["ブラック・マジシャン"]},
            exact={"ブラック・マジシャン"},
        )
        result = app_module._correct_cardname("ブラックマジシャン")
        assert result == "ブラック・マジシャン"

    def test_exact_match_passthrough(self, monkeypatch):
        # 完全一致は fuzzy 経路を通らずそのまま返る（従来挙動の維持確認）
        _patch_cardnames(
            monkeypatch,
            fuzzy={},
            exact={"青眼の白龍"},
        )
        result = app_module._correct_cardname("青眼の白龍")
        assert result == "青眼の白龍"


class TestFuzzyCollision:
    """同一fuzzy_keyに複数の正式名が衝突する場合は補正しない＋warningを出す"""

    def test_collision_returns_input_unchanged(self, monkeypatch):
        # E・HERO と E-HERO のように記号除去で同一キーに衝突する実在別カード群を模擬
        _patch_cardnames(
            monkeypatch,
            fuzzy={"ehero": ["E・HERO", "E-HERO"]},
            exact={"E・HERO", "E-HERO"},
        )
        result = app_module._correct_cardname("EHERO")
        # 衝突キーでしか一致しない入力はそのまま返る（誤ったカードへの合流を避ける）
        assert result == "EHERO"

    def test_collision_logs_warning(self, monkeypatch, caplog):
        _patch_cardnames(
            monkeypatch,
            fuzzy={"ehero": ["E・HERO", "E-HERO"]},
            exact={"E・HERO", "E-HERO"},
        )
        with caplog.at_level(logging.WARNING, logger="app"):
            app_module._correct_cardname("EHERO")
        assert any("fuzzy_key衝突" in rec.message for rec in caplog.records)
