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
from frozen_clock import freeze_now  # noqa: E402


def row(card, shop, price, date, rarity="ノーマル"):
    return {"card_name": card, "shop": shop, "rarity": rarity,
            "min_price": price, "recorded_at": date}


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    def _make(rows, card_names, daily_rows=None):
        monkeypatch.setattr(app_module, "_supabase_client", _FakeClient(rows, daily_rows))
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


# 読み込みは「今日から N 日前まで」の日付ごとなので、テストの日付も今日を起点にする。
# 収集と実行のあいだに JST の0時をまたいでもずれないよう、窓を作る本体の時計をこの時刻に固定する
_NOW = datetime.now(JST)
_TODAY = _NOW.date()


@pytest.fixture(autouse=True)
def _freeze_window_clock(monkeypatch):
    import price_history_reads
    freeze_now(monkeypatch, price_history_reads, _NOW)
OLD, NEW = (_TODAY - timedelta(days=7)).isoformat(), _TODAY.isoformat()
_YESTERDAY = (_TODAY - timedelta(days=1)).isoformat()


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
        elif self._table != "price_history":
            id_to_name = {v: k for k, v in self._ids.items()}
            self._calls.append(sorted(id_to_name[i] for i in self._filters.get("card_id", set())))
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

    def test_failed_daily_min_read_is_error_not_pending(self, failing_client):
        # 日ごとの最安の読み込みに失敗したら、そのカードは「まだ集めていない」ではなく失敗
        c, fake = failing_client([], {"price_daily_min": [RuntimeError("boom")]}, ["新カード"], cached=[])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "error"
        assert fake.calls["price_daily_min"] == [["新カード"]]

    def test_failed_rarity_names_read_is_error_not_pending(self, failing_client):
        # レアリティの名簿が読めなければ、日ごとの最安を読むカードは失敗（「まだ集めていない」にしない）
        c, _ = failing_client([], {"rarity_dim": [RuntimeError("boom")]}, ["新カード"], cached=[])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "error"

    def test_daily_min_connection_terminated_is_retried_once(self, failing_client):
        import httpx
        c, fake = failing_client([], {"price_daily_min": [httpx.RemoteProtocolError("x")]}, ["新カード"], cached=[])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "pending", "再試行で読めた（行は無い）"
        assert len(fake.calls["price_daily_min"]) == 2

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

    def test_fallback_window_is_store_retention_days(self, client):
        # estimate_cache に無いカードは、店舗別の行が残る期間（PRICE_RETENTION_DAYS 日）を1日ずつ読み直す
        import app as app_module
        from constants import PRICE_RETENTION_DAYS
        rows = [row("新カード", "店舗A", 700, (_TODAY - timedelta(days=PRICE_RETENTION_DAYS + 1)).isoformat())]
        c = client(rows, [])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "ready"
        assert info["latest"] == 700, "毎朝の畳み込みまで店舗別にしか無い PRICE_RETENTION_DAYS+1 日前の日も読む"
        ph = [x for x in app_module._supabase_client.log if x[0] == "price_history"]
        assert len(ph) == PRICE_RETENTION_DAYS + 2
        assert not [x for x in app_module._supabase_client.log if x[0] == "price_daily_min"], "店舗別で見つかれば日ごとの表は読まない"

    def test_older_price_comes_from_daily_min_without_change(self, client):
        # 店舗別の行が消えた古い日（15〜30日前）の値段は、日ごとの全店最安（price_daily）から出す。値動きは出さない
        d20 = (_TODAY - timedelta(days=20)).isoformat()
        d18 = (_TODAY - timedelta(days=18)).isoformat()
        daily = [row("新カード", "店舗A", 800, d20), row("新カード", "店舗B", 650, d18),
                 row("新カード", "店舗C", 9000, d18, rarity="シークレット")]
        c = client([], [], daily_rows=daily)
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "ready"
        assert info["latest"] == 650, "最新日の代表レアリティ（最安）の全店最安"
        assert info["diff"] is None and info["pct"] is None and info["base_7d"] is None

    def test_daily_min_day_only_at_suspended_shop_is_skipped(self, client, monkeypatch):
        # 最安の店が休止中だけの日は使わない（店舗別の読み出しが休止中の店を外すのと同じ。次に安い店は畳んだ表に無い）
        import price_history_reads
        monkeypatch.setattr(price_history_reads, "is_suspended", lambda s: s == "休止店")
        d20 = (_TODAY - timedelta(days=20)).isoformat()
        d18 = (_TODAY - timedelta(days=18)).isoformat()
        daily = [row("新カード", "店舗A", 800, d20), row("新カード", "休止店", 500, d18), row("新カード", "店舗A", 900, d18)]
        c = client([], [], daily_rows=daily)
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["latest"] == 800, "休止店だけが最安の 18日前は捨て、20日前の値を出す"

    def test_daily_min_uses_price_floor_like_store_rows(self, client):
        # 10円未満の店がある日も、店舗別の読み出しと同じく10円以上の最安を出す（日を丸ごと捨てない）
        d18 = (_TODAY - timedelta(days=18)).isoformat()
        c = client([], [], daily_rows=[row("新カード", "店舗A", 5, d18), row("新カード", "店舗B", 300, d18)])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "ready"
        assert info["latest"] == 300

    def test_daily_min_beyond_fallback_window_is_ignored(self, client):
        # WISH_FALLBACK_DAYS（30日）より前の日ごとの最安は使わない（今までの30日窓と同じ）
        old = (_TODAY - timedelta(days=45)).isoformat()
        c = client([], [], daily_rows=[row("新カード", "店舗A", 800, old)])
        info = _by_name(c.post("/api/wish-prices", json={"cards": ["新カード"]}).get_json())["新カード"]
        assert info["status"] == "pending"

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


