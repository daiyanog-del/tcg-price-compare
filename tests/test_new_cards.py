"""新着未発売一覧の公開条件・時刻境界・障害・管理キャッシュ連携を検証する。"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import card_display as display
from constants import JST


class Query:
    def __init__(self, database, table):
        self.database, self.table = database, table
        self.filters = []
        self.bounds = None

    def select(self, fields):
        self.database.selections.append((self.table, fields))
        return self

    def in_(self, field, values):
        self.filters.append(lambda row: row.get(field) in values)
        return self

    def eq(self, field, value):
        self.filters.append(lambda row: row.get(field) == value)
        return self

    def is_(self, field, value):
        assert value == "null"
        self.filters.append(lambda row: row.get(field) is None)
        return self

    def order(self, field):
        assert field == "id"
        return self

    def range(self, start, end):
        self.bounds = start, end
        return self

    def execute(self):
        self.database.calls.append(self.table)
        if self.table in self.database.fail:
            raise RuntimeError("DB接続失敗: 内部情報")
        rows = [row for row in self.database.rows[self.table] if all(fn(row) for fn in self.filters)]
        rows.sort(key=lambda row: row.get("id", 0))
        if self.bounds:
            start, end = self.bounds
            rows = rows[start:end + 1]
        return SimpleNamespace(data=deepcopy(rows))


class Database:
    def __init__(self):
        self.rows = {"unreleased_cards": [], "official_card_images": [],
                     "app_settings": [{"key": "OFFICIAL_IMAGE_DISPLAY", "value": {"enabled": True}}]}
        self.fail, self.calls, self.selections = set(), [], []

    def table(self, table):
        return Query(self, table)


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        # UTCでは前日だが、JSTでは発売日当日。
        return datetime(2026, 10, 3, 15, 30, tzinfo=timezone.utc).astimezone(tz)


@pytest.fixture
def database(monkeypatch):
    database = Database()
    display.invalidate_cache()
    monkeypatch.setattr(display, "_supabase_client", database)
    monkeypatch.setattr(display, "datetime", Clock)
    yield database
    display.invalidate_cache()


def card(index, **overrides):
    return {"id": index, "name": f"新カード{index}", "status": "approved", "hidden": False,
            "extracted_at": (datetime(2026, 9, 1, tzinfo=JST) + timedelta(days=index)).isoformat(),
            "release_date": "2026-11-01", "product_name": "新商品", "source_url": "非公開管理情報",
            **overrides}


def image(index, **overrides):
    return {"id": index, "unreleased_card_id": index, "public_url": f"https://example.test/{index}.png",
            "hidden": False, "deleted_at": None, **overrides}


def test_order_limit_and_public_fields(database):
    database.rows["unreleased_cards"] = [card(i) for i in range(40)]
    result = display.get_new_cards()
    assert len(result["cards"]) == 36
    assert result["has_more"] is True
    assert [row["name"] for row in result["cards"]] == [f"新カード{i}" for i in range(39, 3, -1)]
    assert set(result["cards"][0]) == {"name", "added_at", "release_date", "product_name", "image_url"}
    assert result["cards"][0]["added_at"] == card(39)["extracted_at"]
    assert result["cards"][0]["image_url"] is None
    assert any("extracted_at" in fields for table, fields in database.selections if table == "unreleased_cards")


def test_release_and_approval_filters_at_jst_midnight(database):
    database.rows["unreleased_cards"] = [
        card(0, release_date="2026-10-03"), card(1, release_date="2026-10-04"),
        card(2, release_date="2026-10-05"), card(3, release_date=None),
        card(4, status="pending"), card(5, status="rejected"),
        card(6, hidden=True), card(7, status="linked", release_date=None),
        card(8, status="linked", release_date="2026-11-01"),
    ]
    result = display.get_new_cards()
    assert [row["name"] for row in result["cards"]] == ["新カード3", "新カード2"]
    assert result["has_more"] is False
    # 他の表示解決が使う共通取得ではlinkedを引き続き保持する。
    assert {"新カード7", "新カード8"} <= display._get_unreleased().keys()


def test_official_image_flags_and_global_switch(database):
    database.rows["unreleased_cards"] = [card(i) for i in range(4)]
    database.rows["official_card_images"] = [image(0), image(1, hidden=True), image(2, deleted_at="2026-10-01")]
    rows = {row["name"]: row for row in display.get_new_cards()["cards"]}
    assert rows["新カード0"]["image_url"] == "https://example.test/0.png"
    assert all(rows[f"新カード{i}"]["image_url"] is None for i in (1, 2, 3))
    database.rows["app_settings"][0]["value"]["enabled"] = False
    display.invalidate_cache()
    assert all(row["image_url"] is None for row in display.get_new_cards()["cards"])


def test_cache_reused_and_admin_invalidation_hides_card(database):
    database.rows["unreleased_cards"] = [card(1)]
    assert display.get_new_cards()["cards"]
    count = len(database.calls)
    assert display.get_new_cards()["cards"]
    assert len(database.calls) == count
    database.rows["unreleased_cards"][0]["hidden"] = True
    display.invalidate_cache()
    assert display.get_new_cards() == {"cards": [], "has_more": False}


@pytest.mark.parametrize("table", ["app_settings", "unreleased_cards", "official_card_images"])
def test_failure_is_503_and_not_cached_as_empty(database, table):
    import app as app_module
    database.rows["unreleased_cards"] = [card(1)]
    database.fail.add(table)
    response = app_module.app.test_client().get("/api/new-cards")
    assert response.status_code == 503
    assert set(response.get_json()) == {"error"}
    assert "内部情報" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    database.fail.clear()
    response = app_module.app.test_client().get("/api/new-cards")
    assert response.status_code == 200
    assert response.get_json()["cards"]


def test_successful_empty_and_connection_disabled_differ(database, monkeypatch):
    import app as app_module
    response = app_module.app.test_client().get("/api/new-cards")
    assert response.status_code == 200
    assert response.get_json() == {"cards": [], "has_more": False}
    assert response.headers["Cache-Control"] == "no-store"
    display.invalidate_cache()
    monkeypatch.setattr(display, "_supabase_client", None)
    assert display._get_unreleased() == {}
    assert app_module.app.test_client().get("/api/new-cards").status_code == 503


def test_non_strict_failure_does_not_poison_strict_cache(database):
    database.fail.update(["app_settings", "unreleased_cards"])
    assert display._get_unreleased() == {}
    assert display._get_settings() == {}
    with pytest.raises(display.DisplayDataUnavailable):
        display.get_new_cards()


def test_invalidation_during_fetch_cannot_restore_old_rows(database, monkeypatch):
    old_fetch = display._fetch_unreleased_cards
    calls = []
    def fetch(**kwargs):
        rows = old_fetch(**kwargs)
        calls.append(1)
        if len(calls) == 1:
            database.rows["unreleased_cards"][0]["hidden"] = True
            display.invalidate_cache()
        return rows
    database.rows["unreleased_cards"] = [card(1)]
    monkeypatch.setattr(display, "_fetch_unreleased_cards", fetch)
    assert display.get_new_cards()["cards"] == []
    assert len(calls) == 2


def test_candidates_beyond_database_page_limit_are_not_lost(database):
    database.rows["unreleased_cards"] = [card(i, release_date="2026-01-01") for i in range(1001)]
    database.rows["unreleased_cards"].append(card(1001))
    database.rows["official_card_images"] = [image(1001)]
    result = display.get_new_cards()
    assert [row["name"] for row in result["cards"]] == ["新カード1001"]
    assert result["cards"][0]["image_url"] == "https://example.test/1001.png"
