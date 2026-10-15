"""未発売カードの「テキストの誤り報告」（unreleased_text_report.py・POST /api/card-text-report・管理API）のテスト。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import admin_unreleased as admin_module
import app as app_module
import unreleased_text_report as tr
from tests.fake_supabase import FakeSB


def _cards():
    return [
        {"id": 1, "name": "公開カードA", "status": "approved", "hidden": False},
        {"id": 2, "name": "非表示カード", "status": "approved", "hidden": True},
        {"id": 3, "name": "承認待ちカード", "status": "pending", "hidden": False},
        {"id": 4, "name": "ＳＰ－ｃａｒｄ", "status": "approved", "hidden": False},
    ]


def _sb(extra=None):
    tables = {"unreleased_cards": _cards(), "unreleased_text_reports": []}
    tables.update(extra or {})
    return FakeSB(tables, pks={"unreleased_cards": "id"})


# ── 検証 ──

def test_validate_ok_and_strips():
    clean, err = tr.validate_report({"name": " 公開カードA ", "field": "effect_text", "note": " 誤字です "})
    assert err is None and clean == {"name": "公開カードA", "field": "effect_text", "note": "誤字です"}


@pytest.mark.parametrize("body", [
    None, [], "x", {}, {"name": "", "field": "other"}, {"name": 5, "field": "other"},
    {"name": "A", "field": "unknown"}, {"name": "A", "field": None}, {"name": "A" * 201, "field": "other"},
    {"name": "A", "field": "other", "note": 5}, {"name": "A", "field": "other", "note": "あ" * 501},
])
def test_validate_rejects(body):
    clean, err = tr.validate_report(body)
    assert clean is None and err


def test_validate_note_optional_and_limit_boundary():
    assert tr.validate_report({"name": "A", "field": "other"})[1] is None
    assert tr.validate_report({"name": "A", "field": "other", "note": None})[1] is None
    assert tr.validate_report({"name": "A", "field": "other", "note": "あ" * 500})[1] is None


def test_validate_strips_control_chars():
    clean, _ = tr.validate_report({"name": "A", "field": "other", "note": "a\x00b\x07c\nd"})
    assert clean["note"] == "abc\nd"


# ── 保存・通知 ──

def test_process_report_saves_hash_not_raw_ip_and_notifies():
    sb = _sb()
    sent = []
    result, status = tr.process_report(sb, {"name": "公開カードA", "field": "effect_text", "note": "墓地へ返って"},
                                       "203.0.113.9", notify=sent.append, base_url="https://example.test")
    assert status == 200 and result == {"ok": True}
    row = sb.tables["unreleased_text_reports"][0]
    assert row["unreleased_card_id"] == 1 and row["card_name"] == "公開カードA"
    assert row["field"] == "effect_text" and row["status"] == "open"
    assert "203.0.113.9" not in str(row) and len(row["ip_hash"]) == 64
    assert row["ip_hash"] == tr.hash_ip("203.0.113.9") != tr.hash_ip("203.0.113.10")
    assert len(sent) == 1 and "公開カードA" in sent[0] and "https://example.test/admin" in sent[0]
    # 報告だけでカードの公開状態は変えない
    assert sb.tables["unreleased_cards"][0]["status"] == "approved" and sb.tables["unreleased_cards"][0]["hidden"] is False


def test_process_report_only_public_cards():
    for name in ("非表示カード", "承認待ちカード", "存在しない"):
        sb = _sb()
        result, status = tr.process_report(sb, {"name": name, "field": "other"}, "1.1.1.1", notify=lambda m: None)
        assert status == 404 and sb.tables["unreleased_text_reports"] == []


def test_process_report_fuzzy_name_match():
    sb = _sb()
    result, status = tr.process_report(sb, {"name": "SP-card", "field": "other"}, "1.1.1.1", notify=lambda m: None)
    assert status == 200 and sb.tables["unreleased_text_reports"][0]["unreleased_card_id"] == 4


def test_discord_message_truncates_note():
    msg = tr.build_discord_message("カード", "other", "あ" * 500, 1)
    assert "あ" * 300 + "…" in msg and "あ" * 301 not in msg


def test_discord_send_disables_mentions(monkeypatch):
    import discord_notify
    posted = {}
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.test/hook")
    monkeypatch.setattr(discord_notify.requests, "post",
                        lambda url, json=None, timeout=None: posted.update(json=json) or type("R", (), {"status_code": 204})())
    discord_notify.send_discord_message("@everyone テスト", no_mentions=True)
    assert posted["json"]["allowed_mentions"] == {"parse": []}
    posted.clear()
    discord_notify.send_discord_message("従来どおり")
    assert "allowed_mentions" not in posted["json"]


# ── API（Origin・レート制限・配線） ──

@pytest.fixture()
def client(monkeypatch):
    app_module.app.config.update(TESTING=True)
    app_module._reset_rate_limits()
    sb = _sb()
    monkeypatch.setattr(app_module, "_supabase_client", sb)
    monkeypatch.setattr(tr, "_notify_async", lambda m, label="": None)
    c = app_module.app.test_client()
    c.sb = sb
    return c


def test_api_accepts_report(client):
    resp = client.post("/api/card-text-report", json={"name": "公開カードA", "field": "stats", "note": "ATKが違う"})
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert client.sb.tables["unreleased_text_reports"][0]["field"] == "stats"


def test_api_rate_limited_per_ip(client):
    ok = client.post("/api/card-text-report", json={"name": "公開カードA", "field": "stats"})
    again = client.post("/api/card-text-report", json={"name": "公開カードA", "field": "stats"})
    assert ok.status_code == 200 and again.status_code == 429
    assert len(client.sb.tables["unreleased_text_reports"]) == 1


def test_api_rate_bucket_registered_and_independent():
    assert app_module._last_text_report in app_module._all_rate_limit_buckets()


def test_api_rejects_foreign_origin(client):
    resp = client.post("/api/card-text-report", json={"name": "公開カードA", "field": "stats"},
                       headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403 and client.sb.tables["unreleased_text_reports"] == []


def test_api_validation_errors(client):
    assert client.post("/api/card-text-report", json={"name": "公開カードA", "field": "x"}).status_code == 400
    app_module._reset_rate_limits()
    assert client.post("/api/card-text-report", json={"name": "非表示カード", "field": "other"}).status_code == 404
    app_module._reset_rate_limits()
    assert client.post("/api/card-text-report", data="not json", content_type="text/plain").status_code == 400


# ── 管理API ──

def _admin(monkeypatch, sb):
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    # 認証失敗のレート制限は他のテストの失敗分も数えるため、毎回リセットする（全件実行で429にならないように）
    admin_module._auth_fail_log.clear()
    admin_module._auth_fail_global_log.clear()
    monkeypatch.setattr(admin_module, "_auth_fail_global_blocked_until", 0.0)
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client(), {"X-Admin-Key": "test-key"}


def test_admin_list_and_resolve(monkeypatch):
    sb = _sb({"unreleased_text_reports": [
        {"id": 1, "unreleased_card_id": 1, "card_name": "公開カードA", "field": "effect_text", "note": "n1",
         "ip_hash": "h", "status": "open", "created_at": "2026-10-15T00:00:00+00:00"},
        {"id": 2, "unreleased_card_id": 1, "card_name": "公開カードA", "field": "other", "note": "n2",
         "ip_hash": "h", "status": "resolved", "created_at": "2026-10-14T00:00:00+00:00"},
    ]})
    c, h = _admin(monkeypatch, sb)
    resp = c.get("/api/admin/unreleased/1/text-reports", headers=h)
    reports = resp.get_json()["reports"]
    assert [r["id"] for r in reports] == [2, 1]  # 新しい順
    assert all("ip_hash" not in r for r in reports)
    assert reports[0]["field_label"] == "その他" and reports[1]["field_label"] == "効果テキスト"
    assert tr.count_open_by_card(sb) == {1: 1}

    res = c.post("/api/admin/text-reports/1/resolve", headers=h)
    assert res.status_code == 200 and res.get_json()["report"]["status"] == "resolved"
    assert "ip_hash" not in res.get_json()["report"]
    assert tr.count_open_by_card(sb) == {}
    assert c.post("/api/admin/text-reports/999/resolve", headers=h).status_code == 404


def test_admin_endpoints_require_key(monkeypatch):
    c, _ = _admin(monkeypatch, _sb())
    assert c.get("/api/admin/unreleased/1/text-reports").status_code == 401
    assert c.post("/api/admin/text-reports/1/resolve").status_code == 401


def test_count_open_by_card_survives_missing_table():
    class Boom:
        def table(self, name):
            raise RuntimeError("relation does not exist")
    assert tr.count_open_by_card(Boom()) == {}


# ── 画面部品（node で DOM スタブに描画して確認）──

def test_js_text_report_ui():
    import os
    import shutil
    import subprocess
    if not shutil.which("node"):
        pytest.skip("node が無い")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(["node", os.path.join(root, "tests", "js", "text_report_check.js")],
                            cwd=root, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr


# ── 乱用対策（reviewer 指摘）──

def _body(name="公開カードA"):
    return {"name": name, "field": "other", "note": "x"}


def test_same_ip_same_card_only_once_per_day():
    sb = _sb()
    assert tr.process_report(sb, _body(), "1.1.1.1", notify=lambda m: None)[1] == 200
    result, status = tr.process_report(sb, _body(), "1.1.1.1", notify=lambda m: None)
    assert status == 429 and len(sb.tables["unreleased_text_reports"]) == 1
    # 別IP・別カードは受け付ける
    assert tr.process_report(sb, _body(), "2.2.2.2", notify=lambda m: None)[1] == 200
    assert tr.process_report(sb, _body("ＳＰ－ｃａｒｄ"), "1.1.1.1", notify=lambda m: None)[1] == 200


def test_same_ip_same_card_allowed_again_after_a_day():
    old = {"id": 1, "unreleased_card_id": 1, "card_name": "公開カードA", "field": "other", "note": "", "status": "open",
           "ip_hash": tr.hash_ip("1.1.1.1"),
           "created_at": (datetime.now(timezone.utc) - timedelta(days=1, minutes=1)).isoformat()}
    sb = _sb({"unreleased_text_reports": [old]})
    assert tr.process_report(sb, _body(), "1.1.1.1", notify=lambda m: None)[1] == 200


def test_hourly_global_limit_returns_429_without_saving(monkeypatch):
    monkeypatch.setattr(tr, "REPORT_HOURLY_LIMIT", 3)
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"id": i, "unreleased_card_id": 99, "card_name": "x", "field": "other", "note": "", "status": "open",
             "ip_hash": f"h{i}", "created_at": now} for i in range(3)]
    sb = _sb({"unreleased_text_reports": rows})
    result, status = tr.process_report(sb, _body(), "9.9.9.9", notify=lambda m: None)
    assert status == 429 and len(sb.tables["unreleased_text_reports"]) == 3


def test_hourly_limit_ignores_old_reports(monkeypatch):
    monkeypatch.setattr(tr, "REPORT_HOURLY_LIMIT", 2)
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    rows = [{"id": i, "unreleased_card_id": 99, "card_name": "x", "field": "other", "note": "", "status": "open",
             "ip_hash": f"h{i}", "created_at": old} for i in range(5)]
    sb = _sb({"unreleased_text_reports": rows})
    assert tr.process_report(sb, _body(), "9.9.9.9", notify=lambda m: None)[1] == 200


class _FakeTimer:
    created = []

    def __init__(self, interval, fn):
        self.interval, self.fn, self.daemon = interval, fn, False
        _FakeTimer.created.append(self)

    def start(self):
        pass


def test_notify_batcher_sends_first_then_summarises():
    _FakeTimer.created.clear()
    sent = []
    b = tr.NotifyBatcher(send=sent.append, window=300, timer_factory=_FakeTimer)
    import threading as _t
    real_thread = _t.Thread
    # 送信スレッドを同期実行にする
    b._send_async = lambda m: sent.append(m)
    sent.clear()
    assert b.submit("m1", "A") is True
    assert b.submit("m2", "B") is False and b.submit("m3", "B") is False
    assert sent == ["m1"] and len(_FakeTimer.created) == 1
    _FakeTimer.created[0].fn()            # 窓の終わり
    assert len(sent) == 2 and "2件" in sent[1] and "B" in sent[1]
    assert len(_FakeTimer.created) == 2   # まとめを送ったので次の窓を開く
    _FakeTimer.created[1].fn()            # 何も溜まっていなければ窓を閉じるだけ
    assert len(sent) == 2 and b.open is False
    assert b.submit("m4", "C") is True and sent[-1] == "m4"
    assert real_thread


def test_public_names_cached_for_ttl(monkeypatch):
    sb = _sb()
    reads = []
    orig = sb.table

    def counting(name):
        if name == "unreleased_cards":
            reads.append(1)
        return orig(name)

    monkeypatch.setattr(sb, "table", counting)
    tr._names_cache.update(at=0.0, sb=None, rows=[])
    for _ in range(5):
        assert tr.find_public_card(sb, "存在しない名前") is None
    # 毎回の完全一致の問い合わせ（5回）＋ fuzzy 用の全件読み（キャッシュで1回だけ）
    assert len(reads) == 5 + 1
    tr._names_cache.update(at=0.0, sb=None, rows=[])
    tr._public_names(sb, clock=lambda: 1000.0)
    n = len(reads)
    tr._public_names(sb, clock=lambda: 1001.0)                          # TTL 内→読まない
    assert len(reads) == n
    tr._public_names(sb, clock=lambda: 1000.0 + tr.PUBLIC_NAMES_TTL_SEC + 1)   # TTL 超過→読み直す
    assert len(reads) == n + 1
