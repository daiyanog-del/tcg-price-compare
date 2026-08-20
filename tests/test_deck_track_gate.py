"""
tests/test_deck_track_gate.py — /api/deck の tracked_cards 辞書ゲートの検証

背景:
  /api/deck はデッキ内カードを自動的に tracked_cards（次回 collect_prices での
  永続化対象）へ登録する。検索自体は全カード名で実行するが、DBへの永続化だけは
  /api/track-batch と同じ辞書ゲート（カード辞書に存在 or 発売済み未発売カード）を
  通す（スパム・誤登録対策、2026-08-20）。

テスト方針:
  test_deck_ignore_duplicates.py と同じ手法（SHOPSスタブ・Threadの同期化）で
  /api/deck を叩き、_track_cards_async に実際に渡された card_names を検証する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


class _SyncThread:
    """threading.Thread の代わりに start() で target を即時同期実行する。"""

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


def _setup(monkeypatch):
    """辞書ゲートの判定材料を完全に制御下に置く（実データファイルへ依存しない）。"""
    monkeypatch.setattr(app_module, "SHOPS", [("テスト店", _fake_scrape)], raising=False)
    monkeypatch.setattr(app_module, "cache_get_shops",
                        lambda name, shops, include_partial=False: ({}, list(shops)))
    monkeypatch.setattr(app_module, "cache_store_shops",
                        lambda name, shop_results, partial_shops=frozenset(): None)
    monkeypatch.setattr(app_module, "Thread", _SyncThread, raising=False)
    monkeypatch.setattr(app_module, "_supabase_client", object(), raising=False)

    # カード名辞書をテスト専用の小さな集合に固定する（_load_cardnames() の実ファイル読込を無効化）
    monkeypatch.setattr(app_module, "_cardnames_loaded", True, raising=False)
    monkeypatch.setattr(app_module, "_cardnames_set", {"既知カード"}, raising=False)
    monkeypatch.setattr(app_module, "_cardnames_fuzzy", {}, raising=False)
    monkeypatch.setattr(app_module, "_cardnames_reading", {}, raising=False)
    monkeypatch.setattr(app_module, "_cardnames_reading_fuzzy", {}, raising=False)

    tracked_calls: list = []
    monkeypatch.setattr(app_module, "_track_cards_async",
                        lambda names: tracked_calls.append(list(names)), raising=False)

    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client(), tracked_calls


# /api/deck は共有のレートリミットバケット（_last_search）を通るため、テストごとに
# 別IPを名乗って（CF-Connecting-IPヘッダ）互いを弾かないようにする
_IP_HEADERS_UNKNOWN = {"CF-Connecting-IP": "10.0.0.1"}
_IP_HEADERS_KNOWN = {"CF-Connecting-IP": "10.0.0.2"}
_IP_HEADERS_RELEASE_PASSED = {"CF-Connecting-IP": "10.0.0.3"}


def test_unknown_card_not_passed_to_track_cards_async(monkeypatch):
    """辞書に存在せず、発売済み未発売カードでもない名前は tracked_cards に渡らない。"""
    monkeypatch.setattr(app_module, "_is_release_passed_unreleased", lambda name: False, raising=False)
    client, tracked_calls = _setup(monkeypatch)

    resp = client.post("/api/deck", data={"cards": "未知カード", "shops": ["テスト店"]},
                        headers=_IP_HEADERS_UNKNOWN)
    assert resp.status_code == 200
    resp.get_data()  # SSEジェネレータを最後まで消費する

    assert tracked_calls == [], f"辞書に無いカードが tracked_cards に渡ってしまった: {tracked_calls}"


def test_known_card_is_passed_to_track_cards_async(monkeypatch):
    """辞書に存在するカードは従来通り tracked_cards に渡る。"""
    monkeypatch.setattr(app_module, "_is_release_passed_unreleased", lambda name: False, raising=False)
    client, tracked_calls = _setup(monkeypatch)

    resp = client.post("/api/deck", data={"cards": "既知カード", "shops": ["テスト店"]},
                        headers=_IP_HEADERS_KNOWN)
    assert resp.status_code == 200
    resp.get_data()

    assert tracked_calls == [["既知カード"]]


def test_release_passed_unreleased_card_is_passed_to_track_cards_async(monkeypatch):
    """辞書（週次インデックス）に未反映でも、発売日到来済みの未発売テーブル登録カードは
    既知カードとして tracked_cards に渡る。"""
    monkeypatch.setattr(app_module, "_is_release_passed_unreleased",
                        lambda name: name == "発売済み未発売カード", raising=False)
    client, tracked_calls = _setup(monkeypatch)

    resp = client.post("/api/deck", data={"cards": "発売済み未発売カード", "shops": ["テスト店"]},
                        headers=_IP_HEADERS_RELEASE_PASSED)
    assert resp.status_code == 200
    resp.get_data()

    assert tracked_calls == [["発売済み未発売カード"]]
