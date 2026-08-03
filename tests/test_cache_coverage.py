"""
tests/test_cache_coverage.py — 店舗束キャッシュ（カバレッジ付きキャッシュ）の検証

背景:
  従来のキャッシュはカード名だけをキーに「検索結果の全体」を1枚で持っており、
  どの店舗を調べた結果かを記録していなかった。そのため店舗を絞った検索が
  全体キャッシュを上書きし、15分間ほかの店舗が「0件」に見える汚染が起きていた
  （2026-08-03 本番で実証。修正済みのカードが未修正に見える誤診断も誘発した）。

  対策として、キャッシュを店舗ごとの独立したエントリ（タイムスタンプ・partial印
  付き）の束に変更した。取得失敗した店舗は呼び出し側が store に渡さない＝
  キャッシュされず次回再試行される（「0件と取得失敗を区別する」原則の延長）。

テスト方針:
  ネットワーク不使用。CACHE_DIR / BUYBACK_CACHE_DIR を一時ディレクトリに
  差し替えて、純粋にファイル入出力とカバレッジ判定を検証する。
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scraper


def _row(shop, price=100, sold_out=False):
    return {"shop": shop, "name": "テストカード", "rarity": "レア", "code": "",
            "condition": "-", "price": price, "stock": 1, "sold_out": sold_out,
            "url": "", "image": ""}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", True)
    monkeypatch.setattr(scraper, "CACHE_DIR", tmp_path / "sell")
    monkeypatch.setattr(scraper, "BUYBACK_CACHE_DIR", tmp_path / "buy")
    yield


# ── 基本の出し入れ ──

def test_store_then_get_hits_and_missing():
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")], "カーナベル": []})
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル", "トレコロCB"])
    assert set(hit) == {"遊々亭", "カーナベル"}
    assert hit["遊々亭"][0]["price"] == 100
    assert hit["カーナベル"] == []  # 「調べて0件」は有効なキャッシュ
    assert missing == ["トレコロCB"]


def test_partial_store_does_not_clobber_other_shops():
    """店舗を絞った検索が他店舗のキャッシュを消さない（汚染バグの再発防止）"""
    scraper.cache_store_shops("カードA", {"カーナベル": [_row("カーナベル")]})
    scraper.cache_store_shops("カードA", {"トレコロCB": [_row("トレコロCB", 200)]})
    hit, missing = scraper.cache_get_shops("カードA", ["カーナベル", "トレコロCB"])
    assert missing == []
    assert hit["カーナベル"][0]["shop"] == "カーナベル"
    assert hit["トレコロCB"][0]["price"] == 200


def test_unqueried_shop_is_missing_not_zero():
    """調べていない店舗は「0件」ではなく「不足」として返る（汚染バグ本体の検証）"""
    scraper.cache_store_shops("カードA", {"カーナベル": [_row("カーナベル")]})
    hit, missing = scraper.cache_get_shops("カードA", ["トレコロCB"])
    assert hit == {}
    assert missing == ["トレコロCB"]


# ── TTLは店舗ごとに独立 ──

def test_per_shop_ttl_expiry():
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")], "カーナベル": [_row("カーナベル")]})
    fp = scraper.CACHE_DIR / f"{scraper._cache_key('カードA')}.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    old = (datetime.now() - timedelta(minutes=scraper.CACHE_TTL_MINUTES + 1)).isoformat()
    data["shops"]["遊々亭"]["timestamp"] = old
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭", "カーナベル"])
    assert missing == ["遊々亭"]
    assert set(hit) == {"カーナベル"}


def test_store_prunes_expired_entries():
    """store時に期限切れエントリはファイルから間引かれる"""
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    fp = scraper.CACHE_DIR / f"{scraper._cache_key('カードA')}.json"
    data = json.loads(fp.read_text(encoding="utf-8"))
    old = (datetime.now() - timedelta(minutes=scraper.CACHE_TTL_MINUTES + 1)).isoformat()
    data["shops"]["遊々亭"]["timestamp"] = old
    fp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    scraper.cache_store_shops("カードA", {"カーナベル": [_row("カーナベル")]})
    data = json.loads(fp.read_text(encoding="utf-8"))
    assert set(data["shops"]) == {"カーナベル"}


# ── partial印（/api/deck の1ページ目限定スクレイプ） ──

def test_partial_entry_excluded_from_full_search():
    scraper.cache_store_shops("カードA", {"トレコロCB": [_row("トレコロCB")]},
                              partial_shops={"トレコロCB"})
    # 通常検索(include_partial=False)では不足扱い＝完全版を取り直す
    hit, missing = scraper.cache_get_shops("カードA", ["トレコロCB"])
    assert hit == {}
    assert missing == ["トレコロCB"]
    # デッキ検索(include_partial=True)では命中
    hit, missing = scraper.cache_get_shops("カードA", ["トレコロCB"], include_partial=True)
    assert missing == []
    assert hit["トレコロCB"][0]["shop"] == "トレコロCB"


def test_full_store_overwrites_partial():
    scraper.cache_store_shops("カードA", {"トレコロCB": [_row("トレコロCB", 500)]},
                              partial_shops={"トレコロCB"})
    scraper.cache_store_shops("カードA", {"トレコロCB": [_row("トレコロCB", 300)]})
    hit, missing = scraper.cache_get_shops("カードA", ["トレコロCB"])
    assert missing == []
    assert hit["トレコロCB"][0]["price"] == 300


# ── 旧形式・破損ファイル ──

def test_old_format_file_treated_as_miss():
    scraper.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = scraper.CACHE_DIR / f"{scraper._cache_key('カードA')}.json"
    fp.write_text(json.dumps({"timestamp": datetime.now().isoformat(),
                              "results": [_row("遊々亭")]}, ensure_ascii=False),
                  encoding="utf-8")
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭"])
    assert hit == {}
    assert missing == ["遊々亭"]
    assert scraper.cache_get("カードA") is None


def test_corrupt_file_treated_as_miss():
    scraper.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fp = scraper.CACHE_DIR / f"{scraper._cache_key('カードA')}.json"
    fp.write_text("{{{not json", encoding="utf-8")
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭"])
    assert missing == ["遊々亭"]


# ── 互換API cache_get（メタ表示用の平坦化） ──

def test_cache_get_flattens_fresh_shops():
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")],
                                          "カーナベル": [_row("カーナベル", 50)]})
    rows = scraper.cache_get("カードA")
    assert {r["shop"] for r in rows} == {"遊々亭", "カーナベル"}


def test_cache_get_none_when_absent():
    assert scraper.cache_get("存在しないカード") is None


# ── 無効化フラグ ──

def test_cache_disabled(monkeypatch):
    monkeypatch.setattr(scraper, "CACHE_ENABLED", False)
    scraper.cache_store_shops("カードA", {"遊々亭": [_row("遊々亭")]})
    hit, missing = scraper.cache_get_shops("カードA", ["遊々亭"])
    assert hit == {}
    assert missing == ["遊々亭"]
    assert scraper.cache_get("カードA") is None
    assert not (scraper.CACHE_DIR / f"{scraper._cache_key('カードA')}.json").exists()


# ── 買取キャッシュ（販売と同じ挙動・別ディレクトリ） ──

def test_buyback_cache_roundtrip_and_isolation():
    scraper.buyback_cache_store_shops("カードA", {"カードラッシュ": [_row("カードラッシュ")]})
    hit, missing = scraper.buyback_cache_get_shops("カードA", ["カードラッシュ", "遊々亭"])
    assert set(hit) == {"カードラッシュ"}
    assert missing == ["遊々亭"]
    # 販売キャッシュとは独立
    hit_sell, missing_sell = scraper.cache_get_shops("カードA", ["カードラッシュ"])
    assert hit_sell == {}
    assert missing_sell == ["カードラッシュ"]
    assert scraper.buyback_cache_get("カードA")[0]["shop"] == "カードラッシュ"
