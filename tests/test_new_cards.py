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
    assert set(result["cards"][0]) == {"name", "added_at", "release_date", "product_name", "image_url", "thumb_url"}
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
    assert all(row["thumb_url"] is None for row in display.get_new_cards()["cards"])


def test_thumb_url_is_returned_only_with_the_full_image(database):
    database.rows["unreleased_cards"] = [card(i) for i in range(3)]
    database.rows["official_card_images"] = [
        image(0, thumb_url="https://example.test/thumbs/0.webp?t=1"),
        image(1),  # 縮小版を作る前の行（列が空）
        image(2, hidden=True, thumb_url="https://example.test/thumbs/2.webp"),
    ]
    rows = {row["name"]: row for row in display.get_new_cards()["cards"]}
    assert rows["新カード0"]["thumb_url"] == "https://example.test/thumbs/0.webp?t=1"
    assert rows["新カード0"]["image_url"] == "https://example.test/0.png"
    assert rows["新カード1"]["thumb_url"] is None and rows["新カード1"]["image_url"] == "https://example.test/1.png"
    assert rows["新カード2"]["thumb_url"] is None and rows["新カード2"]["image_url"] is None


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


def test_api_all_returns_every_card_and_default_stays_36(database):
    import app as app_module
    database.rows["unreleased_cards"] = [card(i) for i in range(40)]
    client = app_module.app.test_client()
    default = client.get("/api/new-cards").get_json()
    assert len(default["cards"]) == 36
    assert default["has_more"] is True
    everything = client.get("/api/new-cards?all=1").get_json()
    assert len(everything["cards"]) == 40
    assert everything["has_more"] is False
    assert everything["cards"][0]["name"] == "新カード39"


def test_get_new_cards_limit_none_has_no_cap(database):
    database.rows["unreleased_cards"] = [card(i) for i in range(50)]
    assert len(display.get_new_cards(limit=None)["cards"]) == 50
    assert len(display.get_new_cards()["cards"]) == 36


def test_new_cards_page_renders():
    import app as app_module
    response = app_module.app.test_client().get("/new-cards")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<title>未発売カード一覧 | 強欲なラボ</title>" in html
    assert 'class="new-cards-page"' in html


def _age_caches(seconds):
    """キャッシュの取得時刻を seconds 秒前にずらす（TTL・古さの上限の境界を試す用）。"""
    with display._cache_lock:
        display._unreleased_cache_at -= seconds
        display._settings_cache_at -= seconds


def _wait_refresh_done():
    import time
    for _ in range(200):
        with display._cache_lock:
            if not display._refreshing:
                return
        time.sleep(0.01)
    raise AssertionError("裏更新が終わらない")


@pytest.fixture
def fresh_refresh_state(monkeypatch):
    monkeypatch.setattr(display, "_refreshing", set())
    monkeypatch.setattr(display, "_refresh_failed_at", {})


def test_stale_cache_is_returned_immediately_and_refreshed_in_background(database, fresh_refresh_state, monkeypatch):
    database.rows["unreleased_cards"] = [card(1)]
    assert [r["name"] for r in display.get_new_cards()["cards"]] == ["新カード1"]
    database.rows["unreleased_cards"].append(card(2))
    _age_caches(display._CACHE_TTL + 1)
    started = []
    real_thread = display.threading.Thread

    class HeldThread(real_thread):
        def start(self):
            started.append(self)
    monkeypatch.setattr(display.threading, "Thread", HeldThread)
    count = len(database.calls)
    # TTL 切れでも古い値をそのまま返し、その場では DB を読まない
    assert [r["name"] for r in display.get_new_cards()["cards"]] == ["新カード1"]
    assert len(database.calls) == count
    # 同じキャッシュの裏更新は多重に起動しない（unreleased と settings で1本ずつ）
    display.get_new_cards()
    assert sorted(t.name for t in started) == ["card_display_refresh_settings", "card_display_refresh_unreleased"]
    for t in started:
        real_thread.start(t)
        t.join()
    assert [r["name"] for r in display.get_new_cards()["cards"]] == ["新カード2", "新カード1"]


def test_cache_older_than_stale_max_is_fetched_synchronously(database, fresh_refresh_state):
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    database.rows["unreleased_cards"][0]["hidden"] = True
    _age_caches(display._CACHE_STALE_MAX + 1)
    assert display.get_new_cards() == {"cards": [], "has_more": False}


def test_failure_beyond_stale_max_still_raises(database, fresh_refresh_state):
    import app as app_module
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    _age_caches(display._CACHE_STALE_MAX + 1)
    database.fail.add("unreleased_cards")
    assert app_module.app.test_client().get("/api/new-cards").status_code == 503


def test_background_failure_keeps_stale_value_and_backs_off(database, fresh_refresh_state):
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    _age_caches(display._CACHE_TTL + 1)
    database.fail.add("unreleased_cards")
    assert display.get_new_cards()["cards"]
    _wait_refresh_done()
    assert "unreleased" in display._refresh_failed_at
    calls = len(database.calls)
    # 失敗直後は再試行しない（リクエストのたびに DB を叩かない）
    assert display.get_new_cards()["cards"]
    _wait_refresh_done()
    assert database.calls[calls:].count("unreleased_cards") == 0


