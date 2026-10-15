"""レビュー指摘の修正（2026-10-15）のテスト: X投稿の上限・結果不明・過去分除外・承認記録・再判定ほか。

対象: unreleased_x_post.py / x_poster.py / unreleased_auto_approve.py / unreleased_text_check.py / admin_unreleased.py
"""
import logging
import sys
import threading
import time
import types
from datetime import datetime, timedelta, timezone

import pytest
import requests

import admin_unreleased as admin_module
import unreleased_auto_approve as aa
import unreleased_text_check as tc
import unreleased_x_post as xp
import x_poster
from tests.fake_supabase import FakeSB
# autouse フィクスチャ _env（有効化・認証情報・間隔0・発売済み名の無効化）を共用する
from tests.test_unreleased_x_post import (  # noqa: F401
    SNAP, TODAY, Poster, _add_card, _env, _sb,
)
from tests.test_admin_unreleased_release_date_guard import FUTURE, HEADERS, _client
from tests.test_unreleased_auto_approve import CLEAN_TEXT, _card, _ev


_REAL_IS_RELEASED_NAME = xp._is_released_name  # autouse の _env が差し替える前の本物


def _rows(sb):
    return sb.tables["unreleased_x_posts"]


def _status(sb, cid):
    return next(r for r in _rows(sb) if r["card_id"] == cid)["status"]


@pytest.fixture
def notices(monkeypatch):
    got = []
    monkeypatch.setattr(xp, "_notify", got.append)
    return got


# ══════════════════════════════════════════════
# 項目1: 件数の上限・429
# ══════════════════════════════════════════════

def test_bulk_limit_leaves_rest_unreserved():
    """一括の1回上限: 超えた分は投稿せず、予約行も作らない（回収ジョブが後で拾う）"""
    sb = _sb()
    for cid in (2, 3, 4):
        _add_card(sb, cid)
    poster = Poster()
    counts = xp.post_cards_sequential(sb, [1, 2, 3, 4], post_fn=poster, max_cards=2)
    assert counts == {"posted": 2} and len(poster.calls) == 2
    assert sorted(r["card_id"] for r in _rows(sb)) == [1, 2]
    assert xp.find_backfill_candidates(sb, TODAY) == [3, 4]  # 残りは回収候補に残る


def test_bulk_post_limit_constant_is_set():
    assert xp.MAX_BULK_POST_PER_RUN == 10


def test_daily_limit_blocks_without_reserving(monkeypatch):
    monkeypatch.setattr(xp, "DAILY_POST_LIMIT", 2)
    now = xp._now_iso()
    yesterday = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    sb = _sb(posts=[
        {"card_id": 10, "status": "posted", "attempts": 0, "updated_at": now},
        {"card_id": 11, "status": "unknown", "attempts": 0, "updated_at": now},   # unknown も数える
        {"card_id": 12, "status": "posted", "attempts": 0, "updated_at": yesterday},  # 前日分は数えない
        {"card_id": 13, "status": "failed", "attempts": 1, "updated_at": now},         # 失敗は数えない
    ])
    poster = Poster()
    assert xp.count_posted_today(sb) == 2
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "daily_limit"
    assert poster.calls == [] and all(r["card_id"] != 1 for r in _rows(sb))  # 予約しない


def test_daily_limit_allows_when_under(monkeypatch):
    monkeypatch.setattr(xp, "DAILY_POST_LIMIT", 3)
    sb = _sb(posts=[{"card_id": 10, "status": "posted", "attempts": 0, "updated_at": xp._now_iso()},
                    {"card_id": 11, "status": "posted", "attempts": 0, "updated_at": xp._now_iso()}])
    assert xp.post_card(sb, 1, post_fn=Poster(), today=TODAY) == "posted"
    assert xp.count_posted_today(sb) == 3


def test_daily_limit_is_shared_by_all_routes_and_stops_sequence(monkeypatch):
    monkeypatch.setattr(xp, "DAILY_POST_LIMIT", 1)
    sb = _sb()
    _add_card(sb, 2)
    _add_card(sb, 3)
    poster = Poster()
    counts = xp.post_cards_sequential(sb, [1, 2, 3], post_fn=poster)
    assert counts == {"posted": 1, "daily_limit": 1}  # 2枚目で上限→打ち切り（3枚目は試みない）
    assert len(poster.calls) == 1
    assert xp.run_backfill(sb, post_fn=Poster(), sleep=lambda s: None) == {"daily_limit": 1}  # 回収経路も同じ上限


def test_rate_limited_not_counted_as_attempt_and_retried(notices):
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster((None, "rate_limited")), today=TODAY) == "rate_limited"
    row = _rows(sb)[0]
    assert row["status"] == "rate_limited" and row["attempts"] == 0
    assert xp.find_backfill_candidates(sb, TODAY) == [1]  # 回収で再挑戦される
    # 何度当たっても上限なしで再挑戦できる（attempts を増やさない）
    for _ in range(4):
        assert xp.post_card(sb, 1, post_fn=Poster((None, "rate_limited")), today=TODAY) == "rate_limited"
    assert _rows(sb)[0]["attempts"] == 0
    assert xp.post_card(sb, 1, post_fn=Poster(("77", None)), today=TODAY) == "posted"
    assert _rows(sb)[0]["tweet_id"] == "77"


