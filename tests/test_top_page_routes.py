# tests/test_top_page_routes.py — /api/top-decks・/api/top-movers の配線テスト
#
# 方針: ネットワーク不使用。meta_scraper呼び出し・Supabaseクライアントをすべて
# monkeypatch/fakeに置き換え、app.py側のキャッシュ・ソート・エラー分岐だけを検証する
# （集計ロジック本体の単体テストは tests/test_top_page.py 参照）

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


@pytest.fixture()
def _top_decks_isolated(monkeypatch):
    monkeypatch.setattr(app_module, "_top_decks_cache", {})
    monkeypatch.setattr(app_module, "_top_decks_cache_time", 0)
    monkeypatch.setattr(app_module, "_estimate_cache", {
        "カードA": {"shop": "店1", "price": 1000, "rarity": "ノーマル", "recorded_at": "2026-08-31"},
        "カードB": {"shop": "店1", "price": 2000, "rarity": "ノーマル", "recorded_at": "2026-08-31"},
    })
    monkeypatch.setattr(app_module, "_estimate_cache_time", time.time())
    monkeypatch.setattr(app_module, "_cardnames_loaded", True)
    monkeypatch.setattr(app_module, "_cardnames", [])
    monkeypatch.setattr(app_module, "_cardnames_set", set())
    monkeypatch.setattr(app_module, "_cardnames_fuzzy", {})
    monkeypatch.setattr(app_module, "_cardnames_reading", {})
    monkeypatch.setattr(app_module, "_cardnames_reading_fuzzy", {})
    yield


def _fake_tiers():
    return [
        {"name": "強デッキ", "tier": 1, "share": 20.0, "tops": 5, "rank": 1},
        {"name": "安デッキ", "tier": 3, "share": 5.0, "tops": 2, "rank": 5},
    ]


def _fake_deck_cards(theme, force=False):
    if theme == "強デッキ":
        return {"theme": theme, "tier": 1, "share": 20.0,
                "cards": [{"name": "カードA", "adoption": 100.0, "avg": 1.0},
                          {"name": "カードB", "adoption": 100.0, "avg": 1.0},
                          {"name": "見つからないカード", "adoption": 100.0, "avg": 1.0}],
                "full_deck": []}
    return {"theme": theme, "tier": 3, "share": 5.0,
            "cards": [{"name": "カードA", "adoption": 100.0, "avg": 1.0}],
            "full_deck": []}


