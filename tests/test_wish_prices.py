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


# ── 2026-10-22 読み込みの失敗を「値段なし」と区別する ──

class _FailingQuery(_FakeQuery):
    """execute のたびに errors から1つ取り出して投げる（空になったら rows を返す）。in_ の名前を記録する"""

    def __init__(self, rows, errors, calls):
        super().__init__(rows)
        self._errors = errors
        self._calls = calls

    def in_(self, _col, names):
        self._names = list(names)
        return self

    def execute(self):
        self._calls.append(self._names)
        if self._errors:
            raise self._errors.pop(0)
        rows = [r for r in self._rows if r["card_name"] in self._names]
        start, end = getattr(self, "_slice", (0, 999))
        return SimpleNamespace(data=rows[start:end + 1])


class _FailingClient:
    def __init__(self, rows, errors):
        self._rows = rows
        self.errors = errors
        self.calls = []

    def table(self, _name):
        return _FailingQuery(self._rows, self.errors, self.calls)


@pytest.fixture
def failing_client(monkeypatch):
    import app as app_module

    def _make(rows, errors, card_names, cached=None):
        fake = _FailingClient(rows, errors)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
        monkeypatch.setattr(app_module, "_correct_cardname", lambda n: n)
        monkeypatch.setattr(app_module, "_ygores_image_url", lambda n: None)
        cached = card_names if cached is None else cached
        monkeypatch.setattr(app_module, "_estimate_cache", {n: {} for n in cached})
        monkeypatch.setattr(app_module, "_estimate_cache_time", time.time())
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client(), fake

    return _make


class TestReadFailureIsNotNoPrice:
    def test_db_error_returns_error_status_not_ready(self, failing_client):
        # statement timeout などの失敗は再試行せず、status "error"（以前は "ready"・latest None＝「価格が見つかりませんでした」）
        c, fake = failing_client([], [RuntimeError("canceling statement due to statement timeout")], ["カードA", "カードB"])
        res = c.post("/api/wish-prices", json={"cards": ["カードA", "カードB"]})
        cards = _by_name(res.get_json())
        assert cards["カードA"]["status"] == "error"
        assert cards["カードB"]["status"] == "error"
        assert cards["カードA"]["latest"] is None
        assert len(fake.calls) == 1, "重い問い合わせの失敗は再試行しない"

    def test_no_price_rows_stays_ready_with_null(self, failing_client):
        # 読めたが行が無い＝値段なし（従来どおり ready・latest None）。失敗とは別の結果になる
        c, _ = failing_client([], [], ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "ready"
        assert info["latest"] is None

    def test_connection_terminated_is_retried_once(self, failing_client):
        import httpx
        rows = [row("カードA", "店舗A", 500, NEW)]
        c, fake = failing_client(rows, [httpx.RemoteProtocolError("<ConnectionTerminated error_code:0>")], ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "ready"
        assert info["latest"] == 500
        assert len(fake.calls) == 2

    def test_connection_error_twice_gives_error(self, failing_client):
        import httpx
        errs = [httpx.RemoteProtocolError("x"), httpx.RemoteProtocolError("y")]
        c, fake = failing_client([row("カードA", "店舗A", 500, NEW)], errs, ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "error"
        assert len(fake.calls) == 2, "再試行は1回だけ"

    def test_failed_fallback_is_error_not_pending(self, failing_client):
        # 収集済みか分からないカード（30日窓の読み直し）も、読めなければ「まだ集めていない」ではなく失敗
        c, _ = failing_client([], [RuntimeError("boom")], ["新カード"], cached=[])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "error"

    def test_names_split_by_byte_budget_and_only_failed_chunk_is_error(self, failing_client):
        # 長い名前50枚は1回の in_ に詰めない（URL 長）。失敗した分割のカードだけ error、他は読めた値を返す
        names = [f"とても長いカード名の{i:02d}" + "あ" * 35 for i in range(50)]  # 50文字以内（MAX_CARD_NAME_LEN）
        rows = [row(n, "店舗A", 100 + i, NEW) for i, n in enumerate(names)]
        c, fake = failing_client(rows, [], names)
        # 1回目の分割だけ失敗させる
        fake.errors.append(RuntimeError("400 Bad Request"))
        cards = _by_name(c.post("/api/wish-prices", json={"cards": names}).get_json())
        assert len(fake.calls) >= 2, "名前が分割されていない"
        first_chunk = set(fake.calls[0])
        assert len(first_chunk) < len(names)
        for i, n in enumerate(names):
            if n in first_chunk:
                assert cards[n]["status"] == "error"
            else:
                assert cards[n]["status"] == "ready"
                assert cards[n]["latest"] == 100 + i
