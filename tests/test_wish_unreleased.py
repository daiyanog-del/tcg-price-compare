"""
tests/test_wish_unreleased.py — 購入候補の未発売カードの扱い（2026-10-30）

背景:
  「どこで買うと安いか」が未発売カード（発売日前・TCG先行）も店のリアルタイム検索に回し、
  見つからないので「見つからなかったカード」と出していた。サーバーが未発売の印を付けて
  db_missing から外し（unreleased で返す）、/api/wish-prices も status "unreleased" を返す。

  未発売の判定は未発売カード一覧と同じ: approved・hidden=false・発売日が未定か今日(JST)より後。
  linked は発売済み。発売日の当日0時からは普通のカードとして扱う。
"""

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from constants import JST  # noqa: E402
from fake_price_history import FakeClient as _FakeClient  # noqa: E402
from frozen_clock import freeze_now  # noqa: E402

_NOW = datetime.now(JST)
_TODAY = _NOW.date()
TOMORROW = (_TODAY + timedelta(days=1)).isoformat()
TODAY = _TODAY.isoformat()


@pytest.fixture(autouse=True)
def _freeze_window_clock(monkeypatch):
    import price_history_reads
    freeze_now(monkeypatch, price_history_reads, _NOW)


def _row(card, shop, price, rarity="ノーマル"):
    return {"card_name": card, "shop": shop, "rarity": rarity, "min_price": price,
            "recorded_at": TODAY}


# 未発売テーブルの行（_find_unreleased_card が返す形の必要な項目だけ）
UNRELEASED = {
    "未来のカード": {"name": "未来のカード", "status": "approved", "release_date": TOMORROW},
    "日付未定のカード": {"name": "日付未定のカード", "status": "approved", "release_date": None},
    "今日発売のカード": {"name": "今日発売のカード", "status": "approved", "release_date": TODAY},
    "照合済みのカード": {"name": "照合済みのカード", "status": "linked", "release_date": TOMORROW},
    "隠したカード": {"name": "隠したカード", "status": "approved", "release_date": TOMORROW, "hidden": True},
}
TCG = {"Future Card"}


@pytest.fixture
def client(monkeypatch):
    import app as app_module
    import card_display

    def _make(rows, released=("発売済みカード",)):
        monkeypatch.setattr(app_module, "_supabase_client", _FakeClient(rows))
        monkeypatch.setattr(app_module, "_load_cardnames", lambda: None)
        monkeypatch.setattr(app_module, "_correct_cardname", lambda n: n)
        monkeypatch.setattr(app_module, "_ygores_image_url", lambda n: None)
        monkeypatch.setattr(app_module, "_cardnames_set", set(released))
        monkeypatch.setattr(card_display, "_find_unreleased_card", lambda n: UNRELEASED.get(n))
        monkeypatch.setattr(app_module, "_is_tcg_preview_name", lambda n: n in TCG)
        # wish-prices のフォールバック（30日窓）も通すため、事前判定のキャッシュは空にしておく
        monkeypatch.setattr(app_module, "_estimate_cache", {})
        monkeypatch.setattr(app_module, "_estimate_cache_time", time.time())
        app_module.app.config["TESTING"] = True
        return app_module.app.test_client()

    return _make


class TestStillUnreleasedReleaseDate:
    """card_display.get_still_unreleased_release_date の判定（未発売カード一覧と同じ規則）"""

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        import card_display
        monkeypatch.setattr(card_display, "_find_unreleased_card", lambda n: UNRELEASED.get(n))

    def test_発売日が未来なら発売日を返す(self):
        import card_display
        assert card_display.get_still_unreleased_release_date("未来のカード") == TOMORROW

    def test_発売日が未定なら空文字(self):
        import card_display
        assert card_display.get_still_unreleased_release_date("日付未定のカード") == ""

    def test_発売日の当日からは発売済み(self):
        import card_display
        assert card_display.get_still_unreleased_release_date("今日発売のカード") is None

    def test_linkedと隠したカードと登録の無いカードは対象外(self):
        import card_display
        for name in ("照合済みのカード", "隠したカード", "登録の無いカード"):
            assert card_display.get_still_unreleased_release_date(name) is None, name


class TestWishShopTotalsUnreleased:
    def _post(self, c, names):
        return c.post("/api/wish-shop-totals",
                      json={"cards": [{"name": n, "qty": 1} for n in names]}).get_json()

    def test_未発売とTCG先行は店の検索に回さずunreleasedで返す(self, client):
        c = client([_row("発売済みカード", "カードラッシュ", 500)])
        data = self._post(c, ["発売済みカード", "未来のカード", "日付未定のカード", "Future Card", "収集前のカード"])
        assert [m["name"] for m in data["db_missing"]] == ["収集前のカード"]
        marks = {m["name"]: m for m in data["unreleased"]}
        assert marks["未来のカード"]["release_date"] == TOMORROW
        assert marks["未来のカード"]["tcg_preview"] is False
        assert marks["日付未定のカード"]["release_date"] == ""
        assert marks["Future Card"]["tcg_preview"] is True
        assert set(marks) == {"未来のカード", "日付未定のカード", "Future Card"}

    def test_発売日の当日は普通のカードとして店を探す(self, client):
        c = client([])
        data = self._post(c, ["今日発売のカード", "照合済みのカード"])
        assert data["unreleased"] == []
        assert {m["name"] for m in data["db_missing"]} == {"今日発売のカード", "照合済みのカード"}

    def test_DBに値がある未発売カードは通常どおり集計する(self, client):
        # 予約の出品を集めた等で値があるなら、その値を見せる（隠さない）
        c = client([_row("未来のカード", "カードラッシュ", 800)])
        data = self._post(c, ["未来のカード"])
        assert data["unreleased"] == [] and data["db_missing"] == []
        shop = next(s for s in data["shops"] if s["shop"] == "カードラッシュ")
        assert shop["total"] == 800

    def test_発売済みの辞書にある名前は発売済みを優先(self, client):
        c = client([], released=("未来のカード",))
        data = self._post(c, ["未来のカード"])
        assert data["unreleased"] == []
        assert [m["name"] for m in data["db_missing"]] == ["未来のカード"]


class TestWishPricesUnreleased:
    def test_値の無い未発売カードはstatus_unreleased(self, client):
        c = client([])
        res = c.post("/api/wish-prices",
                     json={"cards": ["未来のカード", "Future Card", "収集前のカード", "今日発売のカード"]})
        by = {x["name"]: x for x in res.get_json()["cards"]}
        assert by["未来のカード"]["status"] == "unreleased"
        assert by["未来のカード"]["release_date"] == TOMORROW
        assert by["Future Card"]["status"] == "unreleased"
        assert by["Future Card"]["tcg_preview"] is True
        assert by["収集前のカード"]["status"] == "pending"
        assert by["今日発売のカード"]["status"] == "pending"
