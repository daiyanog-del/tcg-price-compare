"""
tests/test_packs_rate_limit.py — /api/packs/cards のキャッシュ×レート制限回帰テスト

背景:
    /api/packs/cards は同一IP 3秒に1回のレート制限を持つが、pack_scraper.fetch_pack_cards()
    は12時間のディスクキャッシュを持ち、キャッシュヒット時は外部の yugioh-wiki を一切叩かない。
    レート制限の目的は外部サイトへのスクレイプ濫用防止であり、キャッシュヒット時にまで
    レート制限を消費すると「パックAを見た直後にパックBを押す」正当な操作が429になる
    （2026-09-01 にユーザーから報告）。

テスト方針:
    Flask test_client + pack_scraper のキャッシュディレクトリを一時ディレクトリへ隔離。
    fetch_pack_cards 自体は呼ばれてもネットワークに出ないよう、キャッシュヒット時は
    _fetch_from_wiki 等に到達しない前提（キャッシュミス時はモックして代用する）。
"""

from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pack_scraper
import app as app_module


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(pack_scraper, "_CACHE_DIR", tmp_path / "packs")
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def test_cache_hit_bypasses_rate_limit(client):
    """キャッシュ済みのパックは連続で叩いても429にならない"""
    app_module._reset_rate_limits()
    pack_scraper._cache_write("pack_テストパック", {
        "pack": "テストパック", "cards": ["カードA", "カードB"], "count": 2,
    })

    resp1 = client.get("/api/packs/cards?name=テストパック")
    assert resp1.status_code == 200
    assert resp1.get_json()["cards"] == ["カードA", "カードB"]

    # 3秒待たずに連続で叩いても、キャッシュヒットなのでレート制限を消費しない
    resp2 = client.get("/api/packs/cards?name=テストパック")
    assert resp2.status_code == 200
    assert resp2.get_json()["cards"] == ["カードA", "カードB"]


def test_cache_miss_still_rate_limited(client, monkeypatch):
    """キャッシュがない場合は従来どおりレート制限が効く"""
    app_module._reset_rate_limits()
    monkeypatch.setattr(app_module, "fetch_pack_cards",
                         lambda pack_name, wiki_page="", tcg_name="":
                         {"pack": pack_name, "cards": ["カードX"], "count": 1})

    resp1 = client.get("/api/packs/cards?name=未キャッシュパック")
    assert resp1.status_code == 200

    resp2 = client.get("/api/packs/cards?name=未キャッシュパック")
    assert resp2.status_code == 429


def test_api_packs_includes_has_cards(client, monkeypatch):
    """/api/packs のレスポンス各要素に has_cards が含まれること"""
    monkeypatch.setattr(app_module, "get_pack_list",
                         lambda: [{"name": "テストパック", "wiki_page": "テストパック",
                                    "tcg_name": "", "date": "2026-08-01", "has_cards": False}])

    resp = client.get("/api/packs")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data) == 1
    assert data[0]["has_cards"] is False