def test_rate_limited_stops_sequence():
    sb = _sb()
    _add_card(sb, 2)
    counts = xp.post_cards_sequential(sb, [1, 2], post_fn=Poster((None, "rate_limited")))
    assert counts == {"rate_limited": 1}
    assert [r["card_id"] for r in _rows(sb)] == [1]  # 2枚目は予約もしていない


def test_post_fn_tuple_success_and_rejected_counts_attempt():
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster((None, "rejected")), today=TODAY) == "failed"
    row = _rows(sb)[0]
    assert row["status"] == "failed" and row["attempts"] == 1 and "拒否" in row["last_error"]


def test_pre_send_failure_counts_attempt():
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster((None, "pre_send")), today=TODAY) == "failed"
    assert _rows(sb)[0]["attempts"] == 1


def test_kind_constants_match_x_poster():
    assert (xp.KIND_NOT_CONFIGURED, xp.KIND_PRE_SEND, xp.KIND_DRY_RUN, xp.KIND_REJECTED,
            xp.KIND_RATE_LIMITED, xp.KIND_UNKNOWN) == (
        x_poster.ERR_NOT_CONFIGURED, x_poster.ERR_PRE_SEND, x_poster.ERR_DRY_RUN, x_poster.ERR_REJECTED,
        x_poster.ERR_RATE_LIMITED, x_poster.ERR_UNKNOWN)


# ── x_poster.post_tweet_ex（tweepy はダミー）──

class _HttpError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = types.SimpleNamespace(status_code=status, text="body")


def _fake_tweepy(monkeypatch, *, create=None, upload=None):
    """create: create_tweet が返す値 or 投げる例外 / upload: media_upload の同様"""
    seen = {"api_kwargs": None, "clients": []}

    class _Client:
        def __init__(self, **kwargs):
            self.session = None
            seen["clients"].append(self)

        def create_tweet(self, **kwargs):
            if isinstance(create, Exception):
                raise create
            return create if create is not None else types.SimpleNamespace(data={"id": "777"})

    class _API:
        def __init__(self, auth, **kwargs):
            seen["api_kwargs"] = kwargs

        def media_upload(self, path):
            if isinstance(upload, Exception):
                raise upload
            return types.SimpleNamespace(media_id=1)

    fake = types.SimpleNamespace(__version__="test", OAuth1UserHandler=lambda *a, **k: object(),
                                 API=_API, Client=_Client)
    monkeypatch.setitem(sys.modules, "tweepy", fake)
    return seen


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0test")
    return str(p)


def test_ex_success(monkeypatch, img):
    _fake_tweepy(monkeypatch)
    assert x_poster.post_tweet_ex("本文", [img], require_media=True) == ("777", None)


@pytest.mark.parametrize("exc,kind", [
    (_HttpError(429), "rate_limited"),
    (_HttpError(403), "rejected"),
    (_HttpError(400), "rejected"),
    (_HttpError(503), "unknown"),        # 5xx は結果不明
    (requests.exceptions.ReadTimeout("t"), "unknown"),     # 応答なし（タイムアウト）
    (requests.exceptions.ConnectionError("c"), "unknown"),  # 応答なしの切断
    (RuntimeError("何か"), "unknown"),
])
def test_ex_classifies_create_tweet_errors(monkeypatch, img, exc, kind):
    _fake_tweepy(monkeypatch, create=exc)
    assert x_poster.post_tweet_ex("本文", [img], require_media=True) == (None, kind)
    assert x_poster.post_tweet("本文", [img], require_media=True) is None  # 旧関数は None のまま


def test_ex_response_without_id_is_unknown(monkeypatch, img):
    _fake_tweepy(monkeypatch, create=types.SimpleNamespace(data=None))
    assert x_poster.post_tweet_ex("本文", [img]) == (None, "unknown")


def test_ex_pre_send_failures(monkeypatch, img):
    _fake_tweepy(monkeypatch, upload=RuntimeError("upload failed"))
    assert x_poster.post_tweet_ex("本文", [img], require_media=True) == (None, "pre_send")
    assert x_poster.post_tweet_ex("本文", None, require_media=True) == (None, "pre_send")
    _fake_tweepy(monkeypatch, upload=_HttpError(429))
    assert x_poster.post_tweet_ex("本文", [img], require_media=True) == (None, "rate_limited")


def test_ex_not_configured(monkeypatch, img):
    monkeypatch.delenv("X_API_KEY")
    _fake_tweepy(monkeypatch)
    assert x_poster.post_tweet_ex("本文", [img]) == (None, "not_configured")


def test_ex_dry_run(monkeypatch, img):
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    _fake_tweepy(monkeypatch)
    assert x_poster.post_tweet_ex("本文", [img], require_media=True) == (None, "dry_run")


