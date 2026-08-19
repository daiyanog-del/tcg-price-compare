"""
tests/test_deck_per_shop_rarity.py — /api/deck の per_shop（include_per_shop=1）が
店舗別集計でレアリティを潰すバグ（2026-08-19）の回帰テスト（サーバ側）。

背景:
  app.py の _aggregate_per_shop() は「1店につき最安1件」に per_shop を畳んでいた。
  ある店がノーマル¥100とウルトラ¥3,000を両方在庫していても、返るのはノーマル¥100だけ。
  クライアント側（templates/index.html）はレアリティ指定があるとき「その1件が指定
  レアリティと一致するか」で判定するため、実際にはウルトラを在庫している店が
  「持っていない」と誤判定されていた（実害＝見積もりで取得できなかった扱いになり
  総額から抜ける／まとめ買いでその店が候補から外れる）。

  修正後は per_shop を {店舗名: {レアリティ: {price, url, ...}}} の入れ子にし、
  店舗ごとに複数レアリティを持てるようにした。

テスト方針:
  test_deck_ignore_duplicates.py と同じパターンで、Flask test_client + SHOPS スタブ +
  同期 Thread で /api/deck?include_per_shop=1 を叩き、SSE の per_shop payload を検証する。
  ネットワーク・実Supabase不使用。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


class _SyncThread:
    """threading.Thread の代わりに start() で target を即時同期実行する
    （test_deck_ignore_duplicates.py と同じヘルパ）"""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _item(shop, rarity, price, url, sold_out=False):
    return {
        "shop": shop, "name": "テストカード", "rarity": rarity, "code": "",
        "condition": "-", "price": price, "stock": 1, "sold_out": sold_out,
        "url": url, "image": "",
    }


def _fetch_sse_payload(client, **query):
    """/api/deck の SSE レスポンスから card_done イベントの payload を1件取り出す"""
    app_module._last_search.clear()  # レートリミット回避（同一テスト内で複数回叩くため）
    resp = client.get("/api/deck", query_string=query)
    assert resp.status_code == 200
    data = resp.get_data(as_text=True)
    for chunk in data.split("\n\n"):
        if not chunk.startswith("data: "):
            continue
        d = json.loads(chunk[len("data: "):])
        if d.get("type") == "card_done":
            return d
    raise AssertionError("card_done イベントが見つかりませんでした: " + data)


def _fake_scrape_yuyu(card_name: str) -> list:
    # 同一店舗が複数レアリティを在庫している再現ケース（バグ本体）
    return [
        _item("遊々亭", "ノーマル", 100, "u-normal"),
        _item("遊々亭", "ウルトラ", 3000, "u-ultra"),
    ]


def _fake_scrape_clabo(card_name: str) -> list:
    return [
        # 同一店舗・同一レアリティの複数ヒット → 最安(2500)を採用すること
        _item("カードラボ", "ウルトラ", 2800, "c-ultra-high"),
        _item("カードラボ", "ウルトラ", 2500, "c-ultra-low"),
        # sold_out は除外されること
        _item("カードラボ", "ノーマル", 50, "c-normal-soldout", sold_out=True),
        # レアリティ不明（空文字）の商品も "" キーで残ること
        _item("カードラボ", "", 400, "c-unknown-rarity"),
    ]


def _setup(monkeypatch):
    monkeypatch.setattr(
        app_module, "SHOPS",
        [("遊々亭", _fake_scrape_yuyu), ("カードラボ", _fake_scrape_clabo)],
        raising=False,
    )
    monkeypatch.setattr(
        app_module, "cache_get_shops",
        lambda name, shops, include_partial=False: ({}, list(shops)),
    )
    monkeypatch.setattr(
        app_module, "cache_store_shops",
        lambda name, shop_results, partial_shops=frozenset(): None,
    )
    monkeypatch.setattr(app_module, "Thread", _SyncThread, raising=False)
    monkeypatch.setattr(app_module, "_supabase_client", None, raising=False)
    app_module._load_cardnames()
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_per_shop_keeps_multiple_rarities_for_same_shop(monkeypatch):
    """再現ケース: 同一店舗が複数レアリティを在庫していれば、per_shop に両方残ること
    （旧実装は最安の1レアリティしか残らず、ウルトラ指定の判定が「無い」になっていた）"""
    client = _setup(monkeypatch)
    payload = _fetch_sse_payload(
        client, cards="テストカード", shops=["遊々亭"], include_per_shop="1",
    )
    per_shop = payload["per_shop"]
    assert set(per_shop.keys()) == {"遊々亭"}
    yuyu = per_shop["遊々亭"]
    assert set(yuyu.keys()) == {"ノーマル", "ウルトラ"}, "両方のレアリティが残ること"
    assert yuyu["ノーマル"]["price"] == 100
    assert yuyu["ウルトラ"]["price"] == 3000
    assert yuyu["ウルトラ"]["url"] == "u-ultra"


def test_per_shop_picks_min_price_within_same_rarity(monkeypatch):
    """同一店舗・同一レアリティで複数ヒットしたら最安を採ること（各レアリティ内に適用）"""
    client = _setup(monkeypatch)
    payload = _fetch_sse_payload(
        client, cards="テストカード", shops=["カードラボ"], include_per_shop="1",
    )
    per_shop = payload["per_shop"]
    clabo = per_shop["カードラボ"]
    assert clabo["ウルトラ"]["price"] == 2500, "同一レアリティ内の最安が採られること"


def test_per_shop_excludes_sold_out(monkeypatch):
    """sold_out の除外は維持されること"""
    client = _setup(monkeypatch)
    payload = _fetch_sse_payload(
        client, cards="テストカード", shops=["カードラボ"], include_per_shop="1",
    )
    clabo = payload["per_shop"]["カードラボ"]
    assert "ノーマル" not in clabo, "sold_out のレアリティは per_shop に残らないこと"


def test_per_shop_keeps_unknown_rarity_as_empty_key(monkeypatch):
    """レアリティが空・不明の商品は \"\" キーで残ること（捨てない）"""
    client = _setup(monkeypatch)
    payload = _fetch_sse_payload(
        client, cards="テストカード", shops=["カードラボ"], include_per_shop="1",
    )
    clabo = payload["per_shop"]["カードラボ"]
    assert "" in clabo, "レアリティ不明の商品が \"\" キーで残ること"
    assert clabo[""]["price"] == 400


def test_per_shop_filters_to_selected_shops(monkeypatch):
    """selected（クエリの shops）に絞り込む挙動は維持されること"""
    client = _setup(monkeypatch)
    payload = _fetch_sse_payload(
        client, cards="テストカード", shops=["遊々亭"], include_per_shop="1",
    )
    per_shop = payload["per_shop"]
    assert "カードラボ" not in per_shop, "selected に無い店舗は per_shop に含まれないこと"
