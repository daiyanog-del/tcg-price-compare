"""
tests/test_deck_ignore_duplicates.py — /api/deck（fresh なし）の記録への書き方。

2026-11-01 に決まりを変えた（以前は全店を ignore_duplicates=True＝その日の行が無いときだけ書いていた）:
  - 全ページを読む店（遊々亭・カードラッシュ・まんぞく屋・カーナベル）: その日の記録に上書き（/api/search と同じ）
  - 浅い店（トレコロの1ページ目・カードラボ）: 新しいカード・当日の値下がり・当日に無いレアリティだけ書き、
    前日の記録だけある深夜は書かない（price_persist.shallow_rows_to_write）
  深夜0時以降の浅い結果がその日の最初の行になり、深い収集の前日の行を押しのけていたため（TASKS の深夜の件）。

テスト方針:
  Flask test_client で /api/deck を叩き、SHOPS をスタブに差し替え、Thread を同期実行にして書き込みを観測する。
  記録の読み出し（fetch_daily_rows_by_card_names）は差し替える。
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


def _fake_scrape_for(shop: str, price: int = 1000, rarity: str = "レア"):
    def fn(card_name: str, max_pages: int = 5) -> list:
        return [{
            "shop": shop, "name": card_name, "rarity": rarity, "code": "",
            "condition": "-", "price": price, "stock": 1, "sold_out": False,
            "url": "", "image": "",
        }]
    return fn


def _run_deck(monkeypatch, shops, existing=None, failed=False, art_today=False):
    """shops: [(店名, スクレイパー)]。existing: 記録の読み出しが返す行（当日・前日）"""
    monkeypatch.setattr(app_module, "SHOPS", shops, raising=False)
    # トレコロは /api/deck が scrape_torecolo を1ページ目だけで呼び直すので、それも差し替える
    for n, fn in shops:
        if n == "トレコロCB":
            import scraper
            monkeypatch.setattr(scraper, "scrape_torecolo", fn)
    monkeypatch.setattr(app_module, "cache_get_shops",
                        lambda name, shops_, include_partial=False: ({}, list(shops_)))
    monkeypatch.setattr(app_module, "cache_store_shops",
                        lambda name, shop_results, partial_shops=frozenset(): None)
    monkeypatch.setattr(app_module, "Thread", _SyncThread, raising=False)
    monkeypatch.setattr(app_module, "_last_deck", {}, raising=False)  # 同じテストで続けて叩いても受付上限に当てない
    monkeypatch.setattr(app_module, "_read_recent_rows",
                        lambda name, days: None if failed else (list(existing or []), [1]))
    monkeypatch.setattr(app_module, "_has_art_rows_today", lambda ids, today: art_today)
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)
    app_module._load_cardnames()
    app_module.app.config.update(TESTING=True)
    client = app_module.app.test_client()
    resp = client.post("/api/deck", data={"cards": "テストカード", "shops": [n for n, _ in shops]})
    assert resp.status_code == 200
    resp.get_data()
    return sink


def _today():
    return app_module.datetime.now(app_module.JST).strftime("%Y-%m-%d")


def _yesterday():
    return (app_module.datetime.now(app_module.JST) - app_module.timedelta(days=1)).strftime("%Y-%m-%d")


def test_deep_shop_overwrites(monkeypatch):
    sink = _run_deck(monkeypatch, [("遊々亭", _fake_scrape_for("遊々亭"))])
    assert [c["p_ignore_duplicates"] for c in sink] == [False]
    assert sink[0]["p_rows"][0]["min_price"] == 1000


def test_kanabell_is_no_longer_first_page_only(monkeypatch):
    """カーナベルは全件を読む店になり、上書きする"""
    sink = _run_deck(monkeypatch, [("カーナベル", _fake_scrape_for("カーナベル"))])
    assert [c["p_ignore_duplicates"] for c in sink] == [False]


def test_shallow_shop_writes_new_card(monkeypatch):
    """当日も前日も記録が無い（新しいカード）なら浅い店でも書く（上書きしない書き方）"""
    sink = _run_deck(monkeypatch, [("トレコロCB", _fake_scrape_for("トレコロCB"))], existing=[])
    assert len(sink) == 1 and sink[0]["p_rows"][0]["shop"] == "トレコロCB" and sink[0]["p_ignore_duplicates"] is True


def test_shallow_shop_skips_when_only_yesterday(monkeypatch):
    """深夜（前日の記録だけ）は浅い結果を書かない"""
    old = {"card_name": "テストカード", "shop": "トレコロCB", "rarity": "レア", "min_price": 800, "min_price_any": 800,
           "code": "", "url": None, "recorded_at": _yesterday()}
    sink = _run_deck(monkeypatch, [("トレコロCB", _fake_scrape_for("トレコロCB", price=500))], existing=[old])
    assert sink == []


def test_shallow_shop_lowers_today(monkeypatch):
    """当日の記録より安ければそのレアリティだけ下げる。高ければ書かない"""
    old = {"card_name": "テストカード", "shop": "カードラボ", "rarity": "レア", "min_price": 800, "min_price_any": 800,
           "code": "", "url": None, "recorded_at": _today()}
    sink = _run_deck(monkeypatch, [("カードラボ", _fake_scrape_for("カードラボ", price=500))], existing=[old])
    assert len(sink) == 1 and sink[0]["p_rows"][0]["min_price_any"] == 500 and sink[0]["p_ignore_duplicates"] is False
    sink = _run_deck(monkeypatch, [("カードラボ", _fake_scrape_for("カードラボ", price=900))], existing=[old])
    assert sink == []


def test_shallow_shop_skips_when_records_unreadable(monkeypatch):
    sink = _run_deck(monkeypatch, [("トレコロCB", _fake_scrape_for("トレコロCB"))], failed=True)
    assert sink == []


def test_shallow_lower_skipped_when_art_rows_exist(monkeypatch):
    """当日の絵ごとの内訳があるカードは値下げを書かない（本表の上書きが内訳を消すため。reviewer 高2）"""
    old = {"card_name": "テストカード", "shop": "カードラボ", "rarity": "レア", "min_price": 800, "min_price_any": 800,
           "code": "", "url": None, "recorded_at": _today()}
    sink = _run_deck(monkeypatch, [("カードラボ", _fake_scrape_for("カードラボ", price=500))], existing=[old], art_today=True)
    assert sink == []


def test_deep_and_shallow_mixed(monkeypatch):
    """全ページの店は上書き、浅い店は深夜（前日だけ）なら書かない"""
    old = {"card_name": "テストカード", "shop": "トレコロCB", "rarity": "レア", "min_price": 800, "min_price_any": 800,
           "code": "", "url": None, "recorded_at": _yesterday()}
    sink = _run_deck(monkeypatch, [("遊々亭", _fake_scrape_for("遊々亭")), ("トレコロCB", _fake_scrape_for("トレコロCB"))],
                     existing=[old])
    assert [({r["shop"] for r in c["p_rows"]}, c["p_ignore_duplicates"]) for c in sink] == [({"遊々亭"}, False)]


def test_search_route_clabo_follows_shallow_rule(monkeypatch):
    """カードのページの検索（_persist_scrape_async）でも、カードラボは深夜（前日だけ）なら書かない（reviewer 高1）"""
    old = {"card_name": "テストカード", "shop": "カードラボ", "rarity": "レア", "min_price": 800, "min_price_any": 800,
           "code": "", "url": None, "recorded_at": _yesterday()}
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)
    monkeypatch.setattr(app_module, "_read_recent_rows", lambda name, days: ([old], [1]))
    monkeypatch.setattr(app_module, "_has_art_rows_today", lambda ids, today: False)
    app_module._persist_scrape_async("テストカード", _fake_scrape_for("カードラボ", price=500)("テストカード")
                                     + _fake_scrape_for("遊々亭")("テストカード"))
    assert [({r["shop"] for r in c["p_rows"]}, c["p_ignore_duplicates"]) for c in sink] == [({"遊々亭"}, False)]