# ══════════════════════════════════════════════
# 項目2: 結果不明・タイムアウト
# ══════════════════════════════════════════════

def test_unknown_is_not_retried_and_notifies(notices):
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster((None, "unknown")), today=TODAY) == "unknown"
    row = _rows(sb)[0]
    assert row["status"] == "unknown" and row["attempts"] == 0
    assert notices and "結果不明" in notices[0]
    poster = Poster(("5", None))
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"   # 自動再試行しない
    assert poster.calls == []
    assert xp.find_backfill_candidates(sb, TODAY) == []                    # 回収の対象にもならない
    assert xp.reserve(sb, 1) == "taken"


def test_unknown_stops_sequence():
    sb = _sb()
    _add_card(sb, 2)
    assert xp.post_cards_sequential(sb, [1, 2], post_fn=Poster((None, "unknown"))) == {"unknown": 1}


def test_tweepy_timeouts_are_applied(monkeypatch, img):
    seen = _fake_tweepy(monkeypatch)
    x_poster.post_tweet_ex("本文", [img], require_media=True)
    assert seen["api_kwargs"] == {"timeout": x_poster.X_HTTP_TIMEOUT_SEC}  # media_upload(v1)
    assert isinstance(seen["clients"][0].session, x_poster._TimeoutSession)  # create_tweet(v2)


def test_timeout_session_sets_default_but_respects_explicit(monkeypatch):
    captured = []
    monkeypatch.setattr(requests.Session, "request", lambda self, *a, **kw: captured.append(kw))
    s = x_poster._TimeoutSession()
    s.request("GET", "http://x.test/")
    s.request("GET", "http://x.test/", timeout=5)
    assert captured[0]["timeout"] == x_poster.X_HTTP_TIMEOUT_SEC and captured[1]["timeout"] == 5


def test_real_tweepy_client_passes_timeout(monkeypatch):
    """実物の tweepy.Client（4.14）で、差し替えた session 経由のリクエストに timeout が付く"""
    import tweepy
    captured = []

    def fake_request(self, method, url, **kwargs):
        captured.append(kwargs.get("timeout"))
        resp = requests.Response()
        resp.status_code = 201
        resp._content = b'{"data": {"id": "123", "text": "x"}}'
        return resp

    monkeypatch.setattr(requests.Session, "request", fake_request)
    client = tweepy.Client(consumer_key="a", consumer_secret="b", access_token="c", access_token_secret="d")
    client.session = x_poster._TimeoutSession()
    assert client.create_tweet(text="x").data["id"] == "123"
    assert captured == [x_poster.X_HTTP_TIMEOUT_SEC]


def test_real_tweepy_api_accepts_timeout():
    import tweepy
    api = tweepy.API(tweepy.OAuth1UserHandler("a", "b", "c", "d"), timeout=x_poster.X_HTTP_TIMEOUT_SEC)
    assert api.timeout == x_poster.X_HTTP_TIMEOUT_SEC


# ══════════════════════════════════════════════
# 項目3: 効果テキストが空
# ══════════════════════════════════════════════

def test_check_card_empty_is_unchecked_empty():
    for card in ({"name": "A"}, {"name": "A", "effect_text": "", "pendulum_effect": "  \n"},
                 {"name": "A", "effect_text": None}):
        res = tc.check_card(card)
        assert res["status"] == "unchecked" and res["reason"] == "empty" and res["strong"] == 0


@pytest.mark.parametrize("effect", ["", "   \n", None])
def test_evaluate_empty_text_is_ineligible(effect):
    v = _ev(_card(effect_text=effect, card_type="通常魔法"))
    assert "empty_text" in v["reasons"] and v["eligible"] is False
    assert aa.reason_label("empty_text") != "empty_text"


def test_evaluate_empty_text_for_normal_monster_too():
    # 通常モンスター（フレーバーは effect_text に入る）でも空なら不適格
    v = _ev(_card(effect_text="", card_type="通常モンスター"))
    assert "empty_text" in v["reasons"]


def test_evaluate_nonempty_text_has_no_empty_reason():
    assert "empty_text" not in _ev(_card())["reasons"]


# ══════════════════════════════════════════════
# 項目4: 取り残し行の通知・import の位置
# ══════════════════════════════════════════════

def test_notify_stuck_rows(notices):
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=2)).isoformat()
    fresh = (now - timedelta(minutes=10)).isoformat()
    sb = _sb(posts=[
        {"card_id": 10, "status": "posting", "attempts": 0, "updated_at": old},
        {"card_id": 11, "status": "unknown", "attempts": 0, "updated_at": old},
        {"card_id": 12, "status": "posting", "attempts": 0, "updated_at": fresh},   # まだ新しい
        {"card_id": 13, "status": "posted", "attempts": 0, "updated_at": old},      # 完了済み
        {"card_id": 14, "status": "failed", "attempts": 1, "updated_at": old},      # 再試行対象
    ])
    assert xp.notify_stuck_rows(sb) == 2
    assert len(notices) == 1 and "card_id=10" in notices[0] and "card_id=11" in notices[0]
    assert "card_id=12" not in notices[0] and "card_id=13" not in notices[0]
    # 通知しても行は変えない（自動再投稿しない）
    assert _status(sb, 10) == "posting" and _status(sb, 11) == "unknown"


