"""未発売カードのX自動投稿（unreleased_x_post.py）のテスト。"""
import os
import tempfile
from datetime import date

import pytest

import unreleased_x_post as xp
from tests.fake_supabase import FakeSB

TODAY = date(2026, 10, 15)


def _fake_download(url):
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    return path


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("UNRELEASED_X_POST_ENABLED", "1")
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)
    monkeypatch.setattr(xp, "_notify", lambda msg: None)
    monkeypatch.setattr(xp, "_site_url", lambda: "https://example.test")
    monkeypatch.setattr(xp, "_download_image", _fake_download)


def _sb(card=None, image=True, posts=None):
    card = {"id": 1, "name": "テストカード", "product_name": "テストパック", "release_date": "2026-11-01",
            "status": "approved", "hidden": False, **(card or {})}
    return FakeSB({
        "unreleased_cards": [card],
        "official_card_images": ([{"unreleased_card_id": card["id"], "public_url": "https://img.test/1.jpg",
                                   "hidden": False, "deleted_at": None}] if image else []),
        "unreleased_x_posts": posts or [],
    })


class Poster:
    def __init__(self, result="111"):
        self.result, self.calls = result, []

    def __call__(self, text, paths):
        self.calls.append((text, list(paths)))
        return self.result


# ── 本文 ──

def test_text_full():
    t = xp.build_tweet_text("テストカード", "テストパック", "2026-11-01", site_url="https://example.test")
    assert t.split("\n") == [
        "【新カード情報】", "テストカード", "収録：テストパック", "発売日：2026年11月1日",
        "https://example.test/card/%E3%83%86%E3%82%B9%E3%83%88%E3%82%AB%E3%83%BC%E3%83%89",
        "#遊戯王 #OCG"]


def test_text_without_date_and_product():
    t = xp.build_tweet_text("A", "", None, site_url="https://example.test")
    assert "発売日" not in t and "収録" not in t
    assert t.split("\n")[1] == "A"


def test_text_invalid_date_omitted():
    assert "発売日" not in xp.build_tweet_text("A", "P", "bad", site_url="https://example.test")


def test_text_truncated_within_limit():
    name = "あ" * 60
    product = "商品名" * 80
    t = xp.build_tweet_text(name, product, "2026-11-01", site_url="https://example.test")
    assert xp.weighted_length(t) <= xp.TWEET_WEIGHT_LIMIT
    assert "…" in t
    # 商品名を先に詰めるので、カード名は残る
    assert name in t


def test_text_name_truncated_when_still_over():
    name = "あ" * 300
    t = xp.build_tweet_text(name, "P", None, site_url="https://example.test")
    assert xp.weighted_length(t) <= xp.TWEET_WEIGHT_LIMIT
    line = t.split("\n")[1]
    assert line != name and line.endswith("…")


def test_weighted_length_url_and_cjk():
    assert xp.weighted_length("abc") == 3
    assert xp.weighted_length("あい") == 4
    assert xp.weighted_length("x https://example.test/very/long/path/" + "a" * 100) == 2 + xp.URL_WEIGHT


# ── 条件判定 ──

@pytest.mark.parametrize("card,img,ok,reason", [
    ({"status": "approved", "hidden": False, "release_date": None}, True, True, ""),
    ({"status": "approved", "hidden": False, "release_date": "2026-10-15"}, True, True, ""),
    ({"status": "approved", "hidden": False, "release_date": "2026-10-14"}, True, False, "already_released"),
    ({"status": "pending", "hidden": False, "release_date": None}, True, False, "status_not_approved"),
    ({"status": "linked", "hidden": False, "release_date": None}, True, False, "status_not_approved"),
    ({"status": "approved", "hidden": True, "release_date": None}, True, False, "hidden"),
    ({"status": "approved", "hidden": False, "release_date": None}, False, False, "no_visible_image"),
])
def test_is_postable(card, img, ok, reason):
    assert xp.is_postable(card, img, TODAY) == (ok, reason)


# ── 投稿・予約 ──

def test_post_success_records_posted():
    sb, poster = _sb(), Poster("999")
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "posted"
    row = sb.tables["unreleased_x_posts"][0]
    assert row["status"] == "posted" and row["tweet_id"] == "999"
    assert len(poster.calls) == 1 and poster.calls[0][1]  # 画像パスが渡る


def test_double_post_prevented():
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "posted"
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"
    assert len(poster.calls) == 1


@pytest.mark.parametrize("status", ["posting", "posted", "skipped_legacy"])
def test_existing_row_blocks(status):
    sb, poster = _sb(posts=[{"card_id": 1, "status": status, "attempts": 0}]), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"
    assert poster.calls == []


