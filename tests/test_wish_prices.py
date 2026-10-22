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
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from constants import JST  # noqa: E402


from fake_price_history import FakeClient as _FakeClient  # noqa: E402
from fake_price_history import FakeQuery as _FakeQuery  # noqa: E402
from fake_price_history import card_ids as _card_ids  # noqa: E402


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


# 読み込みは「今日から N 日前まで」の日付ごとなので、テストの日付も今日を起点にする
_TODAY = datetime.now(JST).date()
OLD, NEW = (_TODAY - timedelta(days=7)).isoformat(), _TODAY.isoformat()


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

_errors_lock = threading.Lock()


class _FailingQuery(_FakeQuery):
    """execute のたびに errors から1つ取り出して投げる（空なら成功）。
    errors は card_dim が呼ばれた順の list、price_history は日付ごとの list（日付どうしは同時に読むので順番が決まらない）。
    呼び出しをテーブルごとに記録する（price_history は (名前の集合, 日付)、card_dim は名前の並び）"""

    def __init__(self, table, rows, ids, errors, calls):
        super().__init__(table, rows, ids)
        self._errors = errors
        self._calls = calls.setdefault(table, [])

    def execute(self):
        if self._table == "card_dim":
            self._calls.append(sorted(self._filters["card_name"]))
            key = None
        else:
            id_to_name = {v: k for k, v in self._ids.items()}
            key = next(iter(self._filters["recorded_at"]))
            self._calls.append(({id_to_name[i] for i in self._filters["card_id"]}, key))
        with _errors_lock:
            queue = self._errors.get(self._table, [] if key is None else {})
            if key is not None:
                queue = queue.get(key, [])
            err = queue.pop(0) if queue else None
        if err is not None:
            raise err
        return super().execute()


class _FailingClient:
    def __init__(self, rows, errors, card_names):
        self._rows = rows
        # card_dim の名簿: 送ったカード名すべて（行の無いカードも名簿にはある）
        self._ids = _card_ids(rows)
        for n in card_names:
            self._ids.setdefault(n, len(self._ids) + 1)
        self.errors = errors  # {テーブル名: [例外 or None, ...]}
        self.calls = {}

    def table(self, name):
        return _FailingQuery(name, self._rows, self._ids, self.errors, self.calls)


@pytest.fixture
def failing_client(monkeypatch):
    import app as app_module

    def _make(rows, errors, card_names, cached=None):
        fake = _FailingClient(rows, errors, card_names)
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


def _ph(errors, day=None):
    """price_history の day（既定は今日）の読み込みで errors を順に投げる"""
    return {"price_history": {day or NEW: list(errors)}}


def _calls_on(fake, day):
    return [c for c in fake.calls.get("price_history", []) if c[1] == day]


