"""tests/fake_price_history.py — price_history を読む API のテスト用の偽 Supabase クライアント（ネットワーク不使用）。

本物と同じく card_dim は名前→カード番号、price_history はカード番号の in_ と日付の等号で絞る
（2026-10-22 から購入候補・まとめ買い・レアリティ候補はこの読み方。日付が窓の外の行は返らない）。
test_wish_prices.py と test_shipping.py で使う。
"""

from types import SimpleNamespace


class FakeQuery:
    """card_dim と price_history の select/in_/eq/order/range をたどる最小スタブ。

    本物と同じく card_dim は名前→カード番号、price_history はカード番号の in_ と日付の等号で絞る
    （2026-10-22 から購入候補はこの読み方。日付が窓の外の行は返らない）。
    """

    def __init__(self, table, rows, ids, log=None, daily_rows=None):
        self._table = table
        self._rows = rows
        self._daily_rows = daily_rows or []  # price_daily_min（日ごとの全店最安）の元にする行（店舗別の形）
        self._ids = ids  # 名前 -> カード番号（行のある名前だけ＝card_dim の名簿）
        self._filters = {}
        self._orders = []
        self._log = log

    def select(self, *_a, **_k):
        return self

    def in_(self, col, values):
        self._filters[col] = set(values)
        return self

    def eq(self, col, value):
        self._filters[col] = {value}
        return self

    def gte(self, col, value):
        self._filters[col + ">="] = value
        return self

    def order(self, col, **_k):
        self._orders.append(col)
        return self

    def range(self, start, end):
        self._slice = (start, end)
        return self

    def _matched(self):
        if self._table == "card_dim":
            names = self._filters.get("card_name", set())
            return [{"card_id": cid, "card_name": n} for n, cid in self._ids.items() if n in names]
        if self._table == "rarity_dim":
            return [{"rarity_id": i, "rarity": n} for n, i in _dim(self._daily_rows, "rarity").items()]
        if self._table == "shop_dim":
            return [{"shop_id": i, "shop": n} for n, i in _dim(self._daily_rows, "shop").items()]
        if self._table == "price_daily_min":
            # 本物と同じくカード番号の in_ と日付の下限で絞り、日×カード×レアリティの最安と最安の店に畳んで返す
            ids = self._filters.get("card_id", set())
            since = self._filters.get("recorded_at>=", "")
            rid, sid = _dim(self._daily_rows, "rarity"), _dim(self._daily_rows, "shop")
            groups: dict = {}
            for r in self._daily_rows:
                if self._ids[r["card_name"]] in ids and r["recorded_at"] >= since:
                    groups.setdefault((self._ids[r["card_name"]], rid[r["rarity"]], r["recorded_at"]), []).append(r)
            out = []
            for (cid, rr, day), rs in sorted(groups.items()):
                low = min(x["min_price"] for x in rs)
                over = [x["min_price"] for x in rs if x["min_price"] >= 10]  # 本物の min_price_over10（10円未満を除く）
                out.append({"card_id": cid, "rarity_id": rr, "recorded_at": day, "min_price": low,
                            "min_price_over10": min(over) if over else None,
                            "min_shop_ids": sorted(sid[x["shop"]] for x in rs if x["min_price"] == low)})
            return out
        ids = self._filters.get("card_id", set())
        days = self._filters.get("recorded_at", set())
        # 本物と同じく行にカード番号を入れて返す（購入候補の読み込みはカード番号で行を振り分ける）
        return [{**r, "card_id": self._ids[r["card_name"]]} for r in self._rows
                if self._ids[r["card_name"]] in ids and r["recorded_at"] in days]

    def execute(self):
        if self._log is not None:
            self._log.append((self._table, dict(self._filters), list(self._orders)))
        start, end = getattr(self, "_slice", (0, 999))
        return SimpleNamespace(data=self._matched()[start:end + 1])


def _dim(rows, col):
    """rows の col（rarity / shop）の値に番号を振る（rarity_dim・shop_dim の代わり）"""
    out = {}
    for r in rows:
        out.setdefault(r[col], len(out) + 1)
    return out


def card_ids(rows):
    ids = {}
    for r in rows:
        ids.setdefault(r["card_name"], len(ids) + 1)
    return ids


class FakeClient:
    def __init__(self, rows, daily_rows=None):
        self._rows = rows
        self._daily_rows = daily_rows or []
        self._ids = card_ids(list(rows) + self._daily_rows)
        self.log = []

    def table(self, name):
        return FakeQuery(name, self._rows, self._ids, self.log, self._daily_rows)
