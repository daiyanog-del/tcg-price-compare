"""
tests/test_collect_prices_canonicalize.py -- collect_prices.py 経路A（sync_*系）の名寄せ検証
（フェーズ3 P1、docs/design-phase3-generation-side.md 「P1: 書き込み時名寄せゼロ」）

背景:
  経路A（collect_prices.py の夜間同期）は従来 normalize_card_name（全半角・ダッシュ統一のみ）
  しか通しておらず、中黒ゆれ等（例:「ブラックマジシャン」「ブラック・マジシャン」）が
  tracked_cards に別カードとして分裂登録されていた。canonicalize_tracked_name を
  registration前に通すことで、表記ゆれ入力が正式名へ解決されて登録されることを確認する。

  ネットワーク・実Supabase・cardnames_ja.json の実ファイルは一切使わない
  （collect_prices の内部インデックスをmonkeypatchで差し替える）。
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import collect_prices as cp


class _FakeTable:
    """sb.table(name).select(...).gte(...).order(...).range(...).execute() 等の
    チェーン呼び出しを最小限だけ模倣するフェイク。呼ばれたメソッドは全部 self を返し、
    execute() だけがテーブル名に応じたデータを返す。
    """

    def __init__(self, name: str, state: dict):
        self.name = name
        self.state = state
        self._pending_upsert_rows = None

    def select(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def range(self, *a, **k):
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=None):
        self._pending_upsert_rows = rows
        return self

    def execute(self):
        if self._pending_upsert_rows is not None:
            rows = self._pending_upsert_rows
            self._pending_upsert_rows = None
            self.state.setdefault("inserted", []).extend(rows)
            return SimpleNamespace(data=rows)
        if self.name == "search_logs":
            return SimpleNamespace(data=self.state.get("search_logs", []))
        if self.name == "deck_search_logs":
            return SimpleNamespace(data=[])
        if self.name == "tracked_cards":
            return SimpleNamespace(data=self.state.get("tracked_cards", []))
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self, state: dict):
        self.state = state

    def table(self, name: str):
        return _FakeTable(name, self.state)


def _patch_cardnames_index(monkeypatch, fuzzy: dict, exact: set):
    monkeypatch.setattr(cp, "_cardnames_set", exact, raising=False)
    monkeypatch.setattr(cp, "_cardnames_fuzzy", fuzzy, raising=False)
    monkeypatch.setattr(cp, "_cardnames_index_loaded", True, raising=False)


class TestCanonicalizeTrackedName:
    """canonicalize_tracked_name 単体（collect_prices 内のラッパー）"""

    def test_resolves_unique_fuzzy_match(self, monkeypatch):
        from name_normalize import fuzzy_key
        key = fuzzy_key("ブラック・マジシャン")
        _patch_cardnames_index(monkeypatch, fuzzy={key: ["ブラック・マジシャン"]}, exact={"ブラック・マジシャン"})
        assert cp.canonicalize_tracked_name("ブラックマジシャン") == "ブラック・マジシャン"

    def test_collision_returns_input_unchanged(self, monkeypatch):
        _patch_cardnames_index(
            monkeypatch, fuzzy={"ehero": ["E・HERO", "E-HERO"]}, exact={"E・HERO", "E-HERO"}
        )
        assert cp.canonicalize_tracked_name("EHERO") == "EHERO"


class TestSyncSearchedCardsCanonicalizes:
    """経路Aの一例（sync_searched_cards）で表記ゆれ名が正式名として登録されること"""

    def test_variant_spelling_is_canonicalized_before_insert(self, monkeypatch):
        from name_normalize import fuzzy_key
        key = fuzzy_key("ブラック・マジシャン")
        _patch_cardnames_index(monkeypatch, fuzzy={key: ["ブラック・マジシャン"]}, exact={"ブラック・マジシャン"})

        # 検索ログに表記ゆれ（中黒なし）が min_count 回以上登録されている想定
        state = {
            "search_logs": [
                {"card_name": "ブラックマジシャン"},
                {"card_name": "ブラックマジシャン"},
            ],
            "tracked_cards": [],  # 既存の tracked_cards は空（新規登録される）
        }
        sb = _FakeSupabase(state)

        result = cp.sync_searched_cards(sb, recent_days=7, min_count=2)

        # 戻り値（hot集合に合流する）も正式名に解決されていること
        assert result == {"ブラック・マジシャン"}
        # tracked_cards への登録も正式名で行われ、表記ゆれ名では登録されないこと
        assert state["inserted"] == [{"card_name": "ブラック・マジシャン", "active": True}]

    def test_transition_period_duplicate_before_backfill_is_expected(self, monkeypatch):
        """過渡期の意図された挙動を固定するテスト。

        tracked_cards に P1適用前の非canonical名（「ブラックマジシャン」）が既に
        登録済みの状態で、経路Aが同じ検索ログを再処理すると、existing チェックは
        非canonical名の集合に対して行われるため canonical名「ブラック・マジシャン」は
        「新規」と判定され、二重登録が発生する。これはバグではなく意図された過渡的挙動で、
        tools/dedupe_tracked_cards.py の一回限りバックフィルで解消される設計
        （docs/design-phase3-generation-side.md 「A1」参照）。
        """
        from name_normalize import fuzzy_key
        key = fuzzy_key("ブラック・マジシャン")
        _patch_cardnames_index(monkeypatch, fuzzy={key: ["ブラック・マジシャン"]}, exact={"ブラック・マジシャン"})

        state = {
            "search_logs": [
                {"card_name": "ブラックマジシャン"},
                {"card_name": "ブラックマジシャン"},
            ],
            # 既存 tracked_cards には P1適用前の非canonical名の行が既に存在する想定
            "tracked_cards": [{"card_name": "ブラックマジシャン"}],
        }
        sb = _FakeSupabase(state)

        result = cp.sync_searched_cards(sb, recent_days=7, min_count=2)

        # 戻り値（hot集合）は正式名に解決される
        assert result == {"ブラック・マジシャン"}
        # existing（非canonical名の集合）には正式名が含まれないため「新規」と判定され、
        # 正式名の行が新規insertされる = 過渡的に2行（非canonical・canonical）が並存する
        assert state["inserted"] == [{"card_name": "ブラック・マジシャン", "active": True}]

    def test_fuzzy_collision_registers_input_unchanged_with_warning(self, monkeypatch, capsys):
        _patch_cardnames_index(
            monkeypatch, fuzzy={"ehero": ["E・HERO", "E-HERO"]}, exact={"E・HERO", "E-HERO"}
        )
        state = {
            "search_logs": [
                {"card_name": "EHERO"},
                {"card_name": "EHERO"},
            ],
            "tracked_cards": [],
        }
        sb = _FakeSupabase(state)

        result = cp.sync_searched_cards(sb, recent_days=7, min_count=2)

        # 衝突キーは誤合流を避けるため補正されず、入力のまま登録される
        assert result == {"EHERO"}
        assert state["inserted"] == [{"card_name": "EHERO", "active": True}]
        # 衝突の警告が出力されていること（collect_prices.py は logging ではなく print を使う）
        captured = capsys.readouterr()
        assert "fuzzy_key衝突" in captured.out
