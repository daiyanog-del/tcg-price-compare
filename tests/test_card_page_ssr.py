"""
tests/test_card_page_ssr.py — カードのページの HTML に最初から書く価格（card_page_ssr.py・2026-11-01）

  - 一番新しい日の店ごとの最安を作る（休止中の店・10円未満は除く・名前の完全一致を優先）
  - 読んだ結果を取っておく（記録なしも取っておく・失敗は取っておかない）
  - ページは _WAIT_SEC までしか待たず、間に合わなければ価格なしで返して裏で読み終える
  - /card/<name> の HTML に h1・店ごとの最安・説明文・構造化データが入る。知らない名前は noindex
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module  # noqa: E402
import card_page_ssr  # noqa: E402
import shop_status  # noqa: E402


def _row(shop, price, day="2026-10-31", rarity="ノーマル", name="灰流うらら"):
    return {"card_name": name, "shop": shop, "rarity": rarity, "min_price": price, "recorded_at": day}


@pytest.fixture(autouse=True)
def _fresh_cache():
    card_page_ssr._reset_for_tests()
    yield
    card_page_ssr._reset_for_tests()


# ── summarize ──

def test_summarize_uses_latest_day_and_min_per_shop():
    rows = [
        _row("遊々亭", 300), _row("遊々亭", 180, rarity="ウルトラ"),
        _row("遊々亭", 90, day="2026-10-30"),  # その店の古い日は使わない
        _row("カードラッシュ", 200),
    ]
    s = card_page_ssr.summarize(rows, "灰流うらら")
    assert s["date"] == "2026-10-31" and s["date_label"] == "10/31"
    assert [(x["shop"], x["price"], x["rarity"]) for x in s["shops"]] == [
        ("遊々亭", 180, "ウルトラ"), ("カードラッシュ", 200, "ノーマル")]
    assert s["low"] == 180 and s["high"] == 200 and s["best"]["shop"] == "遊々亭"


def test_summarize_keeps_shop_missing_today_and_labels_range():
    # 今日の行が一部の店にしか無いとき、他の店は昨日の行で残し、日付を範囲で書く
    rows = [_row("トレコロCB", 240, day="2026-11-01"), _row("遊々亭", 220, day="2026-10-31")]
    s = card_page_ssr.summarize(rows, "灰流うらら")
    assert [x["shop"] for x in s["shops"]] == ["遊々亭", "トレコロCB"]
    assert s["date"] == "2026-11-01" and s["date_label"] == "10/31〜11/1"


def test_summarize_drops_shop_whose_record_stopped():
    # 売り切れると収集はその店の行を書かないので、記録が止まった店の古い値段は「最安」にしない（reviewer 高2）
    rows = [_row("遊々亭", 100, day="2026-10-25"), _row("カードラボ", 300, day="2026-11-01")]
    s = card_page_ssr.summarize(rows, "灰流うらら")
    assert [x["shop"] for x in s["shops"]] == ["カードラボ"]
    assert s["low"] == 300 and s["date_label"] == "11/1"


def test_summarize_prefers_display_price_and_hides_unknown_rarity():
    rows = [dict(_row("遊々亭", 500, rarity="(不明)"), min_price_any=420),
            dict(_row("カードラボ", 600), min_price_any=None)]
    s = card_page_ssr.summarize(rows, "灰流うらら")
    assert [(x["shop"], x["price"], x["rarity"]) for x in s["shops"]] == [("遊々亭", 420, ""), ("カードラボ", 600, "ノーマル")]


def test_summarize_drops_suspended_shops_and_floor(monkeypatch):
    monkeypatch.setattr(shop_status, "SUSPENDED_SHOPS", frozenset({"まんぞく屋"}))
    rows = [_row("まんぞく屋", 50), _row("遊々亭", 5), _row("カードラボ", 120)]
    s = card_page_ssr.summarize(rows, "灰流うらら")
    assert [x["shop"] for x in s["shops"]] == ["カードラボ"]


def test_summarize_prefers_exact_name_rows():
    rows = [_row("遊々亭", 100, name="Ｎｏ.９６"), _row("カードラボ", 500, name="No.96")]
    s = card_page_ssr.summarize(rows, "No.96")
    assert [x["shop"] for x in s["shops"]] == ["カードラボ"]
    # 完全一致が無ければ、表記をそろえた名前の行を使う
    s2 = card_page_ssr.summarize([_row("遊々亭", 100, name="Ｎｏ.９６")], "No.96")
    assert s2["low"] == 100
    # 完全一致の行が全部使えない（10円未満）なら、表記をそろえた名前の行を使う
    s3 = card_page_ssr.summarize([_row("遊々亭", 5, name="No.96"), _row("カードラボ", 700, name="Ｎｏ.９６")], "No.96")
    assert s3["low"] == 700


def test_summarize_none_when_no_rows():
    assert card_page_ssr.summarize([], "灰流うらら") is None
    assert card_page_ssr.summarize([_row("遊々亭", 3)], "灰流うらら") is None


# ── get_snapshot（取っておき・待ち時間） ──

class _Resp:
    def __init__(self, data):
        self.data = data


class _FakeSb:
    """price_history の読み込みだけを返す。delay 秒待ってから返す・fail なら例外"""

    def __init__(self, rows, delay=0.0, fail=False):
        self.rows, self.delay, self.fail = rows, delay, fail
        self.calls = 0
        self.lock = threading.Lock()

    def table(self, name):
        assert name == "price_history"
        return self

    def select(self, *_a):
        return self

    def in_(self, *_a):
        return self

    def gte(self, *_a):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a):
        return self

    def execute(self):
        with self.lock:
            self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("db down")
        return _Resp(self.rows)


def test_snapshot_cached_including_no_record():
    sb = _FakeSb([_row("遊々亭", 180)])
    assert card_page_ssr.get_snapshot(sb, "灰流うらら")["low"] == 180
    assert card_page_ssr.get_snapshot(sb, "灰流うらら")["low"] == 180
    assert sb.calls == 1
    empty = _FakeSb([])
    assert card_page_ssr.get_snapshot(empty, "記録なし") is None
    assert card_page_ssr.get_snapshot(empty, "記録なし") is None
    assert empty.calls == 1


def test_snapshot_failure_retried_after_fail_ttl(monkeypatch):
    bad = _FakeSb([], fail=True)
    assert card_page_ssr.get_snapshot(bad, "灰流うらら") is None
    # 失敗の直後は読み直さない（DB が落ちている間に積み上げない）
    assert card_page_ssr.get_snapshot(bad, "灰流うらら") is None
    assert bad.calls == 1
    # 期限を過ぎたら読み直す。成功の結果だけ取っておく
    monkeypatch.setattr(card_page_ssr, "_FAIL_TTL_SEC", 0)
    bad.fail, bad.rows = False, [_row("遊々亭", 180)]
    assert card_page_ssr.get_snapshot(bad, "灰流うらら")["low"] == 180
    assert bad.calls == 2


def test_snapshot_does_not_queue_beyond_pending_limit(monkeypatch):
    monkeypatch.setattr(card_page_ssr, "_WAIT_SEC", 0.01)
    monkeypatch.setattr(card_page_ssr, "_MAX_PENDING", 2)
    sb = _FakeSb([_row("遊々亭", 180)], delay=0.3)
    for i in range(5):
        assert card_page_ssr.get_snapshot(sb, f"カード{i}") is None
    assert len(card_page_ssr._inflight) == 2
    time.sleep(0.8)
    assert sb.calls == 2


def test_snapshot_slow_read_returns_none_then_cached(monkeypatch):
    monkeypatch.setattr(card_page_ssr, "_WAIT_SEC", 0.05)
    sb = _FakeSb([_row("遊々亭", 180)], delay=0.3)
    t0 = time.time()
    assert card_page_ssr.get_snapshot(sb, "灰流うらら") is None
    assert time.time() - t0 < 0.25  # ページは読み終わりを待たない
    # 読み込み中にもう一度来ても、新しく読みに行かない
    assert card_page_ssr.get_snapshot(sb, "灰流うらら") is None
    time.sleep(0.4)
    assert card_page_ssr.get_snapshot(sb, "灰流うらら")["low"] == 180
    assert sb.calls == 1


def test_snapshot_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(card_page_ssr, "_CACHE_MAX", 3)
    sb = _FakeSb([_row("遊々亭", 180)])
    for i in range(5):
        card_page_ssr.get_snapshot(sb, f"カード{i}")
    assert len(card_page_ssr._cache) == 3
    assert list(card_page_ssr._cache) == ["カード2", "カード3", "カード4"]


def test_snapshot_without_client():
    assert card_page_ssr.get_snapshot(None, "灰流うらら") is None


# ── /card/<name> の HTML ──

_SNAP = card_page_ssr.summarize([_row("遊々亭", 180, rarity="ウルトラ"), _row("カードラッシュ", 1200)], "灰流うらら")


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_card_page_html_has_prices(client, monkeypatch):
    monkeypatch.setattr(card_page_ssr, "get_snapshot", lambda sb, name: _SNAP)
    body = client.get("/card/%E7%81%B0%E6%B5%81%E3%81%86%E3%82%89%E3%82%89").get_data(as_text=True)
    assert '<h1 class="card-hero-name" id="searchPreviewName">灰流うらら</h1>' in body
    assert 'data-ssr-name="灰流うらら"' in body
    assert "<title>灰流うらら の値段・最安値比較 | 強欲なラボ</title>" in body
    assert "灰流うららの値段（最安値）は¥180（遊々亭・ウルトラ・10/31時点）" in body
    assert '<span class="ssr-shop">カードラッシュ</span>' in body and "¥1,200" in body
    assert '"lowPrice": 180' in body and '"highPrice": 1200' in body and '"offerCount": 2' in body
    assert '<meta name="robots" content="index, follow">' in body


def test_card_page_without_snapshot_keeps_name_h1(client, monkeypatch):
    monkeypatch.setattr(card_page_ssr, "get_snapshot", lambda sb, name: None)
    monkeypatch.setattr(app_module, "_cardnames_set", {"灰流うらら"})
    body = client.get("/card/%E7%81%B0%E6%B5%81%E3%81%86%E3%82%89%E3%82%89").get_data(as_text=True)
    assert '<h1 class="card-hero-name" id="searchPreviewName">灰流うらら</h1>' in body
    assert "ssr-shops" not in body
    assert '<meta name="robots" content="index, follow">' in body


def test_unreleased_card_is_index(client, monkeypatch):
    # 未発売カードは正式名の一覧にも価格の記録にも無いが、新着欄が /card/ でリンクしている（reviewer 高1）
    monkeypatch.setattr(card_page_ssr, "get_snapshot", lambda sb, name: None)
    monkeypatch.setattr(app_module, "_cardnames_set", {"灰流うらら"})
    monkeypatch.setattr(app_module, "_estimate_cache", {})
    monkeypatch.setattr(app_module._card_display, "get_display_proxy",
                        lambda name: {"name": name} if name == "あいうえお" else None)
    body = client.get("/card/%E3%81%82%E3%81%84%E3%81%86%E3%81%88%E3%81%8A").get_data(as_text=True)
    assert '<meta name="robots" content="index, follow">' in body


def test_unknown_card_name_is_noindex(client, monkeypatch):
    monkeypatch.setattr(card_page_ssr, "get_snapshot", lambda sb, name: None)
    monkeypatch.setattr(app_module, "_cardnames_set", {"灰流うらら"})
    monkeypatch.setattr(app_module, "_estimate_cache", {})
    monkeypatch.setattr(app_module._card_display, "get_display_proxy", lambda name: None)
    body = client.get("/card/%E3%81%82%E3%81%84%E3%81%86%E3%81%88%E3%81%8A").get_data(as_text=True)
    assert '<meta name="robots" content="noindex, follow">' in body


def test_buy_page_unchanged(client, monkeypatch):
    called = []
    monkeypatch.setattr(card_page_ssr, "get_snapshot", lambda sb, name: called.append(name))
    body = client.get("/buy/%E7%81%B0%E6%B5%81%E3%81%86%E3%82%89%E3%82%89").get_data(as_text=True)
    assert called == []
    assert 'id="searchPreview" hidden aria-hidden="true"' in body
