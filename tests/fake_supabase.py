"""テスト用の最小インメモリ Supabase クライアント（unreleased_x_post / auto_approve のテストで共用）。

対応: select / insert / upsert / update / delete / eq / neq / lt / gte / is_ / in_ / or_ / order / range / limit
- insert の主キー衝突は、pks に指定した列で code=23505 の例外を送出する（Supabase の APIError 相当）。
- or_ は日付絞り込み（release_date.is.null,release_date.gte.YYYY-MM-DD）だけを解釈する。
"""
from types import SimpleNamespace


class DuplicateKey(Exception):
    code = "23505"

    def __init__(self):
        super().__init__("duplicate key value violates unique constraint (23505)")


class Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload = "select", None
        self.filters = []
        self._range = None
        self._limit = None
        self._order = None

    def select(self, *_a, **_k):
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        """on_conflict の列（カンマ区切り可）が同じ行があれば、ignore_duplicates なら何もせず、そうでなければ上書き。"""
        self.op, self.payload = "upsert", payload
        self._on_conflict = [c.strip() for c in (on_conflict or "").split(",") if c.strip()]
        self._ignore_dup = ignore_duplicates
        return self

    def delete(self):
        self.op = "delete"
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def neq(self, col, val):
        self.filters.append(lambda r: r.get(col) != val)
        return self

    def lt(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) < val)
        return self

    def gte(self, col, val):
        self.filters.append(lambda r: r.get(col) is not None and r.get(col) >= val)
        return self

    def is_(self, col, val):
        self.filters.append(lambda r: r.get(col) is None)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def or_(self, expr):
        # "release_date.is.null,release_date.gte.2026-10-15" のみ対応
        parts = expr.split(",")
        gte = [p.split(".gte.") for p in parts if ".gte." in p]
        col, val = gte[0]
        self.filters.append(lambda r: r.get(col) is None or r.get(col) >= val)
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def range(self, a, b):
        self._range = (a, b)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        rows = self.db.tables.setdefault(self.name, [])
        if self.op == "insert":
            pk = self.db.pks.get(self.name)
            if pk and any(r.get(pk) == self.payload.get(pk) for r in rows):
                raise DuplicateKey()
            row = dict(self.payload)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        if self.op == "upsert":
            cols = self._on_conflict
            hit = next((r for r in rows if cols and all(r.get(c) == self.payload.get(c) for c in cols)), None)
            if hit is not None:
                if self._ignore_dup:
                    return SimpleNamespace(data=[])
                hit.update(self.payload)
                return SimpleNamespace(data=[dict(hit)])
            row = dict(self.payload)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [r for r in rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in matched:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in matched])
        if self.op == "delete":
            for r in matched:
                rows.remove(r)
            return SimpleNamespace(data=[dict(r) for r in matched])
        if self._order:
            matched.sort(key=lambda r: r.get(self._order[0]), reverse=self._order[1])
        if self._range:
            matched = matched[self._range[0]:self._range[1] + 1]
        if self._limit is not None:
            matched = matched[:self._limit]
        return SimpleNamespace(data=[dict(r) for r in matched])


class FakeSB:
    def __init__(self, tables=None, pks=None):
        self.tables = {k: [dict(r) for r in v] for k, v in (tables or {}).items()}
        self.pks = pks or {"unreleased_x_posts": "card_id", "unreleased_cards": "id"}

    def table(self, name):
        return Query(self, name)
