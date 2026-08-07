"""
tests/test_wish_prices.py — /api/wish-prices の±%表示の共通店舗比較テスト（監査F5）

背景:
  従来は「8日窓の最古日vs最新日・全店舗min」で±%を計算していたため、最古日にしか
  記録の無い店舗の安値が base_7d に混入し、実測で drop信号の16%が共通店舗で再計算
  すると消える偽シグナルだった（監査F5）。aggregations.common_shop_price_change 経由の
  共通店舗比較に置き換えた（docs/audit-price-logic-2026-08-06.md F5、docs/decisions.md）。

テスト方針: ネットワーク不使用。fake Supabase クライアント（tests/test_shipping.py と
  同じ最小スタブ）で /api/wish-prices を叩き、diff/pct が共通店舗比較の結果になる
  ことを確認する。latest は従来どおり最新観測日の全店舗min（現在価格）のまま変えない。
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeQuery:
    """price_history の select/in_/gte/order/range をたどるだけの最小スタブ"""

    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        self._slice = (start, end)
        return self

    def execute(self):
        start, end = getattr(self, "_slice", (0, 999))
        return SimpleNamespace(data=self._rows[start:end + 1])


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


def row(card, shop, price, date, rarity="ノーマル"):
    return {"card_name": card, "shop": shop, "rarity": rarity,
            "min_price": price, "recorded_at": date}


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    def _make(rows, card_names):
        monkeypatch.setattr(app_module, "_supabase_client", _FakeClient(rows))
        monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
        monkeypatch.setattr(app_module, "_correct_cardname", lambda n: n)
        monkeypatch.setattr(app_module, "_ygores_image_url", lambda n: None)
        # _estimate_cache に載っているカードだけが8日窓クエリの対象（have_data）になる。
        # ここではフォールバック(30日窓)経路を通したくないので直接ロード済み扱いにする
        monkeypatch.setattr(app_module, "_estimate_cache", {n: {} for n in card_names})
        monkeypatch.setattr(app_module, "_estimate_cache_time", time.time())
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client()

    return _make


def _by_name(payload):
    return {c["name"]: c for c in payload["cards"]}


OLD, NEW = "2026-07-30", "2026-08-06"


class TestCommonShopComparisonUsedForDiff:
    def test_two_common_shops_gives_diff_and_pct(self, client):
        rows = [
            row("テストカードA", "店舗A", 1000, OLD),
            row("テストカードA", "店舗A", 900, NEW),
            row("テストカードA", "店舗B", 1100, OLD),
            row("テストカードA", "店舗B", 950, NEW),
        ]
        c = client(rows, ["テストカードA"])
        res = c.post("/api/wish-prices", json={"cards": ["テストカードA"]})
        info = _by_name(res.get_json())["テストカードA"]
        assert info["status"] == "ready"
        assert info["latest"] == 900  # 現在価格は最新日の全店舗min（変更なし）
        assert info["base_7d"] == 1000  # 共通店舗内min
        assert info["diff"] == -100
        assert info["pct"] == -10.0

    def test_orphan_old_date_shop_does_not_produce_false_drop(self, client):
        # 監査F5の再現: 最古日にしか記録の無い店舗の安値(300)が base_7d に混入しない。
        # 共通店舗(店舗A)は横ばいなので、従来ロジックなら出ていた「値下がり」表示が消える
        rows = [
            row("テストカードA", "店舗A", 1000, OLD),
            row("テストカードA", "店舗A", 1000, NEW),
            row("テストカードA", "店舗B", 300, OLD),  # 最新日に記録なし→共通店舗ではない
        ]
        c = client(rows, ["テストカードA"])
        res = c.post("/api/wish-prices", json={"cards": ["テストカードA"]})
        info = _by_name(res.get_json())["テストカードA"]
        assert info["latest"] == 1000
        # 共通店舗が1店（店舗Aのみ）のため date_old が見つからず None
        assert info["base_7d"] is None
        assert info["diff"] is None
        assert info["pct"] is None

    def test_common_shops_below_two_returns_none_pct(self, client):
        # 単一日しかデータが無いカードは比較不能 → base_7d/diff/pct はNone、latestのみ返す
        rows = [row("テストカードA", "店舗A", 500, NEW)]
        c = client(rows, ["テストカードA"])
        res = c.post("/api/wish-prices", json={"cards": ["テストカードA"]})
        info = _by_name(res.get_json())["テストカードA"]
        assert info["latest"] == 500
        assert info["base_7d"] is None
        assert info["diff"] is None
        assert info["pct"] is None

    def test_pending_card_without_estimate_cache_entry(self, client):
        c = client([], [])  # estimate_cache に何も無い＝未収集
        res = c.post("/api/wish-prices", json={"cards": ["未収集カード"]})
        info = _by_name(res.get_json())["未収集カード"]
        assert info["status"] == "pending"
        assert info["diff"] is None
        assert info["pct"] is None