def test_notify_stuck_rows_none(notices):
    assert xp.notify_stuck_rows(_sb()) == 0 and notices == []


def test_run_backfill_notifies_stuck_rows(notices):
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    sb = _sb(posts=[{"card_id": 10, "status": "posting", "attempts": 0, "updated_at": old}])
    xp.run_backfill(sb, post_fn=Poster(), sleep=lambda s: None)
    assert any("card_id=10" in m for m in notices)


def test_run_backfill_survives_notify_failure(monkeypatch):
    sb = _sb()
    monkeypatch.setattr(xp, "notify_stuck_rows", lambda sb: (_ for _ in ()).throw(RuntimeError("db")))
    assert xp.run_backfill(sb, post_fn=Poster(), sleep=lambda s: None) == {"posted": 1}


def test_x_poster_import_failure_is_pre_send_failure(monkeypatch):
    """x_poster の import 失敗は例外で落とさず、送信前の失敗として記録する"""
    monkeypatch.setitem(sys.modules, "x_poster", None)  # import x_poster が ImportError になる
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=None, today=TODAY) == "failed"
    assert _rows(sb)[0]["status"] == "failed"


def test_og_check_failure_is_pre_send_failure(monkeypatch):
    monkeypatch.setattr(xp, "_verify_og_image", lambda card_id: (False, "OG画像の取得に失敗"))
    sb = _sb()
    poster = Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "failed"
    assert poster.calls == [] and _rows(sb)[0]["attempts"] == 1


# ══════════════════════════════════════════════
# 項目5: 認証情報未設定
# ══════════════════════════════════════════════

def test_not_configured_does_not_reserve_and_warns_once(monkeypatch, caplog):
    monkeypatch.delenv("X_ACCESS_TOKEN")
    sb = _sb()
    _add_card(sb, 2)
    poster = Poster()
    with caplog.at_level(logging.WARNING, logger=xp.logger.name):
        assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "not_configured"
        assert xp.post_card(sb, 2, post_fn=poster, today=TODAY) == "not_configured"
    assert _rows(sb) == [] and poster.calls == []
    warnings = [r for r in caplog.records if "認証情報" in r.getMessage()]
    assert len(warnings) == 1  # 1プロセス1回だけ


def test_not_configured_stops_sequence(monkeypatch):
    monkeypatch.delenv("X_API_KEY")
    sb = _sb()
    _add_card(sb, 2)
    assert xp.post_cards_sequential(sb, [1, 2], post_fn=Poster()) == {"not_configured": 1}


def test_dry_run_does_not_need_credentials(monkeypatch):
    monkeypatch.delenv("X_API_KEY")
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    assert xp.post_card(_sb(), 1, post_fn=Poster(), today=TODAY) == "dry_run"


# ══════════════════════════════════════════════
# 項目6: 承認記録の無いカード・同名重複
# ══════════════════════════════════════════════

def test_no_approved_at_not_posted():
    """approved_at が無い（導入前の承認）カードは、approval_snapshot があっても投稿しない"""
    for approved_at in (None, ""):
        sb = _sb(card={"approved_at": approved_at})
        poster = Poster()
        assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "not_postable"
        assert poster.calls == [] and _rows(sb) == []


def test_approved_at_alone_is_postable_without_snapshot():
    """approval_snapshot が無くても approved_at があれば投稿できる（記録は計測用で投稿条件ではない）"""
    for raw in ({}, None, {"approval_snapshot": "x"}):
        sb = _sb(card={"extraction_raw": raw})
        assert xp.post_card(sb, 1, post_fn=Poster(), today=TODAY) == "posted"


def test_duplicate_name_is_skipped_and_recorded():
    sb = _sb(card={"name": "ＡＢＣ"})
    _add_card(sb, 2)
    sb.tables["unreleased_cards"][1]["name"] = "ABC"   # 全角半角違いの同名
    poster = Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "posted"
    assert xp.post_card(sb, 2, post_fn=poster, today=TODAY) == "skipped_duplicate"
    assert len(poster.calls) == 1
    row = next(r for r in _rows(sb) if r["card_id"] == 2)
    assert row["status"] == "skipped_duplicate" and "card_id=1" in row["last_error"]
    # 記録済みなので回収の対象にならない
    assert xp.find_backfill_candidates(sb, TODAY) == []


def test_duplicate_detected_against_unknown():
    sb = _sb(posts=[{"card_id": 9, "status": "unknown", "attempts": 0}])
    sb.tables["unreleased_cards"].append({"id": 9, "name": "テストカード", "status": "approved", "hidden": False})
    assert xp.post_card(sb, 1, post_fn=Poster(), today=TODAY) == "skipped_duplicate"


