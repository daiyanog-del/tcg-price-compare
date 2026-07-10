"""
tests/test_big_movers.py — select_big_movers（大変動カード単発投稿の候補抽出）の単体テスト

背景:
  x_poster.py の日次投稿を「日次ランキング垂れ流し」から「大変動カードの単発投稿」へ
  切り替えるにあたり、候補抽出ロジックを純関数 select_big_movers に切り出した
  （ネットワーク・Supabaseクライアント不要）。docs/decisions.md 2026-07-10 参照。

テスト方針:
  - card_dates: {(name, rarity): {date_str: price}} を手組みして渡すだけ。
  - 非対称閾値（up +2000円/+100%、down -2000円/-50%）の境界判定
  - 方向別トップ1選抜（変動率絶対値順）
  - 正規化名（全角半角ゆれ）での重複排除
  - 該当なしのとき空リスト
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from x_poster import (
    select_big_movers,
    BIGMOVE_UP_MIN_DIFF, BIGMOVE_UP_MIN_PCT,
    BIGMOVE_DOWN_MIN_DIFF, BIGMOVE_DOWN_MIN_PCT,
)

OLD, NEW = "2026-07-09", "2026-07-10"


def make_card_dates(entries):
    """entries: [(name, rarity, yesterday_price, today_price), ...] から card_dates を組み立てる"""
    card_dates = {}
    for name, rarity, y, t in entries:
        card_dates[(name, rarity)] = {OLD: y, NEW: t}
    return card_dates


# ──────────────────────────────────────────────
# 非対称閾値の境界判定
# ──────────────────────────────────────────────

class TestUpThreshold:
    """急騰: diff >= +2000円 かつ pct >= +100%（両方満たして初めて採用）"""

    def test_exact_boundary_is_included(self):
        # yesterday=2000, today=4000 -> diff=+2000, pct=+100.0 ちょうど境界
        card_dates = make_card_dates([("境界カード", "ノーマル", 2000, 4000)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert len(result) == 1
        assert result[0]["direction"] == "up"
        assert result[0]["diff"] == BIGMOVE_UP_MIN_DIFF
        assert result[0]["pct"] == BIGMOVE_UP_MIN_PCT

    def test_diff_met_but_pct_not_met_excluded(self):
        # yesterday=100000, today=102500 -> diff=+2500(>=2000), pct=+2.5%(<100%) は対象外
        card_dates = make_card_dates([("高額カード", "ノーマル", 100000, 102500)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert result == []

    def test_pct_met_but_diff_not_met_excluded(self):
        # yesterday=1000, today=2500 -> diff=+1500(<2000), pct=+150%(>=100%) は対象外
        card_dates = make_card_dates([("安カード", "ノーマル", 1000, 2500)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert result == []

    def test_both_met_included(self):
        # yesterday=880, today=5180 -> diff=+4300, pct=+488.6%
        card_dates = make_card_dates([("ブラックマジシャン", "プリシク", 880, 5180)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert len(result) == 1
        assert result[0]["name"] == "ブラックマジシャン"
        assert result[0]["direction"] == "up"


class TestDownThreshold:
    """急落: diff <= -2000円 かつ pct <= -50%（両方満たして初めて採用）"""

    def test_exact_boundary_is_included(self):
        # yesterday=4000, today=2000 -> diff=-2000, pct=-50.0 ちょうど境界
        card_dates = make_card_dates([("境界カード", "ノーマル", 4000, 2000)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert len(result) == 1
        assert result[0]["direction"] == "down"
        assert result[0]["diff"] == BIGMOVE_DOWN_MIN_DIFF
        assert result[0]["pct"] == BIGMOVE_DOWN_MIN_PCT

    def test_diff_met_but_pct_not_met_excluded(self):
        # yesterday=100000, today=97500 -> diff=-2500(<=-2000), pct=-2.5%(>-50%) は対象外
        card_dates = make_card_dates([("高額カード", "ノーマル", 100000, 97500)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert result == []

    def test_pct_met_but_diff_not_met_excluded(self):
        # yesterday=1000, today=400 -> diff=-600(>-2000), pct=-60%(<=-50%) は対象外
        card_dates = make_card_dates([("安カード", "ノーマル", 1000, 400)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert result == []

    def test_both_met_included(self):
        # yesterday=8000, today=1500 -> diff=-6500, pct=-81.3%
        card_dates = make_card_dates([("暴落カード", "シク", 8000, 1500)])
        result = select_big_movers(card_dates, OLD, NEW)
        assert len(result) == 1
        assert result[0]["name"] == "暴落カード"
        assert result[0]["direction"] == "down"


# ──────────────────────────────────────────────
# 方向別トップ1選抜
# ──────────────────────────────────────────────

class TestTopOnePerDirection:
    def test_up_picks_highest_pct_only(self):
        card_dates = make_card_dates([
            ("カードA", "ノーマル", 1000, 3000),   # pct=+200%
            ("カードB", "ノーマル", 1000, 6000),   # pct=+500% (最大)
            ("カードC", "ノーマル", 1000, 2100),   # pct=+110%
        ])
        result = select_big_movers(card_dates, OLD, NEW)
        up_results = [r for r in result if r["direction"] == "up"]
        assert len(up_results) == 1
        assert up_results[0]["name"] == "カードB"

    def test_down_picks_highest_abs_pct_only(self):
        card_dates = make_card_dates([
            ("カードD", "ノーマル", 4000, 2000),   # pct=-50%
            ("カードE", "ノーマル", 4000, 400),    # pct=-90% (最大絶対値)
            ("カードF", "ノーマル", 4000, 1900),   # pct=-52.5%
        ])
        result = select_big_movers(card_dates, OLD, NEW)
        down_results = [r for r in result if r["direction"] == "down"]
        assert len(down_results) == 1
        assert down_results[0]["name"] == "カードE"

    def test_both_directions_can_coexist(self):
        card_dates = make_card_dates([
            ("急騰カード", "ノーマル", 1000, 5000),  # pct=+400%
            ("急落カード", "ノーマル", 5000, 1000),  # pct=-80%
        ])
        result = select_big_movers(card_dates, OLD, NEW)
        assert len(result) == 2
        directions = {r["direction"] for r in result}
        assert directions == {"up", "down"}

    def test_top_n_returns_ranked_candidates_for_promotion(self):
        # クールダウン時の次点繰り上げ用: top_n=3 で方向ごとに変動率順の複数候補を返す
        card_dates = make_card_dates([
            ("カードA", "ノーマル", 1000, 3000),   # pct=+200%
            ("カードB", "ノーマル", 1000, 6000),   # pct=+500% (1位)
            ("カードC", "ノーマル", 1000, 4000),   # pct=+300% (2位)
            ("カードG", "ノーマル", 1000, 2100),   # pct=+110% (4位 → top_n=3で落ちる)
        ])
        result = select_big_movers(card_dates, OLD, NEW, top_n=3)
        up_results = [r for r in result if r["direction"] == "up"]
        assert [r["name"] for r in up_results] == ["カードB", "カードC", "カードA"]

    def test_top_n_default_is_one(self):
        card_dates = make_card_dates([
            ("カードA", "ノーマル", 1000, 3000),
            ("カードB", "ノーマル", 1000, 6000),
        ])
        result = select_big_movers(card_dates, OLD, NEW)
        assert len([r for r in result if r["direction"] == "up"]) == 1


# ──────────────────────────────────────────────
# 正規化名での重複排除
# ──────────────────────────────────────────────

class TestNormalizedNameDedup:
    def test_zenkaku_hankaku_variant_deduped(self):
        # 「一撃必殺！居合いドロー」(全角！) と「一撃必殺!居合いドロー」(半角!) は
        # fuzzy_key で同一視され、変動率絶対値が大きい方のみ残る
        card_dates = make_card_dates([
            ("一撃必殺！居合いドロー", "ノーマル", 1000, 5000),  # pct=+400%
            ("一撃必殺!居合いドロー", "ノーマル", 1000, 6000),   # pct=+500% (こちらが残るはず)
        ])
        result = select_big_movers(card_dates, OLD, NEW)
        up_results = [r for r in result if r["direction"] == "up"]
        assert len(up_results) == 1
        assert up_results[0]["today"] == 6000

    def test_different_rarity_not_deduped(self):
        # 同名でもレアリティが異なれば別候補として扱う（集計単位が (name, rarity) のため）
        card_dates = make_card_dates([
            ("青眼の白龍", "ノーマル", 1000, 5000),  # pct=+400%
            ("青眼の白龍", "シークレット", 2000, 4200),  # pct=+110%
        ])
        result = select_big_movers(card_dates, OLD, NEW)
        # 両方とも up 方向だが、集計単位が異なるキーのため dedup 前は2候補存在し、
        # 最終的には方向別トップ1に絞られ「ノーマル」(pct最大)のみ残る
        up_results = [r for r in result if r["direction"] == "up"]
        assert len(up_results) == 1
        assert up_results[0]["rarity"] == "ノーマル"


# ──────────────────────────────────────────────
# 該当なしのケース
# ──────────────────────────────────────────────

class TestNoCandidates:
    def test_empty_card_dates(self):
        assert select_big_movers({}, OLD, NEW) == []

    def test_no_card_meets_threshold(self):
        card_dates = make_card_dates([
            ("平常カード1", "ノーマル", 1000, 1050),   # +5%
            ("平常カード2", "ノーマル", 2000, 1900),   # -5%
        ])
        assert select_big_movers(card_dates, OLD, NEW) == []

    def test_missing_one_of_two_dates_excluded(self):
        # 比較対象日の片方しかデータがないカードは対象外
        card_dates = {("片方のみ", "ノーマル"): {OLD: 1000}}
        assert select_big_movers(card_dates, OLD, NEW) == []

    def test_auto_date_detection_with_less_than_two_dates(self):
        # date_old/date_new を省略した場合、日付が1日分しかなければ空を返す
        card_dates = {("カード", "ノーマル"): {OLD: 1000}}
        assert select_big_movers(card_dates) == []

    def test_zero_yesterday_price_excluded(self):
        # ゼロ除算防止: yesterday=0 は対象外
        card_dates = make_card_dates([("ゼロ円カード", "ノーマル", 0, 5000)])
        assert select_big_movers(card_dates, OLD, NEW) == []
