"""
tests/test_admin_unreleased_release_date_guard.py — 未発売カード承認時の
release_date ガードのテスト（admin_unreleased.py）。

背景:
  release_date が NULL のまま承認されると、発売日到来判定
  （app.py の _is_release_passed_unreleased / /api/validate）が永遠に通らず、
  発売日当日になっても未発売扱いのままになる（id=375 の実害）。
  逆に抽出AIの年誤り（過去日）は承認時点から発売済み扱いになる逆方向の誤判定。
  このガードは NULL への補完（同一 product_name の他カードから最頻値）と
  過去日の警告表示を行う（ブロックはしない）。

テスト方針:
  実 Supabase を使わず、unreleased_cards テーブルを模した fake client を
  monkeypatch で差し込む（ネットワーク不使用）。ADMIN_KEY はモジュール属性を
  直接差し替えることで、import 順序に依存せず認証を通す。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_unreleased as admin_module


# ──────────────────────────────────────────────
# fake Supabase client
# ──────────────────────────────────────────────

class _FakeQuery:
    """unreleased_cards テーブル用の簡易クエリビルダ。
    select/update と eq/neq/in_ フィルタ、execute() のみ模倣する。"""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._op = "select"
        self._update_data = None
        self._filters = []

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def update(self, data):
        self._op = "update"
        self._update_data = data
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def neq(self, col, val):
        self._filters.append(("neq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, vals))
        return self

    def _matched(self):
        result = []
        for row in self._rows:
            ok = True
            for typ, col, val in self._filters:
                if typ == "eq" and row.get(col) != val:
                    ok = False
                elif typ == "neq" and row.get(col) == val:
                    ok = False
                elif typ == "in" and row.get(col) not in val:
                    ok = False
            if ok:
                result.append(row)
        return result

    def execute(self):
        matched = self._matched()
        if self._op == "update":
            for row in matched:
                row.update(self._update_data)
        return SimpleNamespace(data=matched)


class _StubTable:
    """unreleased_cards 以外のテーブル（official_card_images等）向けの
    何でも受け流すスタブ。execute() は常に空データを返す。"""

    def __getattr__(self, _item):
        def _chain(*_a, **_k):
            return self
        return _chain

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def table(self, name):
        if name == "unreleased_cards":
            return _FakeQuery(self.rows)
        return _StubTable()


def _install_fake(monkeypatch, rows):
    fake = _FakeSupabase(rows)
    monkeypatch.setattr(admin_module, "_supabase", fake, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    return fake


def _client():
    import app as app_module
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


HEADERS = {"X-Admin-Key": "test-key"}


# ──────────────────────────────────────────────
# _backfill_release_date 単体テスト
# ──────────────────────────────────────────────

def test_backfill_release_date_empty_product_name_returns_none(monkeypatch):
    _install_fake(monkeypatch, [])
    assert admin_module._backfill_release_date("", 1) is None


def test_backfill_release_date_no_match_returns_none(monkeypatch):
    _install_fake(monkeypatch, [
        {"id": 2, "product_name": "商品A", "release_date": None},
    ])
    assert admin_module._backfill_release_date("商品A", 1) is None


def test_backfill_release_date_majority_wins(monkeypatch):
    _install_fake(monkeypatch, [
        {"id": 2, "product_name": "商品A", "release_date": "2026-09-01"},
        {"id": 3, "product_name": "商品A", "release_date": "2026-09-01"},
        {"id": 4, "product_name": "商品A", "release_date": "2026-08-15"},
        {"id": 1, "product_name": "商品A", "release_date": None},  # 自分自身（NULL）
    ])
    assert admin_module._backfill_release_date("商品A", 1) == "2026-09-01"


def test_backfill_release_date_tie_prefers_newest(monkeypatch):
    _install_fake(monkeypatch, [
        {"id": 2, "product_name": "商品A", "release_date": "2026-08-15"},
        {"id": 3, "product_name": "商品A", "release_date": "2026-09-01"},
    ])
    assert admin_module._backfill_release_date("商品A", 1) == "2026-09-01"


def test_backfill_release_date_excludes_self(monkeypatch):
    fake = _install_fake(monkeypatch, [
        {"id": 1, "product_name": "商品A", "release_date": "2026-01-01"},
    ])
    # exclude_id=1 の行しかないので候補なし → None
    assert admin_module._backfill_release_date("商品A", 1) is None


# ──────────────────────────────────────────────
# 単体承認エンドポイントのガード
# ──────────────────────────────────────────────

def test_approve_backfills_null_release_date(monkeypatch):
    rows = [
        {"id": 1, "name": "カードA", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": None, "product_name": "商品X"},
        {"id": 2, "name": "カードB", "source_url": "", "extraction_raw": {},
         "status": "approved", "release_date": "2026-09-01", "product_name": "商品X"},
    ]
    _install_fake(monkeypatch, rows)
    client = _client()

    resp = client.post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["release_date_note"] == "発売日を同商品の他カードから補完しました: 2026-09-01"
    assert "release_date_warning" not in data
    assert rows[0]["release_date"] == "2026-09-01"
    assert rows[0]["status"] == "approved"


def test_approve_warns_when_no_backfill_available(monkeypatch):
    rows = [
        {"id": 1, "name": "カードA", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": None, "product_name": "商品Y"},
    ]
    _install_fake(monkeypatch, rows)
    client = _client()

    resp = client.post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "release_date_warning" in data
    assert "未設定" in data["release_date_warning"]
    assert "release_date_note" not in data
    assert rows[0]["status"] == "approved"
    assert rows[0]["release_date"] is None


def test_approve_warns_on_past_release_date(monkeypatch):
    rows = [
        {"id": 1, "name": "カードA", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": "2020-01-01", "product_name": "商品Z"},
    ]
    _install_fake(monkeypatch, rows)
    client = _client()

    resp = client.post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert "release_date_warning" in data
    assert "過去日" in data["release_date_warning"]
    assert "2020-01-01" in data["release_date_warning"]


def test_approve_no_warning_for_valid_future_release_date(monkeypatch):
    rows = [
        {"id": 1, "name": "カードA", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": "2099-01-01", "product_name": "商品Z"},
    ]
    _install_fake(monkeypatch, rows)
    client = _client()

    resp = client.post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.get_json()
    assert "release_date_warning" not in data
    assert "release_date_note" not in data


# ──────────────────────────────────────────────
# 一括承認エンドポイントのガード
# ──────────────────────────────────────────────

def test_bulk_approve_backfills_and_warns(monkeypatch):
    rows = [
        # id=1: 補完可能（同じ商品Xの他カードに release_date あり）
        {"id": 1, "name": "カードA", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": None, "product_name": "商品X"},
        # id=2: 参照用（補完元）
        {"id": 2, "name": "カードB", "source_url": "", "extraction_raw": {},
         "status": "approved", "release_date": "2026-09-01", "product_name": "商品X"},
        # id=3: 補完不可（同じ商品名の他カードなし）
        {"id": 3, "name": "カードC", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": None, "product_name": "商品Y"},
        # id=4: 過去日
        {"id": 4, "name": "カードD", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": "2020-01-01", "product_name": "商品Z"},
    ]
    _install_fake(monkeypatch, rows)
    client = _client()

    resp = client.post(
        "/api/admin/unreleased/bulk-approve",
        headers=HEADERS,
        json={"ids": [1, 3, 4]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["approved"] == 3
    assert data["release_date_backfilled"] == 1

    warnings = {w["id"]: w for w in data["release_date_warnings"]}
    assert warnings[3]["reason"] == "発売日未設定"
    assert warnings[4]["reason"] == "発売日が過去日 (2020-01-01)"
    assert 1 not in warnings

    # id=1 は補完されて release_date が入っている
    assert rows[0]["release_date"] == "2026-09-01"
    assert rows[0]["status"] == "approved"


def test_bulk_approve_memoizes_backfill_lookup_per_product(monkeypatch):
    """同じ product_name の複数カードが同時に補完される場合でも、
    互いを補完元として参照し合わないことを確認する
    （NULLのカード同士は release_date を持たないため結果に影響しないはずだが、
    メモ化ロジックが正しく機能しているかを確認する回帰テスト）。"""
    rows = [
        {"id": 1, "name": "カードA", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": None, "product_name": "商品X"},
        {"id": 2, "name": "カードB", "source_url": "", "extraction_raw": {},
         "status": "pending", "release_date": None, "product_name": "商品X"},
        {"id": 3, "name": "カードC", "source_url": "", "extraction_raw": {},
         "status": "approved", "release_date": "2026-09-01", "product_name": "商品X"},
    ]
    _install_fake(monkeypatch, rows)
    client = _client()

    resp = client.post(
        "/api/admin/unreleased/bulk-approve",
        headers=HEADERS,
        json={"ids": [1, 2]},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["release_date_backfilled"] == 2
    assert data["release_date_warnings"] == []
    assert rows[0]["release_date"] == "2026-09-01"
    assert rows[1]["release_date"] == "2026-09-01"