def test_not_duplicate_when_other_not_posted():
    sb = _sb(posts=[{"card_id": 9, "status": "failed", "attempts": 1}])
    sb.tables["unreleased_cards"].append({"id": 9, "name": "テストカード", "status": "approved", "hidden": False})
    assert xp.post_card(sb, 1, post_fn=Poster(), today=TODAY) == "posted"


def test_duplicate_skip_replaces_retry_row_only():
    sb = _sb(posts=[{"card_id": 1, "status": "rate_limited", "attempts": 0},
                    {"card_id": 9, "status": "posted", "attempts": 0}])
    sb.tables["unreleased_cards"].append({"id": 9, "name": "テストカード", "status": "approved", "hidden": False})
    assert xp.post_card(sb, 1, post_fn=Poster(), today=TODAY) == "skipped_duplicate"
    assert _status(sb, 1) == "skipped_duplicate"


def test_duplicate_dry_run_leaves_no_row(monkeypatch):
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    sb = _sb(posts=[{"card_id": 9, "status": "posted", "attempts": 0}])
    sb.tables["unreleased_cards"].append({"id": 9, "name": "テストカード", "status": "approved", "hidden": False})
    assert xp.post_card(sb, 1, post_fn=Poster(), today=TODAY) == "skipped_duplicate"
    assert [r["card_id"] for r in _rows(sb)] == [9]


# ══════════════════════════════════════════════
# 項目7: 発売済みカード名
# ══════════════════════════════════════════════

def test_released_name_not_posted(monkeypatch):
    monkeypatch.setattr(xp, "_is_released_name", lambda name: name == "テストカード")
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "not_postable"
    assert poster.calls == [] and _rows(sb) == []


def test_is_released_name_uses_cardnames_index(monkeypatch):
    import name_normalize
    monkeypatch.setattr(name_normalize, "load_cardnames_index",
                        lambda base_dir=None: (["青眼の白龍", "E・HERO ネオス"], {"青眼の白龍", "E・HERO ネオス"},
                                               {name_normalize.fuzzy_key("青眼の白龍"): ["青眼の白龍"],
                                                name_normalize.fuzzy_key("E・HERO ネオス"): ["E・HERO ネオス"]}))
    monkeypatch.setattr(xp, "_released_index", None)
    monkeypatch.setattr(xp, "_is_released_name", _REAL_IS_RELEASED_NAME)
    assert xp._is_released_name("青眼の白龍") is True
    assert xp._is_released_name("Ｅ・ＨＥＲＯ ネオス") is True   # 全角半角・記号の違いは fuzzy で一致
    assert xp._is_released_name("新カード") is False
    assert xp._is_released_name("") is False


def test_real_cardnames_index_loads(monkeypatch):
    """実データ（data/cardnames_ja.json）で動くこと。実在する名前は発売済み、架空名は違う"""
    monkeypatch.setattr(xp, "_released_index", None)
    monkeypatch.setattr(xp, "_is_released_name", _REAL_IS_RELEASED_NAME)
    names, name_set, _ = __import__("name_normalize").load_cardnames_index()
    assert name_set, "data/cardnames_ja.json が読めない"
    assert xp._is_released_name(names[0]) is True
    assert xp._is_released_name("テスト用に存在しない架空のカード名ＺＺＺ") is False


# ══════════════════════════════════════════════
# 項目8: 承認記録（失敗しても止めない・最小記録）
# ══════════════════════════════════════════════

def test_safe_snapshot_falls_back_to_minimal(monkeypatch):
    def boom(card, via, now_iso=None):
        raise ValueError("壊れた値")
    monkeypatch.setattr(aa, "build_approval_snapshot", boom)
    snap = aa.build_approval_snapshot_safe({"id": 1}, "bulk")
    assert snap["via"] == "bulk" and "壊れた値" in snap["error"] and snap["at"]


def test_record_approval_snapshots_never_raises(monkeypatch):
    def boom(sb, entries):
        raise RuntimeError("db down")
    monkeypatch.setattr(aa, "save_approval_snapshots", boom)
    assert aa.record_approval_snapshots(FakeSB(), [{"id": 1}], "single") == 0


def test_record_writes_error_snapshot_so_card_stays_postable(monkeypatch):
    monkeypatch.setattr(aa, "build_approval_snapshot", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    sb = FakeSB({"unreleased_cards": [{"id": 1, "extraction_raw": {"crop_state": {"mode": "auto"}}}]})
    assert aa.record_approval_snapshots(sb, [{"id": 1}], "single") == 1
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw["approval_snapshot"]["error"] and raw["crop_state"] == {"mode": "auto"}


def _approval_row(rid=1, **over):
    row = {"id": rid, "name": f"カード{rid}", "source_url": "", "status": "pending", "release_date": FUTURE,
           "product_name": "商品X", "card_type": "効果モンスター", "attribute": "闇", "race": "ドラゴン族",
           "level": 4, "rank": None, "link_val": None, "atk": 1800, "def": "1000", "effect_text": "効果",
           "pendulum_scale": None, "pendulum_effect": "", "extraction_raw": {}}
    row.update(over)
    return row


def _admin_with_sb(monkeypatch, rows):
    """rows は FakeSB がコピーして持つ。更新後の確認は戻り値 sb.tables["unreleased_cards"] を見る"""
    sb = FakeSB({"unreleased_cards": rows})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, "resolve_release_date",
                        lambda product: {"status": "unverified", "release_date": None})
    return sb


