"""
tests/test_shipping.py — 送料・最低注文金額ルールのテスト

背景（2026-08-05）:
  まとめ買い比較（/api/wish-shop-totals）が商品代金の合計だけで並べていたため、
  次の2点を見誤っていた。
    1. 送料。購入総額が一定額を超えると無料になる店舗がある（しきい値は店舗ごとに別）
    2. 最低注文金額。遊々亭は1,000円未満、カーナベルは2,000円未満だと注文自体できない。
       とくに遊々亭は送料が常に無料なので、少額のリストでは
       「¥0で1位なのに実際は買えない」状態になっていた。

テスト方針: ネットワーク不使用。shipping.py の純粋関数と、
  fake Supabase クライアント経由の API レスポンスで検証する。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shipping  # noqa: E402


# ---------------------------------------------------------------- 純粋関数

class TestCalcShipping:
    def test_遊々亭は常に送料無料(self):
        assert shipping.calc_shipping("遊々亭", 100) == 0
        assert shipping.calc_shipping("遊々亭", 100000) == 0

    def test_カードラッシュは5000円で無料になる(self):
        assert shipping.calc_shipping("カードラッシュ", 4999) == 180
        assert shipping.calc_shipping("カードラッシュ", 5000) == 0
        assert shipping.calc_shipping("カードラッシュ", 5001) == 0

    def test_トレコロは3000円で無料になる(self):
        assert shipping.calc_shipping("トレコロCB", 2999) == 150
        assert shipping.calc_shipping("トレコロCB", 3000) == 0

    def test_無料条件が無い店舗は金額によらず定額(self):
        # カードラボ・まんぞく屋・カーナベルは無料条件の記載が無い
        for shop, fee in (("カードラボ", 290), ("まんぞく屋", 245), ("カーナベル", 100)):
            assert shipping.calc_shipping(shop, 100) == fee
            assert shipping.calc_shipping(shop, 1000000) == fee

    def test_未登録店舗は送料不明(self):
        # 駿河屋など DEFAULT_SHOPS 外の店舗。0 円と誤認しないよう None を返す
        assert shipping.calc_shipping("駿河屋", 5000) is None


class TestAmountToFreeShipping:
    def test_あといくらで無料かを返す(self):
        assert shipping.amount_to_free_shipping("カードラッシュ", 4800) == 200
        assert shipping.amount_to_free_shipping("トレコロCB", 1000) == 2000

    def test_到達済みならNone(self):
        assert shipping.amount_to_free_shipping("カードラッシュ", 5000) is None
        assert shipping.amount_to_free_shipping("遊々亭", 0) is None

    def test_無料条件が無い店舗はNone(self):
        assert shipping.amount_to_free_shipping("カードラボ", 100) is None
        assert shipping.amount_to_free_shipping("駿河屋", 100) is None


class TestMinOrder:
    def test_遊々亭は1000円未満だと注文できない(self):
        assert shipping.is_orderable("遊々亭", 999) is False
        assert shipping.amount_to_min_order("遊々亭", 999) == 1
        assert shipping.is_orderable("遊々亭", 1000) is True
        assert shipping.amount_to_min_order("遊々亭", 1000) is None

    def test_カーナベルは2000円未満だと注文できない(self):
        assert shipping.is_orderable("カーナベル", 1999) is False
        assert shipping.amount_to_min_order("カーナベル", 1999) == 1
        assert shipping.is_orderable("カーナベル", 2000) is True

    def test_最低注文金額が無い店舗は常に注文可(self):
        for shop in ("カードラッシュ", "トレコロCB", "カードラボ", "まんぞく屋"):
            assert shipping.is_orderable(shop, 1) is True
            assert shipping.amount_to_min_order(shop, 1) is None

    def test_1枚も揃わない店舗は最低注文金額の判定対象外(self):
        # 商品代金0＝「買う物が無い」。金額不足として警告を出すのは誤り
        assert shipping.is_orderable("遊々亭", 0) is True
        assert shipping.amount_to_min_order("遊々亭", 0) is None
        assert shipping.amount_to_min_order("カーナベル", 0) is None

    def test_1枚も揃わない店舗には送料も付かない(self):
        # 何も買わない店に「送料¥100」「あと¥3,000で送料無料」が出るのは誤り
        assert shipping.calc_shipping("カーナベル", 0) == 0
        assert shipping.calc_shipping("カードラボ", 0) == 0
        assert shipping.amount_to_free_shipping("トレコロCB", 0) is None

    def test_未登録店舗は注文可扱い(self):
        assert shipping.is_orderable("駿河屋", 1) is True


class TestRulesForClient:
    def test_フロントに配る形はJSON化できる値だけ(self):
        rules = shipping.rules_for_client()
        assert set(rules) == set(shipping.SHIPPING_RULES)
        for shop, r in rules.items():
            assert isinstance(r["base_fee"], int)
            assert r["free_threshold"] is None or isinstance(r["free_threshold"], int)
            assert r["min_order"] is None or isinstance(r["min_order"], int)
            assert r["source_url"].startswith("https://")
            assert r["checked_on"] == shipping.SHIPPING_CHECKED_ON

    def test_ルールの店舗名はスクレイパーの店舗名と一致する(self):
        # 表記がズレるとキー突合に失敗し、全店「送料不明」になる
        from scraper import DEFAULT_SHOPS
        assert set(shipping.SHIPPING_RULES) == set(DEFAULT_SHOPS)


# ---------------------------------------------------------------- API 結合

class _FakeQuery:
    """price_history の select/in_/gte/range をたどるだけの最小スタブ"""

    def __init__(self, rows):
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        self._slice = (start, end)
        return self

    def execute(self):
        start, end = getattr(self, "_slice", (0, 999))
        return SimpleNamespace(data=self._rows[start:end + 1])


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


@pytest.fixture
def client(monkeypatch):
    import app as app_module

    def _make(rows):
        monkeypatch.setattr(app_module, "_supabase_client", _FakeClient(rows))
        # カード名補正は素通しにする（cardnames_ja.json は毎週自動更新されるため依存しない）
        monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
        monkeypatch.setattr(app_module, "_correct_cardname", lambda n: n)
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client()

    return _make


def _row(card, shop, price, rarity="ノーマル"):
    return {"card_name": card, "shop": shop, "rarity": rarity, "min_price": price}


def _by_shop(payload):
    return {s["shop"]: s for s in payload["shops"]}


class TestWishShopTotalsShipping:
    def test_送料と送料込み合計が返る(self, client):
        # カードラッシュ 1枚 ¥4,000（無料しきい値5,000に未達）
        c = client([_row("テストカードA", "カードラッシュ", 4000)])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        shops = _by_shop(res.get_json())
        cr = shops["カードラッシュ"]
        assert cr["total"] == 4000
        assert cr["shipping"] == 180
        assert cr["total_with_shipping"] == 4180
        assert cr["free_shipping_at"] == 1000
        # items はエントリとの照合用に希望レアリティ（未指定=""）を持つ。
        # 採用レアリティ（rarity）だけだと未指定エントリの突合が全て外れる
        assert cr["items"][0]["rarity_pref"] == ""
        assert cr["items"][0]["rarity"] == "ノーマル"

    def test_しきい値を超えると送料が消える(self, client):
        c = client([_row("テストカードA", "カードラッシュ", 4000)])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 2}]})  # ¥8,000
        cr = _by_shop(res.get_json())["カードラッシュ"]
        assert cr["total"] == 8000
        assert cr["shipping"] == 0
        assert cr["total_with_shipping"] == 8000
        assert cr["free_shipping_at"] is None

    def test_遊々亭は少額だと注文不可として下位に落ちる(self, client):
        # ¥500 のカード1枚。遊々亭は送料0円で最安に見えるが最低注文金額1,000円に届かない
        c = client([
            _row("テストカードA", "遊々亭", 500),
            _row("テストカードA", "カードラボ", 600),
        ])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        payload = res.get_json()
        shops = _by_shop(payload)

        yuyu = shops["遊々亭"]
        assert yuyu["total"] == 500
        assert yuyu["shipping"] == 0
        assert yuyu["orderable"] is False
        assert yuyu["min_order_short"] == 500

        # 送料込みでは高い（600+290=890 > 500）が、実際に注文できるカードラボが上に来る
        labo = shops["カードラボ"]
        assert labo["orderable"] is True
        assert labo["total_with_shipping"] == 890
        order = [s["shop"] for s in payload["shops"]]
        assert order.index("カードラボ") < order.index("遊々亭")

    def test_最低注文金額を満たせば遊々亭が最安になる(self, client):
        c = client([
            _row("テストカードA", "遊々亭", 1200),
            _row("テストカードA", "カードラボ", 1100),
        ])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        payload = res.get_json()
        shops = _by_shop(payload)
        # 商品代金は遊々亭のほうが高いが、送料込みでは安い（1200 < 1100+290）
        assert shops["遊々亭"]["total_with_shipping"] == 1200
        assert shops["カードラボ"]["total_with_shipping"] == 1390
        assert payload["shops"][0]["shop"] == "遊々亭"

    def test_送料込みで順位が入れ替わる(self, client):
        # 商品代金はまんぞく屋が最安だが、送料を足すとカードラッシュが逆転する
        c = client([
            _row("テストカードA", "まんぞく屋", 4900),
            _row("テストカードA", "カードラッシュ", 5000),
        ])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        payload = res.get_json()
        shops = _by_shop(payload)
        assert shops["まんぞく屋"]["total_with_shipping"] == 5145   # 4900 + 245
        assert shops["カードラッシュ"]["total_with_shipping"] == 5000  # 無料しきい値に到達
        assert payload["shops"][0]["shop"] == "カードラッシュ"

    def test_1枚も揃わない店舗は注文不可にしない(self, client):
        c = client([_row("テストカードA", "カードラボ", 600)])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        yuyu = _by_shop(res.get_json())["遊々亭"]
        assert yuyu["total"] == 0
        assert yuyu["covered_qty"] == 0
        assert yuyu["orderable"] is True   # 金額不足ではなく「買う物が無い」
        assert yuyu["min_order_short"] is None

    def test_1枚も揃わない店舗に送料が付かない(self, client):
        c = client([_row("テストカードA", "カードラボ", 600)])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        kana = _by_shop(res.get_json())["カーナベル"]
        assert kana["total"] == 0
        assert kana["shipping"] == 0            # 何も買わないので送料は発生しない
        assert kana["total_with_shipping"] == 0
        assert kana["free_shipping_at"] is None

    def test_揃う店舗は注文可否より優先される(self, client):
        # 2種類のうち遊々亭は1種類しか無い。金額は足りていても揃う店舗が上に来る
        c = client([
            _row("テストカードA", "遊々亭", 3000),
            _row("テストカードA", "カードラボ", 3000),
            _row("テストカードB", "カードラボ", 3000),
        ])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1},
                                     {"name": "テストカードB", "qty": 1}]})
        payload = res.get_json()
        assert payload["shops"][0]["shop"] == "カードラボ"
        assert _by_shop(payload)["遊々亭"]["orderable"] is True

    def test_送料ルールがレスポンスに含まれる(self, client):
        # フロントはリアルタイム検索で合計が増えたあと、このルールで再計算する
        c = client([_row("テストカードA", "カードラボ", 600)])
        res = c.post("/api/wish-shop-totals",
                     json={"cards": [{"name": "テストカードA", "qty": 1}]})
        payload = res.get_json()
        assert payload["shipping_checked_on"] == shipping.SHIPPING_CHECKED_ON
        assert payload["shipping_rules"]["カードラッシュ"]["free_threshold"] == 5000
        assert payload["shipping_rules"]["遊々亭"]["min_order"] == 1000
