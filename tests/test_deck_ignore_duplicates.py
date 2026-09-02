"""
tests/test_deck_ignore_duplicates.py — /api/deck 経路の即時upsertがignore_duplicates=Trueで
呼ばれることの検証（フェーズ3 P6b、docs/design-phase3-generation-side.md 「P6b: 即時upsertと
夜間収集のlast-write-wins競合」推奨案B）。

背景:
  /api/deck はトレコロ・カーナベルを1ページ目に制限したスクレイプのため、網羅性の低い
  高めの min を作ることがある。夜間収集の網羅的な min を同日上書きしないよう、
  /api/deck 経由の即時upsertのみ ignore_duplicates=True にする。/api/search は
  全ページ取得のため対象外（既定の False のまま last-write-wins を維持）。

テスト方針:
  Flask test_client で /api/deck を叩き、SHOPS をスタブ1店舗に差し替え、
  Thread を同期実行に差し替えて fire-and-forget を即時観測可能にする。
  fake Supabase client の rpc("upsert_price_rows", ...) 呼び出しに渡された
  p_ignore_duplicates 引数を検証する（ネットワーク・実Supabase不使用、
  2026-09-02 整数キー化で upsert() から rpc() 経由に変更。price_history_v2.sql）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


class _FakeTable:
    """任意のチェーンメソッドを self を返すだけで模倣し、execute() は空データを返す。"""

    def __init__(self, name: str):
        self.name = name
        self._pending = None

    def __getattr__(self, item):
        # select/eq/gte/order/range/in_ など未定義の全チェーンメソッドを self 返しで許容
        def _chain(*a, **k):
            return self
        return _chain

    def insert(self, rows):
        self._pending = rows
        return self

    def execute(self):
        from types import SimpleNamespace
        self._pending = None
        return SimpleNamespace(data=[])


class _FakeRpc:
    def __init__(self, params: dict):
        self._params = params

    def execute(self):
        from types import SimpleNamespace
        # RPC は RETURNS TABLE(saved_rows integer) なので実物どおり list[dict] で返す
        return SimpleNamespace(data=[{"saved_rows": len(self._params["p_rows"])}])


class _FakeSupabase:
    """price_history_v2 への書き込みは rpc("upsert_price_rows", ...) 経由になった
    （2026-09-02 整数キー化）。rpc() 呼び出しだけ引数を sink に記録する。"""

    def __init__(self, sink: list):
        self.sink = sink

    def table(self, name):
        return _FakeTable(name)

    def rpc(self, name, params):
        if name == "upsert_price_rows":
            self.sink.append(params)
        return _FakeRpc(params)


class _SyncThread:
    """threading.Thread の代わりに start() で target を即時同期実行する。
    fire-and-forget のタイミング依存を排除してテストを決定的にする。"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _fake_scrape(card_name: str) -> list:
    return [{
        "shop": "テスト店", "name": card_name, "rarity": "レア", "code": "",
        "condition": "-", "price": 1000, "stock": 1, "sold_out": False,
        "url": "", "image": "",
    }]


def test_deck_route_persists_with_ignore_duplicates_true(monkeypatch):
    monkeypatch.setattr(app_module, "SHOPS", [("テスト店", _fake_scrape)], raising=False)
    monkeypatch.setattr(app_module, "cache_get_shops",
                        lambda name, shops, include_partial=False: ({}, list(shops)))
    monkeypatch.setattr(app_module, "cache_store_shops",
                        lambda name, shop_results, partial_shops=frozenset(): None)
    monkeypatch.setattr(app_module, "Thread", _SyncThread, raising=False)

    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)

    app_module._load_cardnames()
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()

    resp = client.post("/api/deck", data={"cards": "テストカード", "shops": ["テスト店"]})
    assert resp.status_code == 200
    resp.get_data()  # SSEジェネレータを最後まで消費する

    assert sink, "rpc('upsert_price_rows', ...) が呼ばれていない"
    call = sink[-1]
    assert call["p_ignore_duplicates"] is True
    assert call["p_rows"][0]["min_price"] == 1000
