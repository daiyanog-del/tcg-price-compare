"""
tests/test_x_poster_rpc.py — x_poster._rpc_rows_to_card_dates（RPC行→card_dates変換）の単体テスト

背景:
  post_big_movers の集計を Python 集計（旧 get_price_movers_by_rarity）から
  Supabase RPC `get_price_movers`（店舗粒度のガード①②適用済み）呼び出しに一本化した。
  RPC の戻り行を select_big_movers が受け取る card_dates 形式
  {(name, rarity): {date_str: price}} に変換する _rpc_rows_to_card_dates を対象にテストする。

  post_big_movers は per_card=999 でRPCを呼ぶため、同一カード名で複数レアリティの
  行が返りうる（RPC側で pct 最大の1レアリティに事前に畳み込むと、BIGMOVE閾値が
  diff にも依存するために取りこぼしが生じるため）。変換後も全レアリティが
  card_dates に個別キーとして保持されることを確認する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from x_poster import _rpc_rows_to_card_dates, select_big_movers

OLD, NEW = "2026-07-09", "2026-07-10"


def rpc_row(name, rarity, prev_price, today_price, date_old=OLD, date_new=NEW, direction="up"):
    """get_price_movers RPC の戻り行を模したdict"""
    return {
        "card_name": name, "rarity": rarity,
        "today_price": today_price, "prev_price": prev_price,
        "diff": today_price - prev_price,
        "pct": round((today_price - prev_price) / prev_price * 100, 1),
        "date_new": date_new, "date_old": date_old,
        "direction": direction,
    }


class TestRpcRowsToCardDates:
    def test_empty_rows_returns_empty(self):
        card_dates, date_old, date_new = _rpc_rows_to_card_dates([])
        assert card_dates == {}
        assert date_old is None
        assert date_new is None

    def test_single_row_converted(self):
        rows = [rpc_row("ブラックマジシャン", "プリシク", 880, 5180)]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert date_old == OLD
        assert date_new == NEW
        assert card_dates[("ブラックマジシャン", "プリシク")] == {OLD: 880, NEW: 5180}

    def test_multiple_rows_keyed_by_name_rarity(self):
        rows = [
            rpc_row("カードA", "ノーマル", 1000, 3000),
            rpc_row("カードB", "シク", 2000, 1000, direction="down"),
        ]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert len(card_dates) == 2
        assert card_dates[("カードA", "ノーマル")] == {OLD: 1000, NEW: 3000}
        assert card_dates[("カードB", "シク")] == {OLD: 2000, NEW: 1000}

    def test_missing_keys_default_to_zero_and_empty_string(self):
        # RPC側で予期せず欠損した場合の防御的デフォルト（0円・空文字）
        rows = [{"card_name": "カードX", "date_new": NEW, "date_old": OLD}]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert card_dates[("カードX", "")] == {OLD: 0, NEW: 0}

    def test_same_card_multiple_rarities_all_kept(self):
        # per_card=999 呼び出しにより同名カードの複数レアリティ行が返る想定。
        # (name, rarity) キーで別エントリとして全レアリティが保持されること
        rows = [
            rpc_row("青眼の白龍", "ノーマル", 1000, 3000),
            rpc_row("青眼の白龍", "シークレット", 2000, 5000),
            rpc_row("青眼の白龍", "20thシークレット", 8000, 12000),
        ]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        assert len(card_dates) == 3
        assert card_dates[("青眼の白龍", "ノーマル")] == {OLD: 1000, NEW: 3000}
        assert card_dates[("青眼の白龍", "シークレット")] == {OLD: 2000, NEW: 5000}
        assert card_dates[("青眼の白龍", "20thシークレット")] == {OLD: 8000, NEW: 12000}


class TestRpcConversionFeedsSelectBigMovers(object):
    """変換結果が select_big_movers にそのまま食わせられることを確認する統合的な単体テスト"""

    def test_converted_rows_detected_as_bigmove_candidate(self):
        # RPC min_diff=50 は通過済みだが BIGMOVE 閾値(+2000円/+100%)も満たすケース
        rows = [rpc_row("急騰カード", "ノーマル", 880, 5180)]
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        result = select_big_movers(card_dates, date_old, date_new, top_n=3)
        assert len(result) == 1
        assert result[0]["name"] == "急騰カード"
        assert result[0]["direction"] == "up"

    def test_converted_rows_below_bigmove_threshold_excluded(self):
        # RPCのmin_diff=50は満たすが、BIGMOVE閾値(+2000円/+100%)には届かないケース
        rows = [rpc_row("小変動カード", "ノーマル", 1000, 1100)]  # diff=+100, pct=+10%
        card_dates, date_old, date_new = _rpc_rows_to_card_dates(rows)
        result = select_big_movers(card_dates, date_old, date_new, top_n=3)
        assert result == []
