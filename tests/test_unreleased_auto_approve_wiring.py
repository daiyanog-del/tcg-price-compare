"""自動承認の記録・X投稿の配線テスト（取込経路と承認エンドポイント）。"""
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import admin_unreleased as admin_module
import watch_x_unreleased as watcher
from tests.test_admin_unreleased_release_date_guard import HEADERS, FUTURE, _client, _install_fake


# ── 取込経路（X）: 挿入前に verdict と snapshot が付く ──

def test_x_watcher_annotates_row_before_insert(monkeypatch):
    monkeypatch.setattr(watcher, "_get_known_fuzzy_keys", lambda: set())
    monkeypatch.setattr(watcher, "load_active_names", lambda sb: ["既存カード"])
    ext = ModuleType("unreleased_extractor")
    ext.extract_cards_from_tweet = MagicMock(return_value=[{
        "name": "新カード", "product_name": "P", "card_type": "効果モンスター", "effect_text": "効果",
        "atk": 1000, "level": 4, "confidence": "high", "release_date": FUTURE,
        "extraction_raw": {"source": "x_tweet"},
    }])
    monkeypatch.setitem(sys.modules, "unreleased_extractor", ext)
    store = ModuleType("unreleased_image_store")
    store.ingest_x_card_image = MagicMock(return_value=(True, "ok"))
    monkeypatch.setitem(sys.modules, "unreleased_image_store", store)

    captured = []

    def fake_upsert(sb, rows):
        captured.extend(rows)
        return [dict(r, id=1) for r in rows]

    monkeypatch.setattr(watcher, "_upsert_cards", fake_upsert)
    tweet = {"id": "1", "text": "◤新カード◢", "attachments": {"media_keys": ["m"]}}
    n = watcher.process_tweets([tweet], {"m": "https://example.test/x.jpg"}, MagicMock(), set())
    assert n == 1
    raw = captured[0]["extraction_raw"]
    assert raw["auto_approve_verdict"]["eligible"] is False  # 再読・発売日根拠がないテスト行
    assert "recognition_not_verified" in raw["auto_approve_verdict"]["reasons"]
    assert raw["extracted_snapshot"]["name"] == "新カード" and raw["extracted_snapshot"]["atk"] == 1000


# ── 承認エンドポイント ──

def _row(**over):
    row = {"id": 1, "name": "カードA", "source_url": "", "status": "pending", "release_date": FUTURE,
           "product_name": "商品X", "card_type": "効果モンスター", "attribute": "闇", "race": "ドラゴン族",
           "level": 4, "rank": None, "link_val": None, "atk": 1800, "def": "1000", "effect_text": "効果",
           "pendulum_scale": None, "pendulum_effect": ""}
    row.update(over)
    row["extraction_raw"] = {
        "extracted_snapshot": {**{k: row.get(k) for k in (
            "name", "card_type", "attribute", "race", "level", "rank", "link_val", "atk", "def",
            "effect_text", "pendulum_scale", "pendulum_effect", "product_name", "release_date")}, "atk": 1500},
        "auto_approve_verdict": {"eligible": True, "reasons": []},
        "crop_state": {"mode": "auto"},
    }
    return row


def test_single_approve_saves_approval_snapshot_and_posts(monkeypatch):
    rows = [_row()]
    _install_fake(monkeypatch, rows)
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (True, "ok"))
    started = []
    monkeypatch.setattr(admin_module._x_post, "start_post_thread", lambda sb, ids: started.append(list(ids)))

    resp = _client().post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200
    snap = rows[0]["extraction_raw"]["approval_snapshot"]
    assert snap["via"] == "single" and snap["edited_fields"] == ["atk"]
    assert snap["crop_mode"] == "auto" and snap["verdict_eligible"] is True
    assert rows[0]["extraction_raw"]["crop_state"] == {"mode": "auto"}  # 既存キーを消さない
    assert started == [[1]]


def test_single_approve_does_not_post_when_image_failed(monkeypatch):
    _install_fake(monkeypatch, [_row()])
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (False, "ng"))
    started = []
    monkeypatch.setattr(admin_module._x_post, "start_post_thread", lambda sb, ids: started.append(list(ids)))
    assert _client().post("/api/admin/unreleased/1/approve", headers=HEADERS).status_code == 200
    assert started == []


def test_bulk_approve_saves_snapshots_and_posts_only_fetched(monkeypatch):
    rows = [_row(id=1), _row(id=2, name="カードB")]
    _install_fake(monkeypatch, rows)
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction",
                        lambda card_id, **kw: (card_id == 1, "x"))
    posted = []
    monkeypatch.setattr(admin_module._x_post, "post_cards_sequential", lambda sb, ids: posted.append(list(ids)))

    # バックグラウンドスレッドを同期実行にする
    class SyncThread:
        def __init__(self, target, args=(), daemon=None):
            self._t, self._a = target, args

        def start(self):
            self._t(*self._a)

    monkeypatch.setattr(admin_module.threading, "Thread", SyncThread)
    resp = _client().post("/api/admin/unreleased/bulk-approve", headers=HEADERS, json={"ids": [1, 2]})
    assert resp.status_code == 200
    for r in rows:
        snap = r["extraction_raw"]["approval_snapshot"]
        assert snap["via"] == "bulk" and snap["edited_fields"] == ["atk"]
    assert posted == [[1]]