class TestReadFailureIsNotNoPrice:
    def test_db_error_returns_error_status_not_ready(self, failing_client):
        # statement timeout などの失敗は再試行せず、status "error"（以前は "ready"・latest None＝「価格が見つかりませんでした」）
        c, fake = failing_client([], _ph([RuntimeError("canceling statement due to statement timeout")]), ["カードA", "カードB"])
        res = c.post("/api/wish-prices", json={"cards": ["カードA", "カードB"]})
        cards = _by_name(res.get_json())
        assert cards["カードA"]["status"] == "error"
        assert cards["カードB"]["status"] == "error"
        assert cards["カードA"]["latest"] is None
        assert len(_calls_on(fake, NEW)) == 1, "重い問い合わせの失敗は再試行しない"

    def test_no_price_rows_stays_ready_with_null(self, failing_client):
        # 読めたが行が無い＝値段なし（従来どおり ready・latest None）。失敗とは別の結果になる
        c, _ = failing_client([], {}, ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "ready"
        assert info["latest"] is None

    def test_connection_terminated_is_retried_once(self, failing_client):
        import httpx
        rows = [row("カードA", "店舗A", 500, NEW)]
        c, fake = failing_client(rows, _ph([httpx.RemoteProtocolError("<ConnectionTerminated error_code:0>")]), ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "ready"
        assert info["latest"] == 500
        assert len(_calls_on(fake, NEW)) == 2, "同じ日をもう1回だけ読む"
        assert len(fake.calls["price_history"]) == 9 + 1, "8日窓は今日を含めて9日。再試行の1回を足して10回"

    def test_connection_error_twice_gives_error(self, failing_client):
        import httpx
        errs = [httpx.RemoteProtocolError("x"), httpx.RemoteProtocolError("y")]
        c, fake = failing_client([row("カードA", "店舗A", 500, NEW)], _ph(errs), ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "error"
        assert len(_calls_on(fake, NEW)) == 2, "再試行は1回だけ"

    def test_failed_fallback_is_error_not_pending(self, failing_client):
        # 収集済みか分からないカード（30日窓の読み直し）も、読めなければ「まだ集めていない」ではなく失敗
        c, _ = failing_client([], _ph([RuntimeError("boom")]), ["新カード"], cached=[])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "error"

    def test_later_day_failure_discards_the_days_already_read(self, failing_client):
        # どれか1日で失敗したら、他の日に読めた行も捨てて error（半端な日付で最安・値動きを出さない）
        rows = [row("カードA", "店舗A", 1000, OLD), row("カードA", "店舗A", 900, NEW)]
        c, fake = failing_client(rows, _ph([RuntimeError("boom")], day=OLD), ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "error"
        assert info["latest"] is None
        assert len(_calls_on(fake, OLD)) == 1

    def test_card_dim_lookup_failure_is_error(self, failing_client):
        c, fake = failing_client([row("カードA", "店舗A", 500, NEW)], {"card_dim": [RuntimeError("boom")]}, ["カードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]
        assert info["status"] == "error"
        assert not fake.calls.get("price_history")

    def test_names_split_by_byte_budget_and_only_failed_chunk_is_error(self, failing_client):
        # 長い名前50枚は1回の in_ に詰めない（URL 長）。名前を使うのは card_dim の引き当てなので、そこで分ける。
        # 失敗した分割のカードだけ error、他は読めた値を返す
        names = [f"とても長いカード名の{i:02d}" + "あ" * 35 for i in range(50)]  # 50文字以内（MAX_CARD_NAME_LEN）
        rows = [row(n, "店舗A", 100 + i, NEW) for i, n in enumerate(names)]
        c, fake = failing_client(rows, {"card_dim": [RuntimeError("400 Bad Request")]}, names)
        cards = _by_name(c.post("/api/wish-prices", json={"cards": names}).get_json())
        assert len(fake.calls["card_dim"]) >= 2, "名前が分割されていない"
        first_chunk = set(fake.calls["card_dim"][0])
        assert len(first_chunk) < len(names)
        for i, n in enumerate(names):
            if n in first_chunk:
                assert cards[n]["status"] == "error"
            else:
                assert cards[n]["status"] == "ready"
                assert cards[n]["latest"] == 100 + i


class TestReadsByCardIdAndDayEquality:
    """2026-10-22 statement timeout の根本対策: カード番号＋日付の等号で、索引の並びどおりに読む"""

    def test_each_day_of_window_read_with_eq_and_index_order(self, client):
        import app as app_module
        rows = [row("テストカードA", "店舗A", 900, NEW)]
        c = client(rows, ["テストカードA"])
        c.post("/api/wish-prices", json={"cards": ["テストカードA"]})
        ph = [x for x in app_module._supabase_client.log if x[0] == "price_history"]
        days = sorted(next(iter(f["recorded_at"])) for _t, f, _o in ph)
        expected = [(_TODAY - timedelta(days=d)).isoformat() for d in range(8, -1, -1)]
        assert days == expected, "8日窓＝今日の8日前から今日までを1日ずつ（日付どうしは同時に読むので順は問わない）"
        for _t, f, orders in ph:
            assert "card_name" not in f, "price_history を名前で絞らない（索引が効かない）"
            assert f["card_id"] == {1}
            assert orders == ["card_id", "shop_id", "rarity_id"]

    def test_fallback_window_is_thirty_days(self, client):
        import app as app_module
        rows = [row("新カード", "店舗A", 700, (_TODAY - timedelta(days=20)).isoformat())]
        c = client(rows, [])  # estimate_cache に無い → 30日窓
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "ready"
        assert info["latest"] == 700
        ph = [x for x in app_module._supabase_client.log if x[0] == "price_history"]
        assert len(ph) == 31

    def test_day_over_1000_rows_is_paged_without_gaps(self, client):
        # 1日で1000行を超えたら range で次のページを読む（境目で行が落ちない）。最安の1行を最後に置く
        rows = [row("テストカードA", f"店舗{i:04d}", 5000 + i, NEW) for i in range(1200)]
        rows.append(row("テストカードA", "最安の店", 123, NEW))
        c = client(rows, ["テストカードA"])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["テストカードA"]}).get_json())["テストカードA"]
        assert info["latest"] == 123

    def test_name_missing_from_card_dim_is_pending(self, client):
        # card_dim に無い名前は行が0件（ビューは card_dim との内部結合なので従来と同じ結果）
        c = client([], [])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["名簿に無いカード"]}).get_json())["名簿に無いカード"]
        assert info["status"] == "pending"