class _SyncThread:
    def __init__(self, target, args=(), daemon=None, **kw):
        self._t, self._a = target, args

    def start(self):
        self._t(*self._a)


def test_single_approve_survives_snapshot_failure_and_stays_postable(monkeypatch):
    sb = _admin_with_sb(monkeypatch, [_approval_row()])
    rows = sb.tables["unreleased_cards"]
    monkeypatch.setattr(aa, "build_approval_snapshot", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (True, "ok"))
    started = []
    monkeypatch.setattr(admin_module._x_post, "start_post_thread", lambda sb_, ids: started.append(list(ids)))
    resp = _client().post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200 and rows[0]["status"] == "approved"
    snap = rows[0]["extraction_raw"]["approval_snapshot"]
    assert snap["via"] == "single" and snap["error"]    # 最小記録が書かれ、投稿条件を満たす
    assert started == [[1]]


def test_single_approve_survives_snapshot_write_failure(monkeypatch):
    rows = _admin_with_sb(monkeypatch, [_approval_row()]).tables["unreleased_cards"]
    monkeypatch.setattr(aa, "save_approval_snapshots", lambda sb, entries: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda **kw: (True, "ok"))
    monkeypatch.setattr(admin_module._x_post, "start_post_thread", lambda sb_, ids: None)
    resp = _client().post("/api/admin/unreleased/1/approve", headers=HEADERS)
    assert resp.status_code == 200 and "approval_snapshot" not in rows[0]["extraction_raw"]  # 書けなければ無し＝投稿されない


def test_bulk_approve_builds_snapshots_inside_thread_before_posting(monkeypatch):
    rows = _admin_with_sb(monkeypatch, [_approval_row(1), _approval_row(2)]).tables["unreleased_cards"]
    monkeypatch.setattr(admin_module, "_try_fetch_image_from_extraction", lambda card_id, **kw: (True, "x"))
    order = []
    real_build = aa.build_approval_snapshot

    def build(card, via, now_iso=None):
        order.append(("build", threading.current_thread() is threading.main_thread()))
        if card["id"] == 2:
            raise ValueError("壊れた行")      # 1枚の作成失敗が他を止めない
        return real_build(card, via, now_iso)

    monkeypatch.setattr(aa, "build_approval_snapshot", build)
    seen_at_post = []

    def fake_post(sb_, ids, **kw):
        seen_at_post.append({r["id"]: "approval_snapshot" in r["extraction_raw"] for r in rows})

    monkeypatch.setattr(admin_module._x_post, "post_cards_sequential", fake_post)
    started = {}

    class RecordingThread(_SyncThread):
        def start(self):
            started["before"] = len(order)  # スレッド開始前には作成されていない
            super().start()

    monkeypatch.setattr(admin_module.threading, "Thread", RecordingThread)
    resp = _client().post("/api/admin/unreleased/bulk-approve", headers=HEADERS, json={"ids": [1, 2]})
    assert resp.status_code == 200 and started["before"] == 0 and len(order) == 2
    assert seen_at_post == [{1: True, 2: True}]   # 投稿の時点で全員の承認記録が書き込み済み
    assert rows[1]["extraction_raw"]["approval_snapshot"]["error"]


# ══════════════════════════════════════════════
# 項目9: annotate_row の例外ガード
# ══════════════════════════════════════════════

def test_annotate_row_survives_non_string_fields():
    ctx = tc.TextContext()
    row = _card(effect_text=12345, pendulum_effect=["a"])
    verdict = aa.annotate_row(row, [], ctx)
    assert verdict["eligible"] is False
    raw = row["extraction_raw"]
    assert raw["auto_approve_verdict"] and raw["text_check"]["status"] == "unchecked"
    assert raw["extracted_snapshot"]["effect_text"] == 12345


def test_annotate_row_survives_snapshot_and_context_failures(monkeypatch, caplog):
    monkeypatch.setattr(aa, "build_extracted_snapshot", lambda row: (_ for _ in ()).throw(TypeError("snap")))
    ctx = tc.TextContext()
    monkeypatch.setattr(ctx, "add_card", lambda card: (_ for _ in ()).throw(TypeError("ctx")))
    row = _card()
    with caplog.at_level(logging.WARNING):
        verdict = aa.annotate_row(row, [], ctx)
    assert "eligible" in verdict and "auto_approve_verdict" in row["extraction_raw"]
    assert "extracted_snapshot" not in row["extraction_raw"]
    assert any("スナップショット" in r.getMessage() for r in caplog.records)
    assert any("文脈への追加" in r.getMessage() for r in caplog.records)