class TestApiTopDecks:
    def test_returns_totals_with_missing_count(self, monkeypatch, _top_decks_isolated):
        monkeypatch.setattr(app_module, "fetch_tier_list", lambda force=False: _fake_tiers())
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)

        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-decks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["error"] is None
        names = {d["name"] for d in data["decks"]}
        assert names == {"強デッキ", "安デッキ"}
        strong = next(d for d in data["decks"] if d["name"] == "強デッキ")
        assert strong["total"] == 3000  # カードA(1000)+カードB(2000)、見つからないカードは除外
        assert strong["priced_count"] == 2
        assert strong["missing_count"] == 1

    def test_sort_price_default_ascending(self, monkeypatch, _top_decks_isolated):
        monkeypatch.setattr(app_module, "fetch_tier_list", lambda force=False: _fake_tiers())
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-decks")
        decks = resp.get_json()["decks"]
        assert decks[0]["name"] == "安デッキ"  # total=1000 < 強デッキ3000

    def test_sort_tier(self, monkeypatch, _top_decks_isolated):
        monkeypatch.setattr(app_module, "fetch_tier_list", lambda force=False: _fake_tiers())
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-decks?sort=tier")
        decks = resp.get_json()["decks"]
        assert decks[0]["name"] == "強デッキ"  # tier=1が最強

    def test_invalid_sort_rejected(self, monkeypatch, _top_decks_isolated):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-decks?sort=nonsense")
        assert resp.status_code == 400

    def test_cache_hit_skips_refetch(self, monkeypatch, _top_decks_isolated):
        calls = {"n": 0}
        def counting_fetch_tier_list(force=False):
            calls["n"] += 1
            return _fake_tiers()
        monkeypatch.setattr(app_module, "fetch_tier_list", counting_fetch_tier_list)
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        client.get("/api/top-decks")
        client.get("/api/top-decks")
        assert calls["n"] == 1, "2回目はキャッシュから返り、fetch_tier_listを再度呼ばない"

    def test_empty_tier_list_reports_error(self, monkeypatch, _top_decks_isolated):
        monkeypatch.setattr(app_module, "fetch_tier_list", lambda force=False: [])
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-decks")
        data = resp.get_json()
        assert data["decks"] == []
        assert data["error"], "取得失敗時は空を黙って返さずerrorを示すこと"

    # ── Q-3: as_completed のタイムアウトで500にならず、取得できた分だけで続行する ──

    def test_as_completed_timeout_error_is_caught_directly(self, monkeypatch, _top_decks_isolated):
        """as_completed(timeout=...) が実際にTimeoutErrorを送出しても
        _get_top_decks_cached が例外を外へ漏らさないことを直接確認する"""
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        monkeypatch.setattr(app_module, "fetch_tier_list", lambda force=False: _fake_tiers())
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)

        def raising_as_completed(_futures, timeout=None):
            if False:
                yield None  # ジェネレータにするためのダミー（到達しない）
            raise FuturesTimeoutError("模擬タイムアウト")

        monkeypatch.setattr(app_module, "as_completed", raising_as_completed)
        result = app_module._get_top_decks_cached()
        # 例外が外に漏れず、辞書が返ること（500にならない）。取得できたデッキが
        # 無ければエラーを示す辞書、取得できていれば通常の結果になる
        assert isinstance(result, dict)
        assert "decks" in result

    # ── Q-4: fetch_tier_list は executor 経由・タイムアウト付きで呼ぶ ──

    def test_fetch_tier_list_called_through_executor_with_timeout(self, monkeypatch, _top_decks_isolated):
        """フラスクのリクエストスレッドをブロックしないよう、fetch_tier_list は
        _meta_executor.submit(...).result(timeout=25) 経由で呼ばれること"""
        calls = {"submitted": False}
        real_submit = app_module._meta_executor.submit

        def spy_submit(fn, *args, **kwargs):
            if fn is app_module.fetch_tier_list:
                calls["submitted"] = True
            return real_submit(fn, *args, **kwargs)

        monkeypatch.setattr(app_module._meta_executor, "submit", spy_submit)
        monkeypatch.setattr(app_module, "fetch_tier_list", lambda: _fake_tiers())
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)
        app_module._get_top_decks_cached()
        assert calls["submitted"], "fetch_tier_listはexecutor経由で呼ぶこと（同期直呼びはFlaskスレッドを最悪126秒占有する）"

    # ── Q-5: 相場キャッシュ未ロードのワーカーで全デッキ¥0をキャッシュしない ──

    def test_uncached_estimate_returns_error_without_computing(self, monkeypatch, _top_decks_isolated):
        monkeypatch.setattr(app_module, "_estimate_cache", {})
        monkeypatch.setattr(app_module, "_estimate_cache_time", 0)  # 未ロード
        fetch_calls = {"n": 0}

        def counting_fetch_tier_list(force=False):
            fetch_calls["n"] += 1
            return _fake_tiers()

        monkeypatch.setattr(app_module, "fetch_tier_list", counting_fetch_tier_list)
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-decks")
        data = resp.get_json()
        assert data["decks"] == []
        assert data["error"], "相場キャッシュ未ロード時は計算せずerrorを返すこと"
        assert fetch_calls["n"] == 0, "相場が無いと分かっている時点でTier表の取得すら行わないこと"
        assert app_module._top_decks_cache_time == 0, "この結果をキャッシュしないこと"

    def test_all_priced_count_zero_not_cached(self, monkeypatch, _top_decks_isolated):
        # 相場キャッシュはロード済み(_estimate_cache_time!=0)だが中身が空、という
        # 異常系（Q-5の二段目のガード）。全デッキ priced_count=0 になったらキャッシュしない
        monkeypatch.setattr(app_module, "_estimate_cache", {})  # 空だが時刻は正常値
        monkeypatch.setattr(app_module, "fetch_tier_list", lambda force=False: _fake_tiers())
        monkeypatch.setattr(app_module, "fetch_deck_cards", _fake_deck_cards)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-decks")
        data = resp.get_json()
        assert data["error"], "全デッキpriced_count=0はキャッシュせずerrorを示すこと"
        assert app_module._top_decks_cache_time == 0

    # ── Q-8: [EX]見出し行がカードとして数えられ、未取得枚数が水増しされない ──

    def test_ex_deck_marker_line_not_counted_as_missing_card(self, monkeypatch, _top_decks_isolated):
        monkeypatch.setattr(app_module, "_estimate_cache", {
            "メインカード": {"shop": "店1", "price": 1000, "rarity": "ノーマル", "recorded_at": "2026-08-31"},
            "EXカード": {"shop": "店1", "price": 500, "rarity": "ノーマル", "recorded_at": "2026-08-31"},
        })

        def fake_deck_cards_with_ex(theme, force=False):
            return {
                "theme": theme, "tier": 1, "share": 20.0, "cards": [],
                "full_deck": [
                    {"name": "メインカード", "qty": 2, "is_ex": False},
                    {"name": "EXカード", "qty": 1, "is_ex": True},
                ],
            }

        monkeypatch.setattr(app_module, "fetch_tier_list",
                             lambda force=False: [{"name": "フルレシピデッキ", "tier": 1, "share": 20.0}])
        monkeypatch.setattr(app_module, "fetch_deck_cards", fake_deck_cards_with_ex)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-decks")
        data = resp.get_json()
        assert data["error"] is None
        deck = data["decks"][0]
        assert deck["priced_count"] == 2
        assert deck["missing_count"] == 0, "[EX]見出し行がカードとしてカウントされ、未取得扱いになってはいけない"
        assert deck["total"] == 1000 * 2 + 500