def test_invalidation_during_background_refresh_is_not_overwritten(database, fresh_refresh_state, monkeypatch):
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    old_fetch = display._fetch_unreleased_cards

    def fetch(**kwargs):
        rows = old_fetch(**kwargs)
        # 取得し終えた直後に管理操作で非表示＋無効化された
        database.rows["unreleased_cards"][0]["hidden"] = True
        display.invalidate_cache()
        return rows
    monkeypatch.setattr(display, "_fetch_unreleased_cards", fetch)
    display._refresh_unreleased()
    monkeypatch.setattr(display, "_fetch_unreleased_cards", old_fetch)
    assert display.get_new_cards() == {"cards": [], "has_more": False}


def test_refresh_caches_refetches_only_existing_caches(database):
    display.refresh_caches()
    assert database.calls == []
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    database.rows["unreleased_cards"].append(card(2))
    display.refresh_caches()
    count = len(database.calls)
    assert len(display.get_new_cards()["cards"]) == 2
    assert len(database.calls) == count


def test_refresh_caches_keeps_going_when_settings_fail(database):
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    database.rows["unreleased_cards"].append(card(2))
    database.fail.add("app_settings")
    with pytest.raises(display.DisplayDataUnavailable):
        display.refresh_caches()
    database.fail.clear()
    # settings が失敗しても unreleased は取り直されている
    assert len(display._get_unreleased()) == 2


def test_thread_start_failure_does_not_block_future_refresh(database, fresh_refresh_state, monkeypatch):
    database.rows["unreleased_cards"] = [card(1)]
    display.get_new_cards()
    _age_caches(display._CACHE_TTL + 1)

    class BrokenThread:
        def __init__(self, *a, **k):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")
    monkeypatch.setattr(display.threading, "Thread", BrokenThread)
    assert display.get_new_cards()["cards"]  # 古い値は返る
    assert display._refreshing == set()
    assert "unreleased" in display._refresh_failed_at
    # 失敗の待ち（_CACHE_TTL 秒）が過ぎれば、次のリクエストでまた裏更新が起動する
    started = []

    class HeldThread:
        def __init__(self, *a, name=None, **k):
            self.name = name

        def start(self):
            started.append(self.name)
    monkeypatch.setattr(display.threading, "Thread", HeldThread)
    display.get_new_cards()
    assert started == []  # 待ちの間は起動しない
    with display._cache_lock:
        for name in list(display._refresh_failed_at):
            display._refresh_failed_at[name] -= display._CACHE_TTL + 1
    display.get_new_cards()
    assert "card_display_refresh_unreleased" in started


# ── HTML への埋め込み（2026-10-22 LCP）──

def test_cached_new_cards_never_waits_for_db(database):
    """覚えている値が無いときは DB を引かずに None（ページの HTML を DB 待ちで止めない）。"""
    database.rows["unreleased_cards"] = [card(i) for i in range(3)]
    assert display.cached_new_cards() is None
    assert database.calls == []
    # 一度 API 経由で取れば、以後は覚えている値から返す（DB は引かない）
    expected = display.get_new_cards()
    calls = len(database.calls)
    assert display.cached_new_cards() == expected
    assert len(database.calls) == calls


def test_cached_new_cards_is_none_when_too_old(database, monkeypatch):
    database.rows["unreleased_cards"] = [card(0)]
    display.get_new_cards()
    base = display.time.monotonic()
    monkeypatch.setattr(display.time, "monotonic", lambda: base + display._CACHE_STALE_MAX + 1)
    calls = len(database.calls)
    assert display.cached_new_cards() is None
    assert len(database.calls) == calls


def test_top_page_writes_first_cards_and_new_cards_page_preloads(database):
    import app as app_module
    database.rows["unreleased_cards"] = [card(i) for i in range(8)]
    database.rows["official_card_images"] = [image(7, thumb_url="https://example.test/thumbs/7.webp?v=a"), image(6)]
    client = app_module.app.test_client()
    # 覚えている値が無いときは埋め込まず、仮の枠のまま（従来どおり API で取る）
    html = client.get("/").get_data(as_text=True)
    assert 'id="newCardsInitial"' not in html and "new-card-skel" in html
    display.get_new_cards()
    html = client.get("/").get_data(as_text=True)
    assert 'id="newCardsInitial"' in html and "new-card-skel" not in html
    grid = html[html.index('id="newCardsGrid"'):html.index('id="newCardsStatus"')]
    assert grid.count('class="new-card"') == 6
    # 最初の枠: 縮小版を優先・すぐ読む・日付は日本時間の「MM/DD 追加」・名前は文字として出す
    assert '<img src="https://example.test/thumbs/7.webp?v=a" alt="" loading="eager" decoding="async">' in grid
    assert '<img src="https://example.test/6.png"' in grid
    assert "09/08 追加" in grid and 'data-card-name="新カード7"' in grid
    assert 'rel="preload" as="image"' not in html  # トップは本物の img で足りる
    # /new-cards は埋め込まない（一覧ページの印が付く前に描いて描き直しになり、LCP が悪化したため）
    html = client.get("/new-cards").get_data(as_text=True)
    assert 'id="newCardsInitial"' not in html and 'rel="preload" as="image"' not in html


def test_first_cards_html_escapes_names(database):
    import app as app_module
    database.rows["unreleased_cards"] = [card(0, name='<b>"x"</b>')]
    database.rows["official_card_images"] = [image(0, public_url="javascript:alert(1)")]
    display.get_new_cards()
    html = app_module.app.test_client().get("/").get_data(as_text=True)
    grid = html[html.index('id="newCardsGrid"'):html.index('id="newCardsInitial"')]
    assert "<b>" not in grid and "&lt;b&gt;" in grid
    assert "javascript:" not in grid
