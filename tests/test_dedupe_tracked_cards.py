"""
tests/test_dedupe_tracked_cards.py -- tools/dedupe_tracked_cards.py の単体テスト
（フェーズ3 P1、docs/design-phase3-generation-side.md 「P1」§4 tracked_cardsバックフィル。
 司令塔裁定 2026-07-10: 単独行のリネーム候補もバックフィル対象に含める拡張）

検証方針:
  build_merge_groups の3カテゴリ分類（統合候補/リネーム候補/fuzzy衝突除外）、
  plan_merge の統合計画、apply_merge/apply_rename のDB書き込み呼び出しを
  フェイクのSupabaseクライアントで検証する。ネットワーク・実DBは使用しない。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import dedupe_tracked_cards as ddt


class _FakeTrackedTable:
    """tracked_cards への select/update/delete チェーンを最小限だけ模倣するフェイク。

    state["existing_target_rows"]: {card_name: [row, ...]} — select().eq(name).execute() の返り値
    state["updates"] / state["deletes"]: 呼ばれた内容を記録
    """

    def __init__(self, state: dict):
        self.state = state
        self._mode = None
        self._pending_fields = None
        self._eq_value = None

    def select(self, *a, **k):
        self._mode = "select"
        return self

    def update(self, fields):
        self._mode = "update"
        self._pending_fields = fields
        return self

    def delete(self):
        self._mode = "delete"
        return self

    def eq(self, key, value):
        self._eq_value = value
        return self

    def execute(self):
        if self._mode == "select":
            existing = self.state.get("existing_target_rows", {})
            return SimpleNamespace(data=existing.get(self._eq_value, []))
        if self._mode == "update":
            self.state.setdefault("updates", []).append((self._eq_value, dict(self._pending_fields)))
            return SimpleNamespace(data=[{"card_name": self._eq_value, **self._pending_fields}])
        if self._mode == "delete":
            self.state.setdefault("deletes", []).append(self._eq_value)
            return SimpleNamespace(data=[{"card_name": self._eq_value}])
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self, state: dict):
        self.state = state

    def table(self, name: str):
        assert name == "tracked_cards"
        return _FakeTrackedTable(self.state)


def _cardnames_fixture():
    """3カテゴリ全てを含む cardnames_set / cardnames_fuzzy の固定データ"""
    from name_normalize import fuzzy_key

    cardnames_set = {"ブラック・マジシャン", "スカル・デーモン", "E・HERO", "E-HERO"}
    cardnames_fuzzy = {
        fuzzy_key("ブラック・マジシャン"): ["ブラック・マジシャン"],
        fuzzy_key("スカル・デーモン"): ["スカル・デーモン"],
        # E・HERO と E-HERO は記号除去で同一キーに衝突する実在別カード群を模擬
        "ehero": ["E・HERO", "E-HERO"],
    }
    return cardnames_set, cardnames_fuzzy


class TestBuildMergeGroups:
    def test_three_categories_classified_correctly(self):
        cardnames_set, cardnames_fuzzy = _cardnames_fixture()
        rows = [
            # 統合候補: 表記ゆれ2行が同一正式名に解決される
            {"card_name": "ブラックマジシャン", "active": True, "last_collected_at": "2026-07-01"},
            {"card_name": "ブラック・マジシャン", "active": False, "last_collected_at": "2026-07-05"},
            # リネーム候補: 単独行で、正式名（スカル・デーモン）の行はまだ存在しない
            {"card_name": "スカルデーモン", "active": True, "last_collected_at": "2026-06-01"},
            # fuzzy衝突除外: EHERO は E・HERO/E-HERO のどちらか判別不能
            {"card_name": "EHERO", "active": True, "last_collected_at": "2026-06-02"},
        ]

        dup_groups, rename_candidates, collisions = ddt.build_merge_groups(rows, cardnames_set, cardnames_fuzzy)

        # 統合候補: ブラック・マジシャンの表記ゆれペア
        assert set(dup_groups.keys()) == {"ブラック・マジシャン"}
        assert {r["card_name"] for r in dup_groups["ブラック・マジシャン"]} == {"ブラックマジシャン", "ブラック・マジシャン"}

        # リネーム候補: スカルデーモン -> スカル・デーモン（単独行、正式名の行は未登録）
        assert set(rename_candidates.keys()) == {"スカル・デーモン"}
        assert rename_candidates["スカル・デーモン"]["card_name"] == "スカルデーモン"

        # fuzzy衝突除外
        assert collisions == ["EHERO"]

    def test_singleton_non_canonical_row_is_rename_candidate(self):
        from name_normalize import fuzzy_key
        cardnames_set = {"ブラック・マジシャン"}
        cardnames_fuzzy = {fuzzy_key("ブラック・マジシャン"): ["ブラック・マジシャン"]}
        rows = [
            {"card_name": "ブラックマジシャン", "active": True, "last_collected_at": "2026-07-01"},
        ]

        dup_groups, rename_candidates, collisions = ddt.build_merge_groups(rows, cardnames_set, cardnames_fuzzy)

        assert dup_groups == {}
        assert collisions == []
        assert set(rename_candidates.keys()) == {"ブラック・マジシャン"}
        assert rename_candidates["ブラック・マジシャン"]["card_name"] == "ブラックマジシャン"

    def test_already_canonical_singleton_is_not_a_rename_candidate(self):
        from name_normalize import fuzzy_key
        cardnames_set = {"青眼の白龍"}
        cardnames_fuzzy = {fuzzy_key("青眼の白龍"): ["青眼の白龍"]}
        rows = [{"card_name": "青眼の白龍", "active": True, "last_collected_at": "2026-07-02"}]

        dup_groups, rename_candidates, collisions = ddt.build_merge_groups(rows, cardnames_set, cardnames_fuzzy)

        assert dup_groups == {}
        assert rename_candidates == {}
        assert collisions == []

    def test_unknown_singleton_is_not_a_rename_candidate(self):
        # cardnames_ja.json に載っていない未知カードは canonicalize が入力のまま返すため対象外
        rows = [{"card_name": "未知のカードXYZ", "active": True, "last_collected_at": None}]
        dup_groups, rename_candidates, collisions = ddt.build_merge_groups(rows, {"青眼の白龍"}, {})

        assert dup_groups == {}
        assert rename_candidates == {}
        assert collisions == []


class TestPlanMerge:
    def test_keep_existing_canonical_row_and_merge_others(self):
        rows = [
            {"card_name": "ブラックマジシャン", "active": True, "last_collected_at": "2026-07-01"},
            {"card_name": "ブラック・マジシャン", "active": False, "last_collected_at": "2026-07-05"},
        ]
        keep_row, others, merged_active, merged_last = ddt.plan_merge("ブラック・マジシャン", rows)
        assert keep_row["card_name"] == "ブラック・マジシャン"
        assert [o["card_name"] for o in others] == ["ブラックマジシャン"]
        assert merged_active is True  # OR
        assert merged_last == "2026-07-05"  # MAX

    def test_no_canonical_row_present_renames_first_by_name(self):
        rows = [
            {"card_name": "ブラックマジシャンB", "active": False, "last_collected_at": "2026-07-01"},
            {"card_name": "ブラックマジシャンA", "active": True, "last_collected_at": None},
        ]
        keep_row, others, merged_active, merged_last = ddt.plan_merge("ブラック・マジシャン", rows)
        assert keep_row["card_name"] == "ブラックマジシャンA"  # card_name昇順の先頭
        assert [o["card_name"] for o in others] == ["ブラックマジシャンB"]
        assert merged_active is True
        assert merged_last == "2026-07-01"


class TestApplyMerge:
    def test_renames_and_deletes_others(self):
        state = {}
        sb = _FakeSupabase(state)
        rows = [
            {"card_name": "ブラックマジシャンA", "active": False, "last_collected_at": None},
            {"card_name": "ブラックマジシャンB", "active": True, "last_collected_at": "2026-07-01"},
        ]
        keep_row, others, merged_active, merged_last = ddt.plan_merge("ブラック・マジシャン", rows)
        ddt.apply_merge(sb, "ブラック・マジシャン", keep_row, others, merged_active, merged_last)

        # keep_row（ブラックマジシャンA）が正式名へ改名され、active/last_collected_atも更新される
        assert state["updates"] == [
            ("ブラックマジシャンA", {"card_name": "ブラック・マジシャン", "active": True, "last_collected_at": "2026-07-01"})
        ]
        # 他行は削除される
        assert state["deletes"] == ["ブラックマジシャンB"]

    def test_no_update_call_when_keep_row_already_matches(self):
        # 残す行が既に正式名でactive/last_collected_atも変化なしならUPDATEを呼ばない
        state = {}
        sb = _FakeSupabase(state)
        rows = [
            {"card_name": "ブラック・マジシャン", "active": True, "last_collected_at": "2026-07-05"},
            {"card_name": "ブラックマジシャン", "active": True, "last_collected_at": "2026-07-01"},
        ]
        keep_row, others, merged_active, merged_last = ddt.plan_merge("ブラック・マジシャン", rows)
        ddt.apply_merge(sb, "ブラック・マジシャン", keep_row, others, merged_active, merged_last)

        assert state.get("updates", []) == []
        assert state["deletes"] == ["ブラックマジシャン"]

    def test_falls_back_to_merge_when_rename_target_already_exists(self):
        # レース: 事前計画時点では存在しなかった改名先(正式名)の行が、apply時点では
        # 並行書き込み（app側の _track_card_async 等 or 本スクリプトの別処理）で
        # 既に存在しているケース。改名せず、既存行をkeepとして両方の非正式名行を削除する。
        existing_target_row = {"card_name": "ブラック・マジシャン", "active": False, "last_collected_at": "2026-07-04"}
        state = {"existing_target_rows": {"ブラック・マジシャン": [existing_target_row]}}
        sb = _FakeSupabase(state)
        rows = [
            {"card_name": "ブラックマジシャンA", "active": True, "last_collected_at": "2026-07-01"},
            {"card_name": "ブラックマジシャンB", "active": False, "last_collected_at": None},
        ]
        keep_row, others, merged_active, merged_last = ddt.plan_merge("ブラック・マジシャン", rows)
        # 事前計画では正式名の行がrows内に無いため、card_name昇順の先頭が改名予定のkeepになる
        assert keep_row["card_name"] == "ブラックマジシャンA"

        ddt.apply_merge(sb, "ブラック・マジシャン", keep_row, others, merged_active, merged_last)

        # 改名は発生せず、既存の正式名行をkeepとして両方の非正式名行が削除される
        assert state["updates"] == [("ブラック・マジシャン", {"active": True})]
        assert set(state["deletes"]) == {"ブラックマジシャンA", "ブラックマジシャンB"}

class TestApplyRename:
    def test_renames_when_target_does_not_exist(self):
        state = {"existing_target_rows": {}}
        sb = _FakeSupabase(state)
        row = {"card_name": "ブラックマジシャン", "active": True, "last_collected_at": "2026-07-01"}

        result = ddt.apply_rename(sb, "ブラック・マジシャン", row)

        assert result == "renamed"
        assert state["updates"] == [("ブラックマジシャン", {"card_name": "ブラック・マジシャン"})]
        assert state.get("deletes", []) == []

    def test_falls_back_to_merge_when_target_already_exists(self):
        # ドライラン後の並行書き込み等で改名先の正式名行が既に存在する（レース）ケース
        existing_target_row = {"card_name": "ブラック・マジシャン", "active": False, "last_collected_at": "2026-07-03"}
        state = {"existing_target_rows": {"ブラック・マジシャン": [existing_target_row]}}
        sb = _FakeSupabase(state)
        row = {"card_name": "ブラックマジシャン", "active": True, "last_collected_at": "2026-07-01"}

        result = ddt.apply_rename(sb, "ブラック・マジシャン", row)

        assert result == "merged"
        # keep_row = 既存の正式名行（ブラック・マジシャン）、others = ブラックマジシャン(削除対象)
        # active は OR で True に更新され、last_collected_at は MAX(2026-07-01, 2026-07-03)=07-03のまま変化なし
        assert state["updates"] == [("ブラック・マジシャン", {"active": True})]
        assert state["deletes"] == ["ブラックマジシャン"]