class TestManyCards:
    """2026-10-31: 上限50で、51種目より後に入れたカードが返らず「価格が見つかりませんでした」になっていた。"""

    def test_cards_after_the_50th_are_returned(self, client):
        names = [f"テストカード{i:03d}" for i in range(55)]
        rows = [row(n, "店舗A", 100 + i, NEW) for i, n in enumerate(names)]
        c = client(rows, names)
        res = c.post("/api/wish-prices", json={"cards": names})
        got = _by_name(res.get_json())
        assert len(got) == 55
        assert got["テストカード054"]["latest"] == 154

    def test_cards_over_the_limit_are_cut(self, client):
        import app as app_module
        names = [f"テストカード{i:03d}" for i in range(app_module.MAX_WISH_CARDS + 1)]
        c = client([], names)
        res = c.post("/api/wish-prices", json={"cards": names})
        assert len(res.get_json()["cards"]) == app_module.MAX_WISH_CARDS


class TestSharedReads:
    """2026-11-01: 購入候補を開くと価格とレアリティの API が同じカードの同じ日を同時に読み、共有の実行枠を取り合って
    締め切りを過ぎ、レアリティ候補（58枚）・まとめ買い（19枚）が丸ごと失敗になっていた。"""

    _COLS = "card_id, shop_id, rarity_id, card_name, rarity, shop, min_price, recorded_at"

    def test_second_caller_uses_rows_already_read(self):
        # 同じカード・同じ日は2回読みに行かない（価格→レアリティ候補→まとめ買い）
        import price_history_reads as phr
        days = phr.jst_days_back(7)
        fake = _FailingClient([row("カードA", "店舗A", 500, days[-1])], {}, ["カードA"])
        rows1, f1 = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        rows2, f2 = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], "card_id, card_name, rarity, shop",
                                                       days, "b", shared=True)
        assert f1 == f2 == set()
        assert [r["min_price"] for r in rows1] == [500]
        assert [r["card_name"] for r in rows2] == ["カードA"]
        assert len(fake.calls["price_history"]) == len(days) + 1, "2回目は今日だけ読みに行く"
        assert fake.calls["price_history"][-1] == ({"カードA"}, NEW)

    def test_rows_handed_out_are_copies(self):
        # 呼び出し元は行を書き換える（rarity の正規化・card_name の戻し）。取っておいた行は汚さない
        import price_history_reads as phr
        days = [_YESTERDAY]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0])], {}, ["カードA"])
        rows1, _ = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        rows1[0]["min_price"] = -1
        rows2, _ = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "b", shared=True)
        assert rows2[0]["min_price"] == 500

    def test_concurrent_callers_wait_for_the_same_read(self, monkeypatch):
        # 読んでいる途中のカード・日付は、新しく読みに行かずその読み込みの終わりを待つ
        import price_history_reads as phr
        orig = phr._read_day

        def slow(*a):
            time.sleep(0.05)
            return orig(*a)

        monkeypatch.setattr(phr, "_read_day", slow)
        days = phr.jst_days_back(7)[:-1]  # 過去の日だけ（今日は毎回読む）
        fake = _FailingClient([row("カードA", "店舗A", 500, d) for d in days], {}, ["カードA"])
        out = {}

        def run(tag):
            out[tag] = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, tag, shared=True)

        ts = [threading.Thread(target=run, args=(t,)) for t in ("a", "b")]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert len(fake.calls["price_history"]) == len(days)
        for tag in ("a", "b"):
            rows, failed = out[tag]
            assert failed == set()
            assert len(rows) == len(days)

    def test_only_unread_cards_are_read(self):
        # 取っておきにあるカードは読まず、無いカードだけ読む
        import price_history_reads as phr
        days = [_YESTERDAY]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0]), row("カードB", "店舗A", 700, days[0])],
                              {}, ["カードA", "カードB"])
        phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA", "カードB"], self._COLS, days, "b",
                                                          shared=True)
        assert failed == set()
        assert sorted(r["card_name"] for r in rows) == ["カードA", "カードB"]
        assert fake.calls["price_history"][-1] == ({"カードB"}, days[0])

    def test_today_is_read_fresh_after_a_write(self):
        # 今日は「価格を更新」・検索が書いた直後に画面が読み直すので、取っておかない（2026-11-01 reviewer 高1）
        import price_history_reads as phr
        days = phr.jst_days_back(2)
        rows_in = [row("カードA", "店舗A", 500, NEW)]
        fake = _FailingClient(rows_in, {}, ["カードA"])
        phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        rows_in[0]["min_price"] = 600
        rows, _ = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "b", shared=True)
        assert [r["min_price"] for r in rows] == [600]
        assert [d for _n, d in fake.calls["price_history"]].count(NEW) == 2
        assert len(fake.calls["price_history"]) == len(days) + 1, "過去の日は読み直さない"

    def test_stuck_read_is_not_borrowed(self, monkeypatch):
        # 締め切りより長く掛かっている読み込みは借りず、自分で読み直す（1本の詰まりに後の要求を巻き込まない）
        import price_history_reads as phr
        from concurrent.futures import Future
        days = [_YESTERDAY]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0])], {}, ["カードA"])
        cid = fake._ids["カードA"]
        phr._shared_inflight[(cid, days[0])] = (Future(), time.monotonic() - phr._READ_DEADLINE_SEC - 1)
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        assert failed == set()
        assert [r["min_price"] for r in rows] == [500]

    def test_rows_over_the_limit_are_dropped(self, monkeypatch):
        # 取っておく行の数に上限（メモリを張り付かせない）
        import price_history_reads as phr
        monkeypatch.setattr(phr, "_SHARED_MAX_ROWS", 2)
        days = phr.jst_days_back(4)[:-1]
        fake = _FailingClient([row("カードA", "店舗A", 500, d) for d in days], {}, ["カードA"])
        phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        assert phr._shared_row_total <= 2
        assert sum(max(1, len(r)) for _e, r in phr._shared_rows.values()) == phr._shared_row_total

    def test_read_started_before_reset_is_not_kept(self):
        # 取っておきを捨てる前に始まった読み込みの行は、終わっても取っておかない
        import price_history_reads as phr
        from concurrent.futures import Future
        fut = Future()
        old_gen = phr._shared_gen
        phr._reset_shared_for_tests()
        fut.set_result([{"card_id": 1, "card_name": "カードA"}])
        phr._store_shared([1], _YESTERDAY, old_gen, fut)
        assert phr._shared_rows == {}

    def test_failed_read_is_not_kept(self):
        # 読めなかった日は取っておかず、次の呼び出しが読み直す
        import price_history_reads as phr
        days = [_YESTERDAY]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0])],
                              _ph([RuntimeError("timeout")], day=days[0]), ["カードA"])
        _rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a", shared=True)
        assert failed == {"カードA"}
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "b", shared=True)
        assert failed == set()
        assert [r["min_price"] for r in rows] == [500]

    def test_columns_outside_shared_set_are_refused(self):
        import price_history_reads as phr
        fake = _FailingClient([], {}, ["カードA"])
        with pytest.raises(ValueError):
            phr.fetch_daily_rows_by_card_names(fake, ["カードA"], "card_id, min_price_any", [NEW], "a", shared=True)

    def test_partial_days_keeps_days_that_were_read(self):
        # partial_days: 1日読めなくても、読めた日の行で返す（レアリティ候補）
        import price_history_reads as phr
        days = phr.jst_days_back(7)
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0], "シークレット"),
                               row("カードA", "店舗A", 300, days[-1])],
                              _ph([RuntimeError("timeout")]), ["カードA"])
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a",
                                                          partial_days=True)
        assert failed == set()
        assert [r["rarity"] for r in rows] == ["シークレット"]

    def test_partial_days_fails_card_with_no_day_read(self):
        import price_history_reads as phr
        days = [_TODAY.isoformat()]
        fake = _FailingClient([row("カードA", "店舗A", 500, days[0])],
                              _ph([RuntimeError("timeout")]), ["カードA"])
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a",
                                                          partial_days=True)
        assert failed == {"カードA"}
        assert rows == []

    def test_partial_days_deadline_returns_days_read_so_far(self, monkeypatch):
        # 締め切りを過ぎても、読み終えた日の行は返す
        import price_history_reads as phr
        monkeypatch.setattr(phr, "_READ_DEADLINE_SEC", 0.2)
        monkeypatch.setattr(phr, "_MAX_IN_FLIGHT", 1)
        orig = phr._read_day

        def slow_on_old_days(sb, cols, ids, day, tag):
            if day != NEW:
                time.sleep(0.5)
            return orig(sb, cols, ids, day, tag)

        monkeypatch.setattr(phr, "_read_day", slow_on_old_days)
        days = list(reversed(phr.jst_days_back(3)))  # 今日を先に読む
        fake = _FailingClient([row("カードA", "店舗A", 500, d) for d in days], {}, ["カードA"])
        rows, failed = phr.fetch_daily_rows_by_card_names(fake, ["カードA"], self._COLS, days, "a",
                                                          partial_days=True)
        assert failed == set()
        assert [r["recorded_at"] for r in rows] == [NEW]

    def test_rarities_survive_a_failed_day_but_shop_totals_do_not(self, failing_client):
        # レアリティ候補は読めた日だけで出す。まとめ買いは全部の日が読めたカードだけ DB の値を使い、
        # 読めなかったカードは店のリアルタイム検索に回す（db_missing）
        days = [(_TODAY - timedelta(days=d)).isoformat() for d in range(7, -1, -1)]
        rows = [row("カードA", "店舗A", 500, days[0], "シークレット"), row("カードA", "店舗A", 300, NEW)]
        c, fake = failing_client(rows, _ph([RuntimeError("timeout")], day=days[3]), ["カードA"])
        got = c.post("/api/card-rarities", json={"cards": ["カードA"]}).get_json()
        assert set(got["rarities"]["カードA"]) == {"シークレット", "ノーマル"}
        fake.errors["price_history"][days[4]] = [RuntimeError("timeout")]
        import price_history_reads as phr
        phr._reset_shared_for_tests()  # 取っておきを捨て、まとめ買いにも DB を読ませる
        tot = c.post("/api/wish-shop-totals", json={"cards": [{"name": "カードA", "qty": 1}]}).get_json()
        assert tot["db_missing"] == [{"name": "カードA", "rarity": ""}]

    def test_wish_prices_then_rarities_reads_db_once(self, failing_client):
        # 購入候補を開いたときの流れ: 価格（9日）の後のレアリティ候補（8日）は今日だけ読む
        rows = [row("カードA", "店舗A", 500, NEW, "シークレット")]
        c, fake = failing_client(rows, {}, ["カードA"])
        assert _by_name(c.post("/api/wish-prices", json={"cards": ["カードA"]}).get_json())["カードA"]["latest"] == 500
        n = len(fake.calls["price_history"])
        got = c.post("/api/card-rarities", json={"cards": ["カードA"]}).get_json()
        assert got["rarities"] == {"カードA": ["シークレット"]}
        assert fake.calls["price_history"][n:] == [({"カードA"}, NEW)]
