"""
tests/test_send_daily_report_clabo_warnings.py — 日次レポートへのカードラボ カタログ
巡回の異常警告（reviewer中2）

背景:
  カテゴリ差分（category_drift）・検証失敗カテゴリ（categories_failed）・例外
  （mode="error"）は print ログにしか残らないと収集ログを毎回見ない限り気付けない。
  send_daily_report（Discord日次レポート）に1〜2行の警告として追記する。

  ネットワーク不使用（requests.post をモックする）。
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests as requests_module
import collect_prices as cp


class _FakeQuery:
    """search_logs への select().gte().lt().execute() チェーンを模倣する"""

    def select(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def lt(self, *a, **k):
        return self

    def execute(self):
        return SimpleNamespace(data=[])


class _FakeSupabase:
    def table(self, name):
        return _FakeQuery()


def _send_and_capture(monkeypatch, shop_stats):
    captured = {}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return SimpleNamespace(status_code=204)

    monkeypatch.setattr(requests_module, "post", fake_post)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.invalid/webhook")

    cp.send_daily_report(
        _FakeSupabase(), "2026-08-09",
        success_count=10, fail_count=1, shop_stats=shop_stats,
    )
    return captured["json"]["content"]


def test_includes_category_drift_warning(monkeypatch):
    shop_stats = {
        "カードラボ": {
            "ok": 100, "empty": 5, "error": 0, "mode": "catalog",
            "catalog": {
                "category_drift": ["新しいカテゴリを検出: 9001 = 遊戯王OCG:新種別（CLABO_CATEGORIES未登録）"],
                "categories_failed": [],
            },
        },
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "カテゴリ差分" in message
    assert "9001" in message


def test_includes_categories_failed_warning(monkeypatch):
    shop_stats = {
        "カードラボ": {
            "ok": 90, "empty": 5, "error": 0, "mode": "catalog",
            "catalog": {"category_drift": [], "categories_failed": ["672", "739"]},
        },
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "検証失敗" in message
    assert "672" in message and "739" in message


def test_includes_exception_warning_when_mode_is_error(monkeypatch):
    shop_stats = {
        "カードラボ": {"ok": 0, "empty": 0, "error": 0, "mode": "error", "exception": "ConnectionError"},
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "例外" in message
    assert "ConnectionError" in message


def test_no_extra_warning_when_catalog_is_clean(monkeypatch):
    """drift・categories_failed が両方空なら警告行を足さない（既存レポート形式を壊さない）"""
    shop_stats = {
        "カードラボ": {
            "ok": 100, "empty": 5, "error": 0, "mode": "catalog",
            "catalog": {"category_drift": [], "categories_failed": []},
        },
        "遊々亭": {"ok": 90, "empty": 10, "error": 0},
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "カテゴリ差分" not in message
    assert "検証失敗" not in message
    assert "例外" not in message


def test_category_drift_is_truncated_to_first_5_with_remainder_count(monkeypatch):
    """reviewer低2: 全件付け替えるとDiscordの2,000文字上限に迫るため、先頭5件＋「他N件」に丸める"""
    drift = [f"新しいカテゴリを検出: {9000+i} = 遊戯王OCG:新種別{i}（CLABO_CATEGORIES未登録）" for i in range(8)]
    shop_stats = {
        "カードラボ": {
            "ok": 100, "empty": 5, "error": 0, "mode": "catalog",
            "catalog": {"category_drift": drift, "categories_failed": []},
        },
    }
    message = _send_and_capture(monkeypatch, shop_stats)

    assert "カテゴリ差分 8件" in message
    for d in drift[:5]:
        assert d in message
    for d in drift[5:]:
        assert d not in message
    assert "他3件" in message


def test_harvest_failure_produces_a_single_warning_line(monkeypatch):
    """reviewer低3: harvest_categories取得失敗（category_drift=None）は「取得失敗」の
    警告を1行出す。全カテゴリ消滅の偽警告14件にはしない（stats側のNone保持で防止済み）"""
    shop_stats = {
        "カードラボ": {
            "ok": 100, "empty": 5, "error": 0, "mode": "catalog",
            "catalog": {"category_drift": None, "categories_failed": []},
        },
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "カテゴリ差分チェック取得失敗" in message
    # 偽の「N件」表示（category_driftをNoneのままリスト展開した場合の兆候）が出ていないこと
    assert "カテゴリ差分 " not in message


def test_mode_error_without_catalog_key_does_not_emit_drift_line(monkeypatch):
    """mode="error" で catalog キー自体が無い場合、category_drift関連の警告は出さない
    （exceptionの警告行だけで足りる。Noneの誤判定で「取得失敗」を二重に出さない）"""
    shop_stats = {
        "カードラボ": {"ok": 0, "empty": 0, "error": 0, "mode": "error", "exception": "boom"},
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "例外" in message
    assert "カテゴリ差分チェック取得失敗" not in message


def test_includes_categories_unverified_warning(monkeypatch):
    """reviewer中3b: item_count読取不能（自然終端のため成立扱い）のカテゴリを警告に出す"""
    shop_stats = {
        "カードラボ": {
            "ok": 100, "empty": 5, "error": 0, "mode": "catalog",
            "catalog": {
                "category_drift": [], "categories_failed": [],
                "categories_unverified": ["672", "739"],
            },
        },
    }
    message = _send_and_capture(monkeypatch, shop_stats)
    assert "未検証" in message
    assert "672" in message and "739" in message
