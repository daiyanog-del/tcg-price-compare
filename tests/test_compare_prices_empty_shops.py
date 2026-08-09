"""
tests/test_compare_prices_empty_shops.py — compare_prices/compare_buyback の空店舗ガード
（reviewer低5・中1）

背景:
  低5: shop_names で指定された店舗が SHOPS に1つも一致しない（active が空）場合、
    ThreadPoolExecutor(max_workers=0) が ValueError になっていた。
    compare_buyback には既に同じガードがあり、compare_prices だけ抜けていた。

  中1: `target = shop_names or DEFAULT_SHOPS` は shop_names=[]（明示的な空リスト）を
    偽値として DEFAULT_SHOPS（全店）に化けさせてしまう。カードラボ以外の店舗が
    ヘルスチェック全滅＋カタログ巡回成立の晩、collect_prices.py の loop_shops が
    空リストになるケースで、全カードが未フィルタの DEFAULT_SHOPS 6店舗（403で
    遮断中のカードラボも含む）へ飛ぶ事故になる。None判定に変更して塞いだ。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper
from scraper import compare_prices, compare_buyback


def _forbid_network(monkeypatch):
    """safe_get が呼ばれたら即座にテスト失敗させる（ネットワークに一切出ないことの検証）"""
    def _raise(*a, **k):
        raise AssertionError("safe_get が呼ばれた＝店舗へのフォールバックが発生している")
    monkeypatch.setattr(scraper, "safe_get", _raise)


class TestComparePricesEmptyShops:
    def test_returns_empty_list_when_no_shop_matches(self, monkeypatch):
        """存在しない店舗名だけを渡すと active=[] になり、ThreadPoolExecutor(max_workers=0)
        で ValueError になっていた（低5）"""
        _forbid_network(monkeypatch)
        assert compare_prices("青眼の白龍", shop_names=["存在しない店舗"]) == []

    def test_empty_list_does_not_fall_back_to_default_shops(self, monkeypatch):
        """shop_names=[]（明示的な空リスト）は「対象店舗なし」を意味し、DEFAULT_SHOPS
        （全店）へフォールバックしてはいけない（中1の核心）。ネットワークに一切出ない
        ことを safe_get 呼び出しゼロ件で確認する"""
        _forbid_network(monkeypatch)
        assert compare_prices("青眼の白龍", shop_names=[]) == []

    def test_none_still_falls_back_to_default_shops(self, monkeypatch):
        """shop_names=None（未指定）は従来どおり DEFAULT_SHOPS 相当（＝SHOPS全体）を使う。
        空リストとNoneの意味の違いを固定する回帰テスト。実在の店舗関数は使わず、
        SHOPS/DEFAULT_SHOPS ごとフェイクに差し替えてネットワークを遮断する"""
        calls = []

        def fake_shop_a(card_name):
            calls.append("A")
            return []

        def fake_shop_b(card_name):
            calls.append("B")
            return []

        monkeypatch.setattr(scraper, "SHOPS", [("A", fake_shop_a), ("B", fake_shop_b)])
        monkeypatch.setattr(scraper, "DEFAULT_SHOPS", ["A", "B"])

        result = compare_prices("青眼の白龍", shop_names=None)

        assert result == []
        assert sorted(calls) == ["A", "B"]


class TestCompareBuybackEmptyShops:
    def test_returns_empty_list_when_no_shop_matches(self, monkeypatch):
        _forbid_network(monkeypatch)
        assert compare_buyback("青眼の白龍", shop_names=["存在しない店舗"]) == []

    def test_empty_list_does_not_fall_back_to_default_shops(self, monkeypatch):
        _forbid_network(monkeypatch)
        assert compare_buyback("青眼の白龍", shop_names=[]) == []