def test_text_context_add_card_with_non_string_raises_but_is_guarded():
    # 素の add_card は文字列でない値で例外になる（annotate_row 側のガードが必要な理由）
    with pytest.raises(TypeError):
        tc.TextContext().add_card({"effect_text": 5})


# ══════════════════════════════════════════════
# 項目10: 編集保存時の再計算・キャッシュ
# ══════════════════════════════════════════════

@pytest.fixture(autouse=False)
def fresh_context_cache(monkeypatch):
    monkeypatch.setitem(aa._context_cache, "ctx", None)
    monkeypatch.setitem(aa._context_cache, "sb", None)


class CountingSB(FakeSB):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.calls = []

    def table(self, name):
        self.calls.append(name)
        return super().table(name)


def _ann_row(rid, name, effect, status="pending", annotated=True):
    raw = {"auto_approve_verdict": {"eligible": True, "reasons": []},
           "text_check": {"status": "checked", "strong": 3}} if annotated else {}
    return {"id": rid, "name": name, "effect_text": effect, "pendulum_effect": "", "status": status,
            "extraction_raw": raw, "card_type": "効果モンスター"}


def test_put_recomputes_text_check_and_verdict(monkeypatch, fresh_context_cache):
    sb = CountingSB({"unreleased_cards": [_ann_row(1, "カードA", "旧"), _ann_row(2, "カードB", "「テーマ」")]})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    seen = {}

    def fake_check(card, others, ctx, **kw):
        seen.update(card=card, others=others, word_df=dict(ctx.word_df) if ctx else None, kw=kw)
        return {"version": 1, "status": "checked", "reason": None, "strong": 0, "weak": 0,
                "suspects": [], "unverified_fields": []}

    monkeypatch.setattr(aa, "compute_text_check", fake_check)
    resp = _client().put("/api/admin/unreleased/1", headers=HEADERS, json={"effect_text": "直した文章。"})
    assert resp.status_code == 200
    raw = sb.tables["unreleased_cards"][0]["extraction_raw"]
    assert raw["text_check"]["strong"] == 0                       # 取込時の strong=3 が更新された
    assert raw["auto_approve_verdict"]["reasons"]                  # 再評価された（認識確認などが無いので不適格）
    assert "recognition_not_verified" in raw["auto_approve_verdict"]["reasons"]
    assert seen["card"]["effect_text"] == "直した文章。"
    assert seen["others"] == ["カードB"] and seen["kw"] == {"context_has_self": False}  # 自分は除く
    assert seen["word_df"] == {"テーマ": 1}
    assert resp.get_json()["card"]["extraction_raw"]["text_check"]["strong"] == 0


def test_put_does_not_add_checks_to_unannotated_cards(monkeypatch, fresh_context_cache):
    sb = CountingSB({"unreleased_cards": [_ann_row(1, "手動カード", "旧", annotated=False)]})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    resp = _client().put("/api/admin/unreleased/1", headers=HEADERS, json={"effect_text": "新"})
    assert resp.status_code == 200 and sb.tables["unreleased_cards"][0]["extraction_raw"] == {}


def test_put_succeeds_even_if_recompute_fails(monkeypatch, fresh_context_cache):
    sb = CountingSB({"unreleased_cards": [_ann_row(1, "カードA", "旧")]})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    monkeypatch.setattr(aa, "recompute_card_checks", lambda sb_, card: (_ for _ in ()).throw(RuntimeError("x")))
    resp = _client().put("/api/admin/unreleased/1", headers=HEADERS, json={"effect_text": "新"})
    assert resp.status_code == 200 and sb.tables["unreleased_cards"][0]["effect_text"] == "新"


def test_context_cache_reuses_load_within_ttl(fresh_context_cache):
    sb = CountingSB({"unreleased_cards": [_ann_row(1, "A", "「語」"), _ann_row(2, "B", "x")]})
    now = [100.0]
    first = aa.get_card_context_cached(sb, ttl=60, clock=lambda: now[0])
    loads = len(sb.calls)
    assert aa.get_card_context_cached(sb, ttl=60, clock=lambda: now[0]) is first and len(sb.calls) == loads
    now[0] += 61
    assert aa.get_card_context_cached(sb, ttl=60, clock=lambda: now[0]) is not first and len(sb.calls) > loads
    assert aa.get_card_context_cached(sb, ttl=0) is not first  # ttl=0 は常に読み直す


def test_card_context_excludes_self_by_id(fresh_context_cache):
    sb = FakeSB({"unreleased_cards": [_ann_row(1, "A", "「語」"), _ann_row(2, "B", "「語」「別」"),
                                      _ann_row(3, "C", "「語」", status="rejected")]})
    ctx = aa.load_card_context(sb)
    others, tctx = ctx.excluding(1)
    assert others == ["B"] and tctx.word_df == {tc.name_key("語"): 1, tc.name_key("別"): 1}
    # 文脈に入っていない（rejected の）カードを除いても、他カードの分は減らない
    others3, tctx3 = ctx.excluding(3)
    assert others3 == ["A", "B"] and tctx3.word_df[tc.name_key("語")] == 2


