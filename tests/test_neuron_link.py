"""
tests/test_neuron_link.py — ニューロン連携（/card/by-cid/<cid>）のテスト

背景:
  拡張機能はニューロンURLの cid（= ygoresources の konami_id、2026-08-05 に
  11ペア実測突合で同一体系と確認）を送り、サーバー側で名前解決して
  /card/<名前> へ転送する。DOMスクレイピング由来の読み仮名混入バグの根治。

テスト方針:
  純ロジック（neuron_link.resolve_card_name）は getter を差し替えて直接検証。
  ルートは Flask test_client + monkeypatch でネットワーク不使用。
"""

import sys
from pathlib import Path
from urllib.parse import quote, unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from neuron_link import resolve_card_name, MAX_CARD_NAME_LEN
import app as app_module


# ---- 純ロジック ----

def test_resolve_hit():
    getter = lambda cid: {"name": "ドラゴン族・封印の壺", "konami_id": cid}
    assert resolve_card_name(4335, "", getter) == "ドラゴン族・封印の壺"


def test_resolve_hit_ignores_fallback():
    """ygores解決が成功したらフォールバック名（DOM由来＝信頼度低）は使わない"""
    getter = lambda cid: {"name": "ブラック・マジシャン"}
    assert resolve_card_name(4041, "ぶらっく・まじしゃん", getter) == "ブラック・マジシャン"


def test_resolve_miss_uses_fallback():
    """ygores未収録（発売直後カード等）はフォールバック名で救う"""
    assert resolve_card_name(99999, "新規カード", lambda cid: None) == "新規カード"


def test_resolve_getter_raises_uses_fallback():
    def boom(cid):
        raise RuntimeError("API死亡")
    assert resolve_card_name(4335, "予備名", boom) == "予備名"


def test_resolve_all_miss_returns_none():
    assert resolve_card_name(99999, "", lambda cid: None) is None
    assert resolve_card_name(99999, "   ", lambda cid: None) is None


def test_resolve_too_long_name_rejected():
    """card_page の上限（50文字）を超える名前は解決失敗として扱う"""
    long_name = "あ" * (MAX_CARD_NAME_LEN + 1)
    getter = lambda cid: {"name": long_name}
    # 解決名が長すぎる → フォールバックへ
    assert resolve_card_name(1, "予備名", getter) == "予備名"
    # フォールバックも長すぎる → None
    assert resolve_card_name(1, long_name, getter) is None


def test_resolve_name_stripped():
    getter = lambda cid: {"name": "  ブラック・マジシャン\n"}
    assert resolve_card_name(4041, "", getter) == "ブラック・マジシャン"


# ---- ルート ----

@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_route_redirects_to_card_page(client, monkeypatch):
    monkeypatch.setattr(app_module._ygores_repo, "get_card_summary",
                        lambda cid: {"name": "ドラゴン族・封印の壺"})
    resp = client.get("/card/by-cid/4335")
    assert resp.status_code == 302
    assert unquote(resp.headers["Location"]) == "/card/ドラゴン族・封印の壺"


def test_route_fallback_name(client, monkeypatch):
    monkeypatch.setattr(app_module._ygores_repo, "get_card_summary",
                        lambda cid: None)
    resp = client.get("/card/by-cid/99999?name=" + quote("新規カード"))
    assert resp.status_code == 302
    assert unquote(resp.headers["Location"]) == "/card/新規カード"


def test_route_unresolvable_404(client, monkeypatch):
    monkeypatch.setattr(app_module._ygores_repo, "get_card_summary",
                        lambda cid: None)
    resp = client.get("/card/by-cid/99999")
    assert resp.status_code == 404


def test_route_non_numeric_cid_falls_through(client):
    """非数値cidは <int:cid> に一致せず既存の /card/<path> が受ける（500にしない）"""
    resp = client.get("/card/by-cid/abc")
    assert resp.status_code == 200