class TestFailedChunkStopsAndOthersSurvive:
    def test_failed_chunk_reads_no_more_days(self, monkeypatch):
        # 失敗した束の残りの日は読みに行かない（statement timeout のときに負荷を積まない）。
        # 1本ずつ積む設定にして、時間に頼らず確かめる
        import price_history_reads as phr
        monkeypatch.setattr(phr, "_MAX_IN_FLIGHT", 1)
        days = [(_TODAY - timedelta(days=d)).isoformat() for d in range(8, -1, -1)]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[-1])],
                              {"price_history": {days[0]: [RuntimeError("timeout")]}}, ["カードA"])
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], "card_name", days, "test")
        assert failed == {"カードA"}
        assert rows == []
        assert len(fake.calls["price_history"]) == 1

    def test_in_flight_never_exceeds_limit(self, monkeypatch):
        # 1回の要求が同時に積むのは _MAX_IN_FLIGHT 本まで（共有の枠で新商品の表を待たせない）
        import price_history_reads as phr
        monkeypatch.setattr(phr, "_MAX_IN_FLIGHT", 2)
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}
        orig = phr._read_day

        def counting(*a):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            try:
                time.sleep(0.01)
                return orig(*a)
            finally:
                with lock:
                    state["now"] -= 1

        monkeypatch.setattr(phr, "_read_day", counting)
        days = [(_TODAY - timedelta(days=d)).isoformat() for d in range(30, -1, -1)]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[-1])], {}, ["カードA"])
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], "card_name", days, "test")
        assert failed == set()
        assert len(rows) == 1
        assert len(fake.calls["price_history"]) == 31
        assert state["peak"] <= 2

    def test_only_the_failed_chunk_is_error(self, monkeypatch):
        # 束が分かれるとき、失敗した束だけ捨て、ほかの束の行は残す
        import price_history_reads as phr
        monkeypatch.setattr(phr, "_ID_CHUNK", 1)
        days = [_TODAY.isoformat()]
        rows_in = [row("カードA", "店舗A", 500, days[0]), row("カードB", "店舗A", 700, days[0])]
        fake = _FailingClient(rows_in, {}, ["カードA", "カードB"])

        class OneChunkFails:
            def __init__(self, inner):
                self.inner = inner

            def table(self, name):
                q = self.inner.table(name)
                if name != "price_history":
                    return q
                orig_execute = q.execute

                def execute():
                    if q._filters.get("card_id") == {fake._ids["カードA"]}:
                        raise RuntimeError("boom")
                    return orig_execute()
                q.execute = execute
                return q

        rows, failed = phr.fetch_daily_rows_by_card_names(OneChunkFails(fake), ["カードA", "カードB"],
                                                          "card_name, min_price", days, "test")
        assert failed == {"カードA"}
        assert [r["card_name"] for r in rows] == ["カードB"]

    def test_read_timeout_is_not_retried(self):
        # 読み取りの時間切れは送り直さない（共有の枠を長くふさがない）
        import httpx
        import price_history_reads as phr
        days = [_TODAY.isoformat()]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0])],
                              {"price_history": {days[0]: [httpx.ReadTimeout("slow")]}}, ["カードA"])
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], "card_name", days, "test")
        assert failed == {"カードA"}
        assert len(fake.calls["price_history"]) == 1

    def test_deadline_returns_unread_chunks_as_failed(self, monkeypatch):
        # 締め切りを過ぎたら、読み終えていない束は failed にしてすぐ返す（要求を gunicorn の120秒まで引っぱらない）
        import price_history_reads as phr
        monkeypatch.setattr(phr, "_READ_DEADLINE_SEC", 0.05)
        orig = phr._read_day

        def slow(*a):
            time.sleep(0.3)
            return orig(*a)

        monkeypatch.setattr(phr, "_read_day", slow)
        days = phr.jst_days_back(8)
        fake = _FailingClient([row("カードA", "店舗A", 500, days[-1])], {}, ["カードA"])
        t = time.monotonic()
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], "card_name", days, "test")
        assert failed == {"カードA"}
        assert rows == []
        assert time.monotonic() - t < 0.25, "動いている読み込みの終わりを待たずに返す"

    def test_jst_days_back_matches_old_cutoff(self):
        # 以前の `recorded_at >= 今日の N 日前` と同じ日付の集合（今日を含めて N+1 日）
        import price_history_reads as phr
        days = phr.jst_days_back(7)
        assert len(days) == 8
        assert days[0] == (_TODAY - timedelta(days=7)).isoformat()
        assert days[-1] == _TODAY.isoformat()