def test_none_from_post_tweet_is_failure():
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=Poster(None), today=TODAY) == "failed"
    row = sb.tables["unreleased_x_posts"][0]
    assert row["status"] == "failed" and row["attempts"] == 1 and row["last_error"]


def test_failed_retried_until_limit():
    sb = _sb()
    for expected in (1, 2, 3):
        assert xp.post_card(sb, 1, post_fn=Poster(None), today=TODAY) == "failed"
        assert sb.tables["unreleased_x_posts"][0]["attempts"] == expected
    poster = Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "already"  # 上限到達で再試行しない
    assert poster.calls == []


def test_failed_then_success():
    sb = _sb()
    xp.post_card(sb, 1, post_fn=Poster(None), today=TODAY)
    assert xp.post_card(sb, 1, post_fn=Poster("5"), today=TODAY) == "posted"
    assert sb.tables["unreleased_x_posts"][0]["status"] == "posted"


def test_exception_in_post_fn_is_failure():
    def boom(text, paths):
        raise RuntimeError("API down")
    sb = _sb()
    assert xp.post_card(sb, 1, post_fn=boom, today=TODAY) == "failed"
    assert "API down" in sb.tables["unreleased_x_posts"][0]["last_error"]


def test_dry_run_leaves_no_row(monkeypatch):
    monkeypatch.setenv("X_POST_DRY_RUN", "1")
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "dry_run"
    assert sb.tables["unreleased_x_posts"] == [] and poster.calls == []


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("UNRELEASED_X_POST_ENABLED")
    sb, poster = _sb(), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "disabled"
    assert sb.tables["unreleased_x_posts"] == [] and poster.calls == []


def test_no_image_not_posted():
    sb, poster = _sb(image=False), Poster()
    assert xp.post_card(sb, 1, post_fn=poster, today=TODAY) == "not_postable"
    assert sb.tables["unreleased_x_posts"] == []


def test_reserve_race_only_one_wins():
    sb = _sb()
    assert xp.reserve(sb, 1) == "reserved"
    assert xp.reserve(sb, 1) == "taken"


def test_reserve_takeover_only_failed_under_limit():
    sb = _sb(posts=[{"card_id": 1, "status": "failed", "attempts": 1}])
    assert xp.reserve(sb, 1) == "reserved"
    assert sb.tables["unreleased_x_posts"][0]["status"] == "posting"
    assert xp.reserve(sb, 1) == "taken"


def test_temp_file_removed(monkeypatch):
    made = []

    def dl(url):
        p = _fake_download(url)
        made.append(p)
        return p
    monkeypatch.setattr(xp, "_download_image", dl)
    xp.post_card(_sb(), 1, post_fn=Poster(), today=TODAY)
    assert made and not os.path.exists(made[0])


# ── 連続投稿・回収 ──

def _add_card(sb, cid, *, image=True, release_date=None):
    sb.tables["unreleased_cards"].append({"id": cid, "name": f"C{cid}", "product_name": "", "release_date": release_date,
                                          "status": "approved", "hidden": False})
    if image:
        sb.tables["official_card_images"].append({"unreleased_card_id": cid, "public_url": "u", "hidden": False,
                                                  "deleted_at": None})


def test_sequential_waits_only_after_attempt():
    sb = _sb()
    _add_card(sb, 2)
    sleeps = []
    xp.post_cards_sequential(sb, [1, 2], interval=20, post_fn=Poster(), sleep=sleeps.append)
    assert sleeps == [20]


def test_backfill_candidates():
    sb = _sb()  # id=1: 画像あり・行なし → 候補
    _add_card(sb, 2, image=False)                    # 画像なし
    _add_card(sb, 3)                                 # legacy
    _add_card(sb, 4)                                 # failed attempts=1 → 候補
    _add_card(sb, 5)                                 # failed attempts=3
    _add_card(sb, 6, release_date="2026-10-01")      # 発売済み（日付絞りで除外）
    sb.tables["unreleased_x_posts"] += [
        {"card_id": 3, "status": "skipped_legacy", "attempts": 0},
        {"card_id": 4, "status": "failed", "attempts": 1},
        {"card_id": 5, "status": "failed", "attempts": 3},
    ]
    assert xp.find_backfill_candidates(sb, TODAY) == [1, 4]


def test_run_backfill_posts_candidates(monkeypatch):
    sb = _sb()
    poster = Poster()
    assert xp.run_backfill(sb, post_fn=poster, sleep=lambda s: None) == {"posted": 1}
    assert len(poster.calls) == 1
