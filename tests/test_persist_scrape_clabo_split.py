"""
tests/test_persist_scrape_clabo_split.py — /api/search 等の即時upsertでカードラボだけ
ignore_duplicates=True に固定されることの検証（監査M1、docs/audit-403-plan-2026-08-08.md）。

背景:
  夜間収集がカードラボだけカタログ巡回（全数網羅）に切り替わったため、_persist_scrape_async
  （/api/search・/api/deck が使う即時upsert）がライブ検索由来の行（取りこぼしあり）で
  同日のカタログ由来の行を上書きすると、系列がギザギザになる。カードラボの行だけ
  ignore_duplicates=True（既存行があれば書かない）に分離し、他店は呼び出し元の指定
  （/api/search=False・/api/deck=True）のまま変えない。

  ネットワーク・実Supabase不使用。app_module._persist_scrape_async を直接呼ぶ。
  price_history_v2 への書き込みは rpc("upsert_price_rows", ...) 経由
  （2026-09-02 整数キー化。price_history_v2.sql）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


class _FakeTable:
    def __init__(self, name: str):
        self.name = name
        self._pending = None

    def execute(self):
        self._pending = None
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self, sink: list):
        self.sink = sink

    def table(self, name):
        return _FakeTable(name)

    def rpc(self, name, params):
        if name == "upsert_price_rows":
            rows = params["p_rows"]
            self.sink.append({
                "shops": {r["shop"] for r in rows},
                "ignore_duplicates": params["p_ignore_duplicates"],
            })
        return _FakeRpc(params)


class _FakeRpc:
    def __init__(self, params: dict):
        self._params = params

    def execute(self):
        # RPC は RETURNS TABLE(saved_rows integer) なので実物どおり list[dict] で返す
        return SimpleNamespace(data=[{"saved_rows": len(self._params["p_rows"])}])


def _item(shop, price=1000):
    return {
        "shop": shop, "name": "青眼の白龍", "rarity": "ウルトラ", "code": "",
        "condition": "-", "price": price, "stock": 1, "sold_out": False,
        "url": "", "image": "",
    }


def test_clabo_rows_are_split_and_forced_to_ignore_duplicates(monkeypatch):
    """混在結果（カードラボ＋他店）は2回のupsertに分かれ、カードラボ側だけTrueになる"""
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)

    scrape_results = [_item("カードラボ"), _item("遊々亭")]
    app_module._persist_scrape_async("青眼の白龍", scrape_results, ignore_duplicates=False)

    assert len(sink) == 2
    by_shops = {frozenset(c["shops"]): c["ignore_duplicates"] for c in sink}
    assert by_shops[frozenset({"カードラボ"})] is True
    assert by_shops[frozenset({"遊々亭"})] is False


def test_clabo_stays_true_even_when_caller_requests_true(monkeypatch):
    """/api/deck 経由（呼び出し元が ignore_duplicates=True）でも他店とカードラボの
    どちらも True のまま（カードラボは固定・他店は呼び出し元の指定どおり）"""
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)

    scrape_results = [_item("カードラボ"), _item("遊々亭")]
    app_module._persist_scrape_async("青眼の白龍", scrape_results, ignore_duplicates=True)

    assert len(sink) == 2
    assert all(c["ignore_duplicates"] is True for c in sink)


def test_no_clabo_results_keeps_single_upsert_call(monkeypatch):
    """カードラボの結果が無ければ従来どおり1回のupsertのまま（回帰防止）"""
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)

    scrape_results = [_item("遊々亭"), _item("まんぞく屋")]
    app_module._persist_scrape_async("青眼の白龍", scrape_results, ignore_duplicates=False)

    assert len(sink) == 1
    assert sink[0]["ignore_duplicates"] is False
    assert sink[0]["shops"] == {"遊々亭", "まんぞく屋"}


def test_only_clabo_results_writes_once_with_true(monkeypatch):
    sink: list = []
    monkeypatch.setattr(app_module, "_supabase_client", _FakeSupabase(sink), raising=False)

    scrape_results = [_item("カードラボ")]
    app_module._persist_scrape_async("青眼の白龍", scrape_results, ignore_duplicates=False)

    assert len(sink) == 1
    assert sink[0]["ignore_duplicates"] is True
    assert sink[0]["shops"] == {"カードラボ"}
