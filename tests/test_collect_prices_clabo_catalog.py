"""
tests/test_collect_prices_clabo_catalog.py — collect_prices.py のカードラボ配線検証

背景:
  docs/audit-403-plan-2026-08-08.md（P1・カタログ巡回）＋ reviewer監査（高3/中1/中3/中4）。
  夜間収集は `_prepare_clabo_catalog`（ループ前）と `_save_clabo_catalog_rows`
  （ループ後）の2段階に分かれている:
    - _prepare_clabo_catalog: 成立なら loop_shops（カード単位ループ専用の店舗リスト）
      からカードラボを外す。available_shops 自体は変更しない（record_collection_run に
      渡す観測店舗集合はカードラボ入りのまま。reviewer中1）
    - 不成立・対象外・例外時: loop_shops は変更せず、従来のカード単位の検索方式
      （collect_and_save 経由）へフォールバックする（reviewer高3）
    - _save_clabo_catalog_rows: 実際にカード単位ループが処理したカードだけを対象に
      保存する（時間予算打ち切りで処理されなかったカードには書かない。reviewer中3）。
      upsertはチャンク化する（reviewer中4）

  ネットワークには一切出ない（clabo_catalog.collect_clabo_catalog をモックする）。
  price_history_v2 への書き込みは rpc("upsert_price_rows", ...) 経由
  （2026-09-02 整数キー化。price_history_v2.sql）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import collect_prices as cp


class _FakeTable:
    """price_history 以外のテーブル操作（eq/update等）を模倣する最小フェイク"""

    def __init__(self, name: str, state: dict):
        self.name = name
        self.state = state

    def eq(self, *a, **k):
        return self

    def update(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeRpc:
    def __init__(self, state: dict, rows):
        self.state = state
        self.rows = rows

    def execute(self):
        self.state.setdefault("price_history", {}).setdefault("upserted", []).extend(self.rows)
        self.state.setdefault("price_history", {}).setdefault("upsert_calls", 0)
        self.state["price_history"]["upsert_calls"] += 1
        # RPC は RETURNS TABLE(saved_rows integer) なので実物どおり list[dict] で返す
        return SimpleNamespace(data=[{"saved_rows": len(self.rows)}])


class _FakeSupabase:
    def __init__(self, state: dict):
        self.state = state

    def table(self, name: str):
        return _FakeTable(name, self.state)

    def rpc(self, name, params):
        assert name == "upsert_price_rows"
        return _FakeRpc(self.state, params["p_rows"])


def _selected(names):
    return [{"card_name": n, "last_collected_at": None} for n in names]


def _product(name="青眼の白龍", price=500):
    return {"shop": "カードラボ", "name": name, "rarity": "ウルトラ", "code": "TEST-JP001",
            "condition": "-", "price": price, "stock": 3, "sold_out": False, "url": "", "image": ""}


def _catalog_stats(**overrides):
    stats = {
        "categories_total": 14, "categories_failed": [], "pages_fetched": 14,
        "products": 1, "failed_pages": 0, "categories": {},
        "matched_products": 1, "unmatched_products": 0, "multi_match_products": 0,
        "category_drift": [],
    }
    stats.update(overrides)
    return stats


class TestPrepareClaboCatalogSuccess:
    def test_removes_clabo_from_loop_shops_but_not_available_shops(self, monkeypatch):
        """reviewer中1: available_shops自体は変更しない。loop_shopsだけ外す"""
        products = [_product()]
        monkeypatch.setattr(
            cp, "collect_clabo_catalog",
            lambda names: ({"青眼の白龍": products}, _catalog_stats()),
        )

        sb = _FakeSupabase({})
        selected = _selected(["青眼の白龍", "死者蘇生"])
        available_shops = ["遊々亭", "カードラボ", "まんぞく屋"]
        shop_stats = {}

        loop_shops, matched, catalog_stats = cp._prepare_clabo_catalog(
            selected, available_shops, shop_stats
        )

        assert "カードラボ" not in loop_shops
        assert loop_shops == ["遊々亭", "まんぞく屋"]
        assert available_shops == ["遊々亭", "カードラボ", "まんぞく屋"]  # 不変
        assert matched == {"青眼の白龍": products}
        assert catalog_stats["products"] == 1
        # 保存フェーズはまだ走っていない（shop_statsは未確定）
        assert "カードラボ" not in shop_stats


class TestSaveClaboCatalogRows:
    def test_saves_rows_and_counts_ok_empty(self, monkeypatch):
        products = [_product()]
        matched = {"青眼の白龍": products}
        catalog_stats = _catalog_stats()

        state = {}
        sb = _FakeSupabase(state)
        processed = _selected(["青眼の白龍", "死者蘇生"])  # 死者蘇生はカタログに0件
        shop_stats = {}

        saved = cp._save_clabo_catalog_rows(sb, processed, matched, "2026-08-09", shop_stats, catalog_stats)

        assert saved == 1
        assert state["price_history"]["upserted"][0]["card_name"] == "青眼の白龍"
        assert shop_stats["カードラボ"]["mode"] == "catalog"
        assert shop_stats["カードラボ"]["ok"] == 1
        assert shop_stats["カードラボ"]["empty"] == 1
        assert shop_stats["カードラボ"]["error"] == 0
        assert shop_stats["カードラボ"]["catalog"] == catalog_stats

    def test_does_not_write_rows_for_cards_outside_processed(self):
        """reviewer中3: processed（実際にループが処理したカード）以外には書かない。
        selected全体ではなく processed を渡すのが呼び出し側の責務"""
        products = [_product()]
        matched = {
            "青眼の白龍": products,
            "打ち切られたカード": products,   # processed に含まれない＝打ち切り後
        }
        catalog_stats = _catalog_stats(matched_products=2)

        state = {}
        sb = _FakeSupabase(state)
        processed = _selected(["青眼の白龍"])   # 打ち切られたカードは processed に無い
        shop_stats = {}

        cp._save_clabo_catalog_rows(sb, processed, matched, "2026-08-09", shop_stats, catalog_stats)

        saved_names = {r["card_name"] for r in state["price_history"]["upserted"]}
        assert saved_names == {"青眼の白龍"}
        assert shop_stats["カードラボ"]["ok"] == 1

    def test_upsert_is_chunked(self, monkeypatch):
        """reviewer中4: 全カード分の行をまとめてチャンク化して upsert する"""
        monkeypatch.setattr(cp, "_UPSERT_CHUNK_SIZE", 2)

        matched = {f"カード{i}": [_product(name=f"カード{i}")] for i in range(5)}
        catalog_stats = _catalog_stats(matched_products=5)

        state = {}
        sb = _FakeSupabase(state)
        processed = _selected(list(matched.keys()))
        shop_stats = {}

        saved = cp._save_clabo_catalog_rows(sb, processed, matched, "2026-08-09", shop_stats, catalog_stats)

        assert saved == 5
        assert len(state["price_history"]["upserted"]) == 5
        # 5行をチャンクサイズ2で分割 → 3回のupsert呼び出し（2+2+1）
        assert state["price_history"]["upsert_calls"] == 3

    def test_dedups_rows_with_same_key_before_upsert(self):
        """reviewer低6: (card_name, shop, rarity, recorded_at) が重複する行が
        チャンクにまたがって混ざると ON CONFLICT の二重更新でチャンクごとエラーになるため、
        チャンク分割の前に一意化しておく（保険。現状は到達不能想定）"""
        products = [_product()]
        matched = {"青眼の白龍": products}
        catalog_stats = _catalog_stats()

        state = {}
        sb = _FakeSupabase(state)
        # processed に同じカードが2回現れるケース（実運用では起きない想定の保険）
        processed = _selected(["青眼の白龍", "青眼の白龍"])
        shop_stats = {}

        saved = cp._save_clabo_catalog_rows(sb, processed, matched, "2026-08-09", shop_stats, catalog_stats)

        assert saved == 1
        assert len(state["price_history"]["upserted"]) == 1


class TestPrepareClaboCatalogFallback:
    def test_keeps_loop_shops_unchanged_when_catalog_not_ok(self, monkeypatch):
        catalog_stats = _catalog_stats(categories_failed=["672"], failed_pages=1)
        monkeypatch.setattr(cp, "collect_clabo_catalog", lambda names: (None, catalog_stats))

        sb = _FakeSupabase({})
        selected = _selected(["青眼の白龍"])
        available_shops = ["遊々亭", "カードラボ"]
        shop_stats = {}

        loop_shops, matched, returned_stats = cp._prepare_clabo_catalog(
            selected, available_shops, shop_stats
        )

        assert loop_shops == available_shops   # 変更なし＝カードラボが残る
        assert matched is None
        assert returned_stats is None

        assert shop_stats["カードラボ"] == {
            "ok": 0, "empty": 0, "error": 0,
            "mode": "fallback", "catalog": catalog_stats,
        }

    def test_fallback_stats_are_mutated_in_place_by_per_card_loop(self, monkeypatch):
        """不成立時、shop_stats の枠は先にここで作るだけで、実際の集計は
        collect_and_save（カード単位ループ）が従来どおり書き込む（setdefaultで
        上書きされず、既存の枠に加算されることを確認）"""
        catalog_stats = _catalog_stats(categories_failed=["672"], failed_pages=1)
        monkeypatch.setattr(cp, "collect_clabo_catalog", lambda names: (None, catalog_stats))

        sb = _FakeSupabase({})
        selected = _selected(["青眼の白龍"])
        available_shops = ["カードラボ"]
        shop_stats = {}

        loop_shops, matched, _ = cp._prepare_clabo_catalog(selected, available_shops, shop_stats)
        assert "カードラボ" in loop_shops
        assert matched is None

        # カード単位の検索方式（collect_and_save）がカードラボを叩いて0件だった想定
        def fake_compare_prices(card_name, shop_names, status_out=None):
            if status_out is not None:
                status_out["カードラボ"] = {"count": 0, "fetch_errors": 0}
            return []

        monkeypatch.setattr(cp, "compare_prices", fake_compare_prices)
        cp.collect_and_save(sb, "青眼の白龍", "2026-08-09", loop_shops, shop_stats=shop_stats)

        # mode/catalog は保持されたまま、ok/empty/error だけが加算されている
        assert shop_stats["カードラボ"]["mode"] == "fallback"
        assert shop_stats["カードラボ"]["catalog"] == catalog_stats
        assert shop_stats["カードラボ"]["empty"] == 1


class TestPrepareClaboCatalogException:
    def test_exception_falls_back_without_changing_loop_shops(self, monkeypatch):
        """reviewer高3: collect_clabo_catalog が例外を投げても、ループは
        従来どおり回る（loop_shopsが変更されない）"""
        def _raise(names):
            raise RuntimeError("ネットワーク的な何か")

        monkeypatch.setattr(cp, "collect_clabo_catalog", _raise)

        sb = _FakeSupabase({})
        selected = _selected(["青眼の白龍"])
        available_shops = ["遊々亭", "カードラボ"]
        shop_stats = {}

        loop_shops, matched, catalog_stats = cp._prepare_clabo_catalog(
            selected, available_shops, shop_stats
        )

        assert loop_shops == available_shops
        assert matched is None
        assert catalog_stats is None
        assert shop_stats["カードラボ"]["mode"] == "error"
        assert "ネットワーク的な何か" in shop_stats["カードラボ"]["exception"]
        assert shop_stats["カードラボ"]["ok"] == 0

    def test_loop_still_processes_cards_after_exception(self, monkeypatch):
        """例外後もカード単位ループが従来どおり回ることを確認"""
        monkeypatch.setattr(
            cp, "collect_clabo_catalog",
            lambda names: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        sb = _FakeSupabase({})
        selected = _selected(["青眼の白龍"])
        shop_stats = {}
        loop_shops, matched, _ = cp._prepare_clabo_catalog(selected, ["カードラボ"], shop_stats)
        assert matched is None

        called = []

        def fake_compare_prices(card_name, shop_names, status_out=None):
            called.append((card_name, list(shop_names)))
            if status_out is not None:
                status_out["カードラボ"] = {"count": 1, "fetch_errors": 0}
            return [_product()]

        monkeypatch.setattr(cp, "compare_prices", fake_compare_prices)
        saved = cp.collect_and_save(sb, "青眼の白龍", "2026-08-09", loop_shops, shop_stats=shop_stats)

        assert called == [("青眼の白龍", ["カードラボ"])]
        assert saved == 1


class TestPrepareClaboCatalogSkipped:
    def test_does_nothing_when_clabo_not_available(self, monkeypatch):
        def _should_not_be_called(names):
            raise AssertionError("カードラボが available_shops に無いのに呼ばれた")

        monkeypatch.setattr(cp, "collect_clabo_catalog", _should_not_be_called)

        sb = _FakeSupabase({})
        selected = _selected(["青眼の白龍"])
        available_shops = ["遊々亭", "まんぞく屋"]
        shop_stats = {}

        loop_shops, matched, catalog_stats = cp._prepare_clabo_catalog(
            selected, available_shops, shop_stats
        )

        assert loop_shops == available_shops
        assert matched is None
        assert catalog_stats is None
        assert shop_stats == {}
