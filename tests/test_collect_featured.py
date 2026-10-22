"""
tests/test_collect_featured.py — collect_featured.py の純関数テスト

対象:
  - _is_shop_failed: (card, shop) 1回の取得試行の失敗判定（4分岐）
  - classify_blocklike_shops: 店舗ごとの失敗率によるブロック様判定
  - _group_pairs_by_card: (card, shop) ペアのカード単位グルーピング

ネットワーク・実Supabaseは一切使わない。
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from collect_featured import _is_shop_failed, classify_blocklike_shops, _group_pairs_by_card


# ──────────────────────────────────────────────
# _is_shop_failed（4分岐）
# ──────────────────────────────────────────────

class TestIsShopFailed:
    def test_status_none_is_failed(self):
        # compare_prices が結果を書き込まなかった（想定外）
        assert _is_shop_failed(None) is True

    def test_exception_present_is_failed(self):
        assert _is_shop_failed({"count": 0, "fetch_errors": 0, "exception": "boom"}) is True

    def test_fetch_errors_with_zero_count_is_failed(self):
        assert _is_shop_failed({"count": 0, "fetch_errors": 2}) is True

    def test_count_positive_with_fetch_errors_is_not_failed(self):
        # 一部エラーがあっても結果が得られていれば「取得できた」扱い
        assert _is_shop_failed({"count": 3, "fetch_errors": 1}) is False

    def test_zero_errors_zero_count_is_not_failed(self):
        # 0件かつエラーなし＝在庫なし。取得失敗ではない
        assert _is_shop_failed({"count": 0, "fetch_errors": 0}) is False


# ──────────────────────────────────────────────
# classify_blocklike_shops
# ──────────────────────────────────────────────

class TestClassifyBlocklikeShops:
    def test_shop_at_threshold_is_blocklike(self):
        failed = [("カードA", "カードラボ"), ("カードB", "カードラボ"), ("カードC", "カードラボ")]
        attempted = {"カードラボ": 10}  # 3/10=30% ちょうど閾値
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == {"カードラボ"}

    def test_shop_under_threshold_is_not_blocklike(self):
        failed = [("カードA", "遊々亭")]
        attempted = {"遊々亭": 10}  # 1/10=10%
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == set()

    def test_mixed_shops_classified_independently(self):
        failed = [
            ("カードA", "カードラボ"), ("カードB", "カードラボ"), ("カードC", "カードラボ"),
            ("カードA", "遊々亭"),
        ]
        attempted = {"カードラボ": 10, "遊々亭": 10}
        result = classify_blocklike_shops(failed, attempted, threshold=0.30)
        assert result == {"カードラボ"}

    def test_zero_attempted_shop_is_not_blocklike(self):
        # attempted=0（試行数不明）はゼロ除算を避け、ブロック様と判定しない
        failed = [("カードA", "遊々亭")]
        result = classify_blocklike_shops(failed, {}, threshold=0.30)
        assert result == set()


# ──────────────────────────────────────────────
# _group_pairs_by_card
# ──────────────────────────────────────────────

class TestGroupPairsByCard:
    def test_groups_shops_under_card_preserving_order(self):
        pairs = [("カードA", "遊々亭"), ("カードA", "カードラボ"), ("カードB", "遊々亭")]
        result = _group_pairs_by_card(pairs)
        assert result == {"カードA": ["遊々亭", "カードラボ"], "カードB": ["遊々亭"]}


# ──────────────────────────────────────────────
# トレコロCBの当日取得済みカードを第1パスから外す（2026-10-14）
# ──────────────────────────────────────────────

import collect_featured
from collect_featured import fetch_cards_collected_today


class _FakeQuery:
    """_fetch_all_rows が使う select().eq().eq().order().range().execute() のチェーンを模す"""

    def __init__(self, rows, log):
        self._rows = rows
        self._log = log
        self._range = (0, 999)

    def select(self, *_):
        return self

    def eq(self, col, val):
        self._log.append((col, val))
        return self

    def order(self, *_):
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def execute(self):
        a, b = self._range
        return type("R", (), {"data": self._rows[a:b + 1]})()


class _FakeSb:
    def __init__(self, rows):
        self.rows = rows
        self.log = []

    def table(self, name):
        assert name == "price_history"
        return _FakeQuery(self.rows, self.log)


class TestFetchCardsCollectedToday:
    def test_returns_only_target_cards_with_rows(self):
        sb = _FakeSb([{"card_name": "A"}, {"card_name": "B"}, {"card_name": "Z"}])
        got = fetch_cards_collected_today(sb, ["A", "C", "B"], "2026-10-14", "トレコロCB")
        assert got == {"A", "B"}
        # 店舗と日付で絞っている
        assert ("shop", "トレコロCB") in sb.log and ("recorded_at", "2026-10-14") in sb.log

    def test_paginates_beyond_1000_rows(self):
        rows = [{"card_name": f"C{i}"} for i in range(2500)]
        got = fetch_cards_collected_today(_FakeSb(rows), ["C0", "C1500", "C2499"], "2026-10-14", "トレコロCB")
        assert got == {"C0", "C1500", "C2499"}

    def test_query_failure_falls_back_to_empty_set(self):
        class Boom:
            def table(self, *_):
                raise RuntimeError("db down")
        assert fetch_cards_collected_today(Boom(), ["A"], "2026-10-14", "トレコロCB") == set()


class TestMainSkipsCollectedTorecolo:
    def test_first_pass_excludes_torecolo_only_for_collected_cards(self, monkeypatch):
        calls = []  # (card, shops)

        def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None):
            calls.append((card, list(shops)))
            return 0, []

        monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
        monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
        monkeypatch.setattr(collect_featured, "_resolve_pack", lambda sb, a: {"pack_name": "P"})
        monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: ["A", "B", "C"])
        monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["カードラッシュ", "トレコロCB"])
        monkeypatch.setattr(collect_featured, "fetch_cards_collected_today",
                            lambda sb, names, today, shop: {"A", "B"})
        monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
        monkeypatch.setattr(collect_featured.time, "sleep", lambda *_: None)

        collect_featured.main()

        by_card = dict(calls)
        assert by_card["A"] == ["カードラッシュ"], "取得済みカードはトレコロを外す・他店は変えない"
        assert by_card["B"] == ["カードラッシュ"]
        assert by_card["C"] == ["カードラッシュ", "トレコロCB"], "行が無いカードは従来どおり取る"


class TestBlocklikeMinAttempted:
    def test_small_denominator_is_not_blocklike(self):
        # 1/1 失敗（100%）でも試行数が下限未満ならブロック様にしない
        assert classify_blocklike_shops([("A", "トレコロCB")], {"トレコロCB": 1}) == set()
        assert classify_blocklike_shops([("A", "S")] * 4, {"S": 4}) == set()

    def test_at_min_attempted_is_judged(self):
        from collect_featured import BLOCKLIKE_MIN_ATTEMPTED
        pairs = [(f"C{i}", "S") for i in range(BLOCKLIKE_MIN_ATTEMPTED)]
        assert classify_blocklike_shops(pairs, {"S": BLOCKLIKE_MIN_ATTEMPTED}) == {"S"}


class TestAttemptedDenominatorExcludesSkipped:
    def test_failures_ratio_uses_actually_attempted_cards(self, monkeypatch):
        """トレコロを外した分は分母に入れない: 10枚中6枚取得済み→試行4枚（下限未満）なので
        4枚全失敗でもブロック様（長時間待ち）にならず、即時再試行へ回る"""
        cards = [f"C{i}" for i in range(10)]
        done = set(cards[:6])
        sleeps = []
        retried = []

        def fake_collect(sb, card, today, shops, shop_stats=None, db_guard=None):
            if "トレコロCB" in shops and card not in done:
                retried.append((card, list(shops)))
                return 0, ["トレコロCB"]
            return 0, []

        monkeypatch.setattr(sys, "argv", ["collect_featured.py"])
        monkeypatch.setattr(collect_featured, "get_supabase", lambda: object())
        monkeypatch.setattr(collect_featured, "_resolve_pack", lambda sb, a: {"pack_name": "P"})
        monkeypatch.setattr(collect_featured, "get_featured_cards", lambda sb, p: cards)
        monkeypatch.setattr(collect_featured, "check_shop_availability", lambda shops: ["トレコロCB"])
        monkeypatch.setattr(collect_featured, "fetch_cards_collected_today", lambda *a: done)
        monkeypatch.setattr(collect_featured, "collect_and_save_for_shops", fake_collect)
        monkeypatch.setattr(collect_featured.time, "sleep", lambda s: sleeps.append(s))

        collect_featured.main()

        assert all(s < collect_featured.RETRY_WAIT_SEC - 100 for s in sleeps), "長時間待ちが発生した"
        # 第1パス4件 + 即時再試行ラウンド(最大2回)分
        assert len(retried) >= 8
        assert all(c not in done for c, _ in retried), "取得済みカードは試行されない"