# ── /api/top-movers ──

class _FakeDayQuery:
    """.select().gte().lt().order().range().execute() チェーンを模倣し、
    date_str に応じたrowsを1000行ページ分割で返す"""

    def __init__(self, rows_by_date: dict):
        self.rows_by_date = rows_by_date
        self._gte = None
        self._lt = None
        self._start = 0
        self._end = 0

    def select(self, *_a, **_k):
        return self

    def gte(self, _col, value):
        self._gte = value
        return self

    def lt(self, _col, value):
        self._lt = value
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        self._start, self._end = start, end
        return self

    def execute(self):
        rows = self.rows_by_date.get(self._gte, [])
        return SimpleNamespace(data=rows[self._start:self._end + 1])


class _FakeLatestQuery:
    def __init__(self, latest_date):
        self.latest_date = latest_date

    def select(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def execute(self):
        if not self.latest_date:
            return SimpleNamespace(data=[])
        return SimpleNamespace(data=[{"recorded_at": self.latest_date}])


class _FakeSupabaseMovers:
    """price_history.select(...) 呼び出しを、最初の1回=最新日取得、以降=日別取得
    として振り分けるfake。呼び出し順は app.py の実装（先に最新日を取ってから
    day-fetchを2回行う）に依存する。"""

    def __init__(self, latest_date, rows_by_date):
        self.latest_date = latest_date
        self.rows_by_date = rows_by_date
        self._call_n = 0

    def table(self, name):
        assert name == "price_history"
        self._call_n += 1
        if self._call_n == 1:
            return _FakeLatestQuery(self.latest_date)
        return _FakeDayQuery(self.rows_by_date)


# ── Q-2: _fetch_price_history_day のページング境界 ──

class _FakePriceHistoryTable:
    """price_history.select(...).gte().lt().order()×N.range().execute() を模倣する。
    渡された全行リストを、range(start,end) のとおりスライスして返す
    （本物のPostgRESTのrange分割と同じ意味）。.order() の呼び出し列を記録する。"""

    def __init__(self, rows: list, order_calls: list | None = None):
        self.rows = rows
        self.order_calls = order_calls if order_calls is not None else []
        self._start = 0
        self._end = 0

    def select(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def lt(self, *_a, **_k):
        return self

    def order(self, col, *_a, **_k):
        self.order_calls.append(col)
        return self

    def range(self, start, end):
        self._start, self._end = start, end
        return self

    def execute(self):
        from types import SimpleNamespace as _SNS
        return _SNS(data=self.rows[self._start:self._end + 1])


class _FakeSupabasePriceHistory:
    def __init__(self, rows: list):
        self.rows = rows
        self.order_calls: list = []

    def table(self, name):
        assert name == "price_history"
        return _FakePriceHistoryTable(self.rows, self.order_calls)


def _make_price_history_rows(n: int):
    return [{"card_name": f"カード{i:05d}", "shop": "A店", "rarity": "ノーマル", "min_price": 1000}
            for i in range(n)]


class TestFetchPriceHistoryDay:
    def test_paginates_across_1000_row_boundary_without_loss(self, monkeypatch):
        # 本番実測(30,851行)を模した規模。同一card_nameが店舗・レアリティを跨いで
        # 複数存在する状況を想定し、複合キーのページングで全件過不足なく取れることを確認
        rows = _make_price_history_rows(2500)
        fake = _FakeSupabasePriceHistory(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)

        result_rows, complete = app_module._fetch_price_history_day("2026-08-31")
        assert complete is True
        assert len(result_rows) == 2500
        assert {r["card_name"] for r in result_rows} == {r["card_name"] for r in rows}

    def test_orders_by_card_name_shop_rarity_for_stable_pagination(self, monkeypatch):
        # card_name だけでは一意にならず、ページ境界で行が重複・欠落しうる(Q-2)。
        # 複合キー(card_name, shop, rarity)で安定化していることを確認する
        fake = _FakeSupabasePriceHistory([])
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module._fetch_price_history_day("2026-08-31")
        assert fake.order_calls == ["card_name", "shop", "rarity"]

    def test_truncates_and_marks_incomplete_beyond_max_pages(self, monkeypatch):
        total = app_module._PRICE_HISTORY_DAY_MAX_PAGES * 1000 + 500
        rows = _make_price_history_rows(total)
        fake = _FakeSupabasePriceHistory(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)

        result_rows, complete = app_module._fetch_price_history_day("2026-08-31")
        assert complete is False, "暴走ガード到達時は complete=False で不完全であることを示すこと"
        assert len(result_rows) == app_module._PRICE_HISTORY_DAY_MAX_PAGES * 1000

    def test_max_pages_covers_production_volume(self):
        # 本番実測(2026-08-31・司令塔): 最新日のprice_historyは30,851行。
        # 旧 max_pages=20（2万行上限）はこれを下回っており、後半のカードが黙って
        # 欠落していた。十分な余裕を持たせていることを固定する
        assert app_module._PRICE_HISTORY_DAY_MAX_PAGES * 1000 > 30_851


@pytest.fixture()
def _top_movers_isolated(monkeypatch):
    monkeypatch.setattr(app_module, "_top_movers_cache", {})
    monkeypatch.setattr(app_module, "_top_movers_cache_time", 0)
    yield


class TestApiTopMovers:
    def test_returns_up_and_down(self, monkeypatch, _top_movers_isolated):
        rows_by_date = {
            "2026-08-31": [{"card_name": "値上がりカード", "shop": "A店", "min_price": 2000}],
            "2026-08-24": [{"card_name": "値上がりカード", "shop": "A店", "min_price": 1000}],
        }
        fake = _FakeSupabaseMovers("2026-08-31", rows_by_date)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-movers?direction=up")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["error"] is None
        assert data["date_new"] == "2026-08-31"
        assert data["date_old"] == "2026-08-24"
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "値上がりカード"

        resp_down = client.get("/api/top-movers?direction=down")
        assert resp_down.get_json()["items"] == []

    def test_invalid_direction_rejected(self, monkeypatch, _top_movers_isolated):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-movers?direction=sideways")
        assert resp.status_code == 400

    def test_no_supabase_client_reports_error_not_silent_empty(self, monkeypatch, _top_movers_isolated):
        monkeypatch.setattr(app_module, "_supabase_client", None)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-movers")
        data = resp.get_json()
        assert data["items"] == []
        assert data["error"], "DB未接続時は失敗を示すこと（黙って空にしない）"

    def test_cache_hit_skips_refetch(self, monkeypatch, _top_movers_isolated):
        rows_by_date = {
            "2026-08-31": [{"card_name": "値上がりカード", "shop": "A店", "min_price": 2000}],
            "2026-08-24": [{"card_name": "値上がりカード", "shop": "A店", "min_price": 1000}],
        }
        fake = _FakeSupabaseMovers("2026-08-31", rows_by_date)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        client.get("/api/top-movers")
        call_count_after_first = fake._call_n
        client.get("/api/top-movers")
        assert fake._call_n == call_count_after_first, "2回目はキャッシュから返り、DBを再度叩かない"

    # ── P-2: 定着チェックの配線（前日データの取得〜app.py側の受け渡し） ──

    def test_stability_check_filters_unstable_entry_via_route(self, monkeypatch, _top_movers_isolated):
        """前日(date_new-1)のデータが取れれば定着チェックが働き、在庫入替の疑いが
        あるカード（前日≠当日）はランキングから消える"""
        rows_by_date = {
            "2026-08-31": [{"card_name": "在庫入替カード", "shop": "A店", "min_price": 2000},
                            {"card_name": "定着カード", "shop": "A店", "min_price": 3000}],
            "2026-08-24": [{"card_name": "在庫入替カード", "shop": "A店", "min_price": 1000},
                            {"card_name": "定着カード", "shop": "A店", "min_price": 1500}],
            "2026-08-30": [{"card_name": "在庫入替カード", "shop": "A店", "min_price": 1200},
                            {"card_name": "定着カード", "shop": "A店", "min_price": 3000}],
        }
        fake = _FakeSupabaseMovers("2026-08-31", rows_by_date)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-movers?direction=up")
        data = resp.get_json()
        assert data["stability_checked"] is True
        names = [item["name"] for item in data["items"]]
        assert "定着カード" in names
        assert "在庫入替カード" not in names, "前日と当日が食い違うカードは定着チェックで除外されること"

    def test_prev_day_fetch_failure_falls_back_to_skip_stability(self, monkeypatch, _top_movers_isolated):
        """前日データの取得が例外で失敗しても、当日/7日前の取得自体は成功していれば
        ランキング全体を失敗にせず、定着チェックだけをスキップして返す"""
        rows_by_date = {
            "2026-08-31": [{"card_name": "値上がりカード", "shop": "A店", "min_price": 2000}],
            "2026-08-24": [{"card_name": "値上がりカード", "shop": "A店", "min_price": 1000}],
        }
        fake = _FakeSupabaseMovers("2026-08-31", rows_by_date)
        monkeypatch.setattr(app_module, "_supabase_client", fake)

        date_prev = "2026-08-30"
        real_fetch = app_module._fetch_price_history_day

        def flaky_fetch(date_str):
            if date_str == date_prev:
                raise RuntimeError("前日データの取得に失敗（模擬）")
            return real_fetch(date_str)

        monkeypatch.setattr(app_module, "_fetch_price_history_day", flaky_fetch)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-movers?direction=up")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["error"] is None, "前日取得の失敗だけでランキング全体を失敗にしないこと"
        assert data["stability_checked"] is False
        assert len(data["items"]) == 1
        assert data["items"][0]["name"] == "値上がりカード"

    # ── Q-2: 当日/7日前データが不完全（暴走ガード到達）ならキャッシュしない ──

    def test_incomplete_new_or_old_day_not_cached(self, monkeypatch, _top_movers_isolated):
        def incomplete_fetch(date_str):
            if date_str == "2026-08-31":
                return ([{"card_name": "カード", "shop": "A店", "rarity": "ノーマル", "min_price": 2000}], False)
            return ([{"card_name": "カード", "shop": "A店", "rarity": "ノーマル", "min_price": 1000}], True)

        monkeypatch.setattr(app_module, "_fetch_price_history_day", incomplete_fetch)
        fake = _FakeSupabaseMovers("2026-08-31", {})  # 最新日取得のためだけに使う
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-movers")
        data = resp.get_json()
        assert data["error"], "不完全なデータを黙って使わず失敗を示すこと"
        assert app_module._top_movers_cache_time == 0, "不完全な結果はキャッシュされないこと"
