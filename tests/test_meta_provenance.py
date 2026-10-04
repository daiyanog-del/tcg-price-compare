"""環境集計の採用期間・取得日時・古いデータへの退避をネットワークなしで検証する。"""

import json
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import meta_scraper as meta


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(meta, "_CACHE_DIR", tmp_path)
    monkeypatch.setattr(meta, "_fetch_meta_complete", lambda *_: None)


def summaries(count):
    return [{"deckGuideName": f"テーマ{i}", "totalDecks": 3, "usageRate": 10,
             "tier": 1, "rank": i + 1} for i in range(count)]


def seed_cache(*, old=False, metadata=True):
    payload = {"tiers": [{"name": "保存テーマ"}],
               "_ts": (datetime.now() - timedelta(hours=4 if old else 0)).isoformat()}
    if metadata:
        payload["metadata"] = {"from": "2026-08-01", "to": "2026-08-31",
                               "fetched_at": "2026-08-31T12:00:00+09:00"}
    meta._cache_path("tier_list").write_text(json.dumps(payload), encoding="utf-8")
    return payload


@pytest.mark.parametrize("counts, days", [([5], 30), ([2, 6], 60), ([3, 2], 30)])
def test_actual_selected_window_is_persisted(monkeypatch, counts, days):
    responses = iter(summaries(n) for n in counts)
    monkeypatch.setattr(meta, "_fetch_meta_complete", lambda *_: next(responses))
    result = meta.fetch_tier_snapshot()
    metadata = result["metadata"]
    assert (datetime.fromisoformat(metadata["to"]) -
            datetime.fromisoformat(metadata["from"])).days == days
    assert metadata["fetched_at"]
    assert metadata["sample_size"] is None
    assert metadata["theme_count"] == max(counts)
    assert metadata["stale"] is False
    assert metadata["refresh_error"] is None
    assert meta.fetch_tier_snapshot() == result
    assert meta.fetch_tier_list() == result["tiers"]


@pytest.mark.parametrize("response, error", [(None, "fetch_failed"), ([], "empty_response"),
                                           ([{"deckGuideName": ""}], "empty_response")])
def test_failed_refresh_keeps_original_metadata(monkeypatch, response, error):
    cached = seed_cache(old=True)
    monkeypatch.setattr(meta, "_fetch_meta_complete", lambda *_: response)
    result = meta.fetch_tier_snapshot()
    assert result["tiers"] == cached["tiers"]
    assert result["metadata"]["stale"] is True
    assert result["metadata"]["refresh_error"] == error
    for key in ("from", "to", "fetched_at"):
        assert result["metadata"][key] == cached["metadata"][key]
    assert meta._cache_read("tier_list", timedelta(days=7))["_ts"] == cached["_ts"]


@pytest.mark.parametrize("old", [False, True])
def test_legacy_cache_conditions_remain_unknown(old):
    seed_cache(old=old, metadata=False)
    result = meta.fetch_tier_snapshot()
    assert all(result["metadata"][key] is None for key in ("from", "to", "fetched_at", "sample_size"))
    assert result["metadata"]["stale"] is old


def test_failed_refresh_without_cache():
    result = meta.fetch_tier_snapshot()
    assert result["tiers"] == []
    assert result["metadata"]["refresh_error"] == "fetch_failed"
    assert result["metadata"]["from"] is None


def test_atomic_cache_keeps_rows_and_metadata_together():
    def write(index):
        payload = {"tiers": [{"name": str(index)}], "metadata": {"from": str(index)}}
        meta._cache_write("tier_list", payload)
        assert "_ts" not in payload
        cached = meta._cache_read("tier_list", timedelta(days=1))
        assert cached is not None
        assert cached["tiers"][0]["name"] == cached["metadata"]["from"]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(write, range(12)))


@pytest.mark.parametrize("cached", [False, True])
def test_api_meta_returns_snapshot(monkeypatch, cached):
    import app as app_module
    if cached:
        seed_cache()
    monkeypatch.setattr(meta, "_fetch_meta_complete", lambda *_: summaries(5))
    monkeypatch.setattr(app_module, "_cache_read", meta._cache_read)
    monkeypatch.setattr(app_module, "_meta_executor", SimpleNamespace(
        submit=lambda fn, *args: SimpleNamespace(result=lambda **_: fn(*args))))
    with app_module.app.test_request_context("/api/meta?include_metadata=1"):
        result = app_module.api_meta().get_json()
    assert result["tiers"]
    assert result["metadata"]["from"]
    assert result["metadata"]["stale"] is False


def test_api_timeout_uses_stale_cache(monkeypatch):
    import app as app_module
    seed_cache(old=True)
    monkeypatch.setattr(app_module, "_cache_read", meta._cache_read)
    def timeout(**_):
        raise TimeoutError()
    monkeypatch.setattr(app_module, "_meta_executor", SimpleNamespace(
        submit=lambda *_: SimpleNamespace(result=timeout)))
    with app_module.app.test_request_context("/api/meta?include_metadata=1"):
        result = app_module.api_meta().get_json()
        assert app_module.g.no_cache is True
    assert result["metadata"]["stale"] is True
    assert result["metadata"]["refresh_error"] == "timeout_or_error"


@pytest.mark.parametrize("cached", [False, True])
def test_api_legacy_client_still_gets_list(monkeypatch, cached):
    import app as app_module
    if cached:
        seed_cache()
    monkeypatch.setattr(meta, "_fetch_meta_complete", lambda *_: summaries(5))
    monkeypatch.setattr(app_module, "_cache_read", meta._cache_read)
    monkeypatch.setattr(app_module, "_meta_executor", SimpleNamespace(
        submit=lambda fn, *args: SimpleNamespace(result=lambda **_: fn(*args))))
    with app_module.app.test_request_context("/api/meta"):
        result = app_module.api_meta().get_json()
    assert isinstance(result, list)
    assert result
