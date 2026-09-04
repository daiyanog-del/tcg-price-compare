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


# ── /api/top-movers（V-1: DB側RPC get_top_movers 版） ──

class _FakeSupabaseTopMovers:
    """.rpc("get_top_movers", params).execute() を模倣するfake。
    exc を渡すと execute() で例外を送出する。"""

    def __init__(self, rows: list | None = None, exc: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.exc = exc
        self.rpc_calls: list = []

    def rpc(self, name, params):
        assert name == "get_top_movers"
        self.rpc_calls.append(params)
        return self

    def execute(self):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(data=self.rows)


def _movers_row(name, rarity, price_old, price_new, pct, date_new="2026-09-01",
                 date_old="2026-08-25", stability_checked=True):
    return {"card_name": name, "rarity": rarity, "price_old": price_old, "price_new": price_new,
            "pct": pct, "date_new": date_new, "date_old": date_old,
            "stability_checked": stability_checked}


@pytest.fixture()
def _top_movers_isolated(monkeypatch):
    monkeypatch.setattr(app_module, "_top_movers_cache", {})
    monkeypatch.setattr(app_module, "_top_movers_cache_time", 0)
    monkeypatch.setattr(app_module, "_top_movers_group_cache", {})
    monkeypatch.setattr(app_module, "_top_movers_group_cache_time", {})
    yield


class TestApiTopMovers:
    def test_returns_up_and_down(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-movers?direction=up")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["error"] is None
        assert data["date_new"] == "2026-09-01"
        assert data["date_old"] == "2026-08-25"
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["name"] == "値上がりカード"
        assert item["today"] == 2000
        assert item["yesterday"] == 1000
        assert item["diff"] == 1000
        assert item["pct"] == 100.0

        resp_down = client.get("/api/top-movers?direction=down")
        assert resp_down.get_json()["items"] == []

    def test_splits_up_and_down_by_pct_sign(self, monkeypatch, _top_movers_isolated):
        # RPCはup/down合算・abs(pct)降順で返す。app.py側でpctの符号から振り分ける
        rows = [
            _movers_row("値上がりカード", "UR", 1000, 1300, 30.0),
            _movers_row("値下がりカード", "SR", 2000, 1000, -50.0),
        ]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        up_items = client.get("/api/top-movers?direction=up").get_json()["items"]
        down_items = client.get("/api/top-movers?direction=down").get_json()["items"]
        assert [i["name"] for i in up_items] == ["値上がりカード"]
        assert [i["name"] for i in down_items] == ["値下がりカード"]

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

    def test_rpc_failure_reports_error_not_silent_empty(self, monkeypatch, _top_movers_isolated):
        fake = _FakeSupabaseTopMovers(exc=RuntimeError("RPC失敗（模擬）"))
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-movers")
        data = resp.get_json()
        assert data["items"] == []
        assert data["error"], "RPC失敗時は失敗を示すこと（黙って空にしない）"

    def test_cache_hit_skips_refetch(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        client.get("/api/top-movers")
        call_count_after_first = len(fake.rpc_calls)
        client.get("/api/top-movers")
        assert len(fake.rpc_calls) == call_count_after_first, "2回目はキャッシュから返り、RPCを再度呼ばない"

    def test_rpc_called_with_top_page_constants(self, monkeypatch, _top_movers_isolated):
        """min_price/stability_daysはtop_page.pyの既存定数をそのまま渡し、
        二重定義しないこと"""
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        client.get("/api/top-movers")
        assert len(fake.rpc_calls) == 1
        params = fake.rpc_calls[0]
        assert params["p_min_price"] == app_module._top_page.MOVERS_MIN_PRICE
        assert params["p_stability_days"] == app_module._top_page.MOVERS_STABILITY_DAYS
        assert params["p_limit"] == app_module._top_page.TOP_MOVERS_RPC_LIMIT

    # ── stability_checked のRPC→APIレスポンスへのパススルー ──

    def test_stability_checked_passed_through_from_rpc(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0, stability_checked=True)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        data = client.get("/api/top-movers?direction=up").get_json()
        assert data["stability_checked"] is True

    def test_stability_checked_false_when_rpc_says_so(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0, stability_checked=False)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        data = client.get("/api/top-movers?direction=up").get_json()
        assert data["stability_checked"] is False

    def test_empty_rows_reports_error_not_cached(self, monkeypatch, _top_movers_isolated):
        fake = _FakeSupabaseTopMovers([])
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-movers")
        data = resp.get_json()
        assert data["items"] == []
        assert data["error"], "データが無い時は失敗を示すこと（黙って空にしない）"
        assert app_module._top_movers_cache_time == 0, "空データはキャッシュされないこと"

    # ── OF(オーバーフレーム)専用タブ（2026-09-04追加） ──

    def test_group_of_passes_of_rarities_to_rpc(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("OFカード", "OFウルトラ", 1000, 1300, 30.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-movers?group=of&direction=up")
        assert resp.status_code == 200
        assert len(fake.rpc_calls) == 1
        assert fake.rpc_calls[0]["p_rarities"] == list(app_module._top_page.OF_RARITIES)
        data = resp.get_json()
        assert data["items"][0]["name"] == "OFカード"

    def test_group_none_does_not_pass_p_rarities(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        client.get("/api/top-movers")
        assert "p_rarities" not in fake.rpc_calls[0]

    def test_invalid_group_rejected(self, monkeypatch, _top_movers_isolated):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-movers?group=gmr")
        assert resp.status_code == 400

    # ── グループ間キャッシュ分離: group=None と group=of は別辞書で管理されており、
    # 片方を温めても他方のキャッシュヒットにはならないこと ──

    def test_group_none_then_of_cache_is_separate(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        client.get("/api/top-movers")  # group=None を温める
        assert len(fake.rpc_calls) == 1
        client.get("/api/top-movers?group=of")
        assert len(fake.rpc_calls) == 2, "group=ofはgroup=Noneのキャッシュを流用せずRPCを再度呼ぶこと"
        assert fake.rpc_calls[1]["p_rarities"] == list(app_module._top_page.OF_RARITIES)

    def test_group_of_then_none_cache_is_separate(self, monkeypatch, _top_movers_isolated):
        rows = [_movers_row("値上がりカード", "UR", 1000, 2000, 100.0)]
        fake = _FakeSupabaseTopMovers(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        client.get("/api/top-movers?group=of")  # group=of を温める
        assert len(fake.rpc_calls) == 1
        client.get("/api/top-movers")
        assert len(fake.rpc_calls) == 2, "group=Noneはgroup=ofのキャッシュを流用せずRPCを再度呼ぶこと"
        assert "p_rarities" not in fake.rpc_calls[1]


# ── /api/top-priced（シリアル付きオーバーフレーム(GMR)専用: 高額順ランキング、2026-09-04追加） ──

class _FakeSupabaseTopPriced:
    """.rpc("get_top_priced", params).execute() を模倣するfake。"""

    def __init__(self, rows: list | None = None, exc: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.exc = exc
        self.rpc_calls: list = []

    def rpc(self, name, params):
        assert name == "get_top_priced"
        self.rpc_calls.append(params)
        return self

    def execute(self):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(data=self.rows)


def _priced_row(name, rarity, price_new, price_new_common=None, price_old=None, pct=None,
                 date_new="2026-09-04", date_old="2026-08-28"):
    return {"card_name": name, "rarity": rarity, "price_new": price_new,
            "price_new_common": price_new_common, "price_old": price_old, "pct": pct,
            "date_new": date_new, "date_old": date_old}


@pytest.fixture()
def _top_priced_isolated(monkeypatch):
    monkeypatch.setattr(app_module, "_top_priced_cache", {})
    monkeypatch.setattr(app_module, "_top_priced_cache_time", {})
    yield


class TestApiTopPriced:
    def test_returns_items_with_and_without_diff(self, monkeypatch, _top_priced_isolated):
        rows = [
            _priced_row("差額ありカード", "グランドマスター", 10000, price_new_common=10000,
                        price_old=8000, pct=25.0),
            _priced_row("差額なしカード", "グランドマスター", 5000),  # 7日前の共通店舗価格なし
        ]
        fake = _FakeSupabaseTopPriced(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-priced?group=gmr&limit=10")
        assert resp.status_code == 200
        assert len(fake.rpc_calls) == 1
        assert fake.rpc_calls[0]["p_rarities"] == list(app_module._top_page.GMR_RARITIES)
        data = resp.get_json()
        assert data["error"] is None
        items = data["items"]
        assert items[0]["name"] == "差額ありカード"
        assert items[0]["price"] == 10000
        assert items[0]["diff"] == 2000
        assert items[0]["pct"] == 25.0
        assert items[1]["name"] == "差額なしカード"
        assert items[1]["diff"] is None
        assert items[1]["pct"] is None

    def test_invalid_group_rejected(self, monkeypatch, _top_priced_isolated):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-priced?group=xyz")
        assert resp.status_code == 400

    def test_missing_group_rejected(self, monkeypatch, _top_priced_isolated):
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-priced")
        assert resp.status_code == 400

    def test_no_supabase_client_reports_error_not_silent_empty(self, monkeypatch, _top_priced_isolated):
        monkeypatch.setattr(app_module, "_supabase_client", None)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-priced?group=gmr")
        data = resp.get_json()
        assert data["items"] == []
        assert data["error"], "DB未接続時は失敗を示すこと（黙って空にしない）"

    def test_empty_rows_reports_error_not_cached(self, monkeypatch, _top_priced_isolated):
        fake = _FakeSupabaseTopPriced([])
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-priced?group=gmr")
        data = resp.get_json()
        assert data["items"] == []
        assert data["error"], "データが無い時は失敗を示すこと（黙って空にしない）"
        assert app_module._top_priced_cache_time.get("gmr", 0) == 0, "空データはキャッシュされないこと"

    def test_cache_hit_skips_refetch(self, monkeypatch, _top_priced_isolated):
        rows = [_priced_row("差額なしカード", "グランドマスター", 5000)]
        fake = _FakeSupabaseTopPriced(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        client.get("/api/top-priced?group=gmr")
        call_count_after_first = len(fake.rpc_calls)
        client.get("/api/top-priced?group=gmr")
        assert len(fake.rpc_calls) == call_count_after_first, "2回目はキャッシュから返り、RPCを再度呼ばない"

    def test_limit_20_passed_to_rpc(self, monkeypatch, _top_priced_isolated):
        """M-3: limit=20を要求したらRPCのp_limitも20以上でなければ頭打ちになる"""
        rows = [_priced_row(f"カード{i}", "グランドマスター", 5000 - i) for i in range(20)]
        fake = _FakeSupabaseTopPriced(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-priced?group=gmr&limit=20")
        assert resp.status_code == 200
        assert fake.rpc_calls[0]["p_limit"] >= 20
        assert len(resp.get_json()["items"]) == 20

    def test_group_of_rejected(self, monkeypatch, _top_priced_isolated):
        """L-1: get_top_priced の代表レアリティ選定は「カードごとの最安レアリティ」の
        ため複数レアリティが混在する群には不向き。groupはgmrのみ許可する"""
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        resp = client.get("/api/top-priced?group=of")
        assert resp.status_code == 400

    def test_rpc_failure_falls_back_to_stale_cache(self, monkeypatch, _top_priced_isolated):
        """RPC例外時は空を返さず、既存キャッシュがあればそれにフォールバックすること"""
        rows = [_priced_row("差額なしカード", "グランドマスター", 5000)]
        ok_fake = _FakeSupabaseTopPriced(rows)
        monkeypatch.setattr(app_module, "_supabase_client", ok_fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()
        first = client.get("/api/top-priced?group=gmr").get_json()
        assert first["items"], "前提: 1回目は正常にキャッシュされていること"

        fail_fake = _FakeSupabaseTopPriced(exc=RuntimeError("RPC失敗（模擬）"))
        monkeypatch.setattr(app_module, "_supabase_client", fail_fake)
        monkeypatch.setattr(app_module, "_top_priced_cache_time",
                             {"gmr": 0})  # 強制的にキャッシュ失効させ再取得を発生させる
        resp = client.get("/api/top-priced?group=gmr")
        data = resp.get_json()
        assert data["items"] == first["items"], "RPC失敗時は古いキャッシュのitemsをそのまま返すこと"
        assert data["error"] is None, "キャッシュへフォールバックできた場合はerrorを出さないこと"

    def test_diff_null_when_price_and_common_price_differ(self, monkeypatch, _top_priced_isolated):
        """H-2: 主価格(price=全店最安)と差額(price_common基準)の基準が食い違う行
        （別の店由来）は、価格−差額≠7日前 となり読み違えるため差額を出さない"""
        rows = [
            _priced_row("店違いカード", "グランドマスター", price_new=12000,
                        price_new_common=10000, price_old=9000, pct=11.1),
        ]
        fake = _FakeSupabaseTopPriced(rows)
        monkeypatch.setattr(app_module, "_supabase_client", fake)
        app_module.app.config.update(TESTING=True)
        client = app_module.app.test_client()

        resp = client.get("/api/top-priced?group=gmr")
        item = resp.get_json()["items"][0]
        assert item["price"] == 12000
        assert item["price_common"] == 10000, "price_commonはそのまま返す"
        assert item["diff"] is None
        assert item["pct"] is None
        assert item["yesterday"] is None