def test_load_card_context_failure_returns_none():
    class Broken:
        def table(self, name):
            raise RuntimeError("db")
    assert aa.load_card_context(Broken()) is None


def test_recompute_without_context_is_ineligible(monkeypatch, fresh_context_cache):
    monkeypatch.setattr(aa, "load_card_context", lambda sb: None)
    card = _card()
    card["id"] = 1
    res = aa.recompute_card_checks(FakeSB(), card)
    assert "same_name_unchecked" in res["auto_approve_verdict"]["reasons"]


# ══════════════════════════════════════════════
# 項目11: 強調位置（context_start）
# ══════════════════════════════════════════════

def test_suspects_carry_context_start_matching_context():
    d = tc.TextCheckDict(["あいう"], [], [])
    text = "あ" * 3 + "。(誤り)" + "さしすせそ。"
    for sus in tc.check_text(text, dictionary=d):
        cs = sus["context_start"]
        assert text[cs:cs + len(sus["context"])] == sus["context"]
        assert sus["context"][sus["start"] - cs:sus["end"] - cs] == sus["text"]


def test_ngram_suspect_context_start(monkeypatch):
    d = tc.TextCheckDict(["発動で", "動でき", "ができ", "きる。"], [], [])
    text = "このカードは発動できるQ。"
    sus_list = [s for s in tc.check_text(text, dictionary=d) if s["kind"] == "unknown_ngram"]
    assert sus_list
    for sus in sus_list:
        cs = sus["context_start"]
        assert text[cs:cs + len(sus["context"])] == sus["context"]
        assert sus["context"][sus["start"] - cs:sus["end"] - cs] == sus["text"]


# ══════════════════════════════════════════════
# 項目12: 再検査APIの文脈
# ══════════════════════════════════════════════

def test_recheck_loads_everything_once_and_does_not_subtract_missing_card(monkeypatch):
    sb = CountingSB({"unreleased_cards": [
        _ann_row(1, "A", "「語」"), _ann_row(2, "B", "「語」"),
        _ann_row(3, "R", "「語」", status="rejected"),   # rejected は文脈に入っていない
    ]})
    monkeypatch.setattr(admin_module, "_supabase", sb, raising=False)
    monkeypatch.setattr(admin_module, "_ADMIN_KEY", "test-key", raising=False)
    seen = {}

    def fake_check(card, others, ctx, **kw):
        seen["word_df"] = dict(ctx.word_df)
        seen["others"] = others
        return {"version": 1, "status": "checked", "reason": None, "strong": 0, "weak": 0,
                "suspects": [], "unverified_fields": []}

    monkeypatch.setattr(aa, "compute_text_check", fake_check)
    resp = _client().post("/api/admin/unreleased/3/text-check", headers=HEADERS, json={})
    assert resp.status_code == 200
    assert seen["word_df"] == {tc.name_key("語"): 2}    # A と B の2枚分。R(rejected) の分を引いて1にならない
    assert seen["others"] == ["A", "B"]
    # unreleased_cards の読み込み: 対象1回 + 全件1回（旧実装は名前と文脈で全件2回）
    assert sb.calls.count("unreleased_cards") == 2


# ══════════════════════════════════════════════
# 項目13: 投稿間隔のプロセス内共有
# ══════════════════════════════════════════════

def test_interval_shared_across_threads(monkeypatch):
    monkeypatch.setattr(xp, "_clock", lambda: 1000.0)   # 時間が進まない → 2回目以降は毎回 interval 待つ
    sb = _sb()
    for cid in (2, 3):
        _add_card(sb, cid)
    sleeps, inflight, peak = [], [0], [0]
    guard = threading.Lock()

    def poster(text, *args, **kw):
        with guard:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05)   # 並行実行されれば重なる長さ
        with guard:
            inflight[0] -= 1
        return ("1", None)

    threads = [threading.Thread(target=xp.post_card, args=(sb, cid),
                                kwargs={"post_fn": poster, "today": TODAY, "interval": 20,
                                        "sleep": sleeps.append}) for cid in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r["status"] for r in _rows(sb)) == ["posted"] * 3
    assert peak[0] == 1                      # 同時に投稿されない
    assert sleeps == [20, 20]                # 最初の1枚以外は間隔を空ける


def test_interval_not_waited_when_enough_time_passed(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(xp, "_clock", lambda: now[0])
    sb = _sb()
    _add_card(sb, 2)
    sleeps = []
    xp.post_card(sb, 1, post_fn=Poster(), today=TODAY, interval=20, sleep=sleeps.append)
    now[0] += 15
    xp.post_card(sb, 2, post_fn=Poster(), today=TODAY, interval=20, sleep=sleeps.append)
    assert sleeps == [5]   # 残りの分だけ待つ
