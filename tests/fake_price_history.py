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

    def __init__(self, table, rows, ids, log=None):
        self._table = table
        self._rows = rows
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
        ids = self._filters.get("card_id", set())
        days = self._filters.get("recorded_at", set())
        return [r for r in self._rows if self._ids[r["card_name"]] in ids and r["recorded_at"] in days]

    def execute(self):
        if self._log is not None:
            self._log.append((self._table, dict(self._filters), list(self._orders)))
        start, end = getattr(self, "_slice", (0, 999))
        return SimpleNamespace(data=self._matched()[start:end + 1])


def card_ids(rows):
    ids = {}
    for r in rows:
        ids.setdefault(r["card_name"], len(ids) + 1)
    return ids


class FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self._ids = card_ids(rows)
        self.log = []

    def table(self, name):
        return FakeQuery(name, self._rows, self._ids, self.log)
