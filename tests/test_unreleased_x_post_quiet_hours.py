"""未発売カード X 投稿の「投稿しない時間帯」と tweet_log 記録のテスト（2026-10-21 追加）。"""
from datetime import datetime

import pytest

import unreleased_x_post as xp
from constants import JST


def _jst(hour):
    return datetime(2026, 10, 22, hour, 30, tzinfo=JST)


@pytest.mark.parametrize("env,hour,expected", [
    ("", 0, False), ("", 1, True), ("", 3, True), ("", 6, True), ("", 7, False), ("", 23, False),
    ("2-5", 1, False), ("2-5", 4, True),
    ("23-6", 23, True), ("23-6", 2, True), ("23-6", 6, False), ("23-6", 12, False),
    ("off", 3, False),
    ("壊れた値", 3, True), ("5-5", 3, True), ("1-25", 3, True),  # 読めない値は既定(1-7)に戻す
])
def test_in_quiet_hours(monkeypatch, env, hour, expected):
    monkeypatch.setenv(xp.QUIET_HOURS_ENV, env)
    assert xp.in_quiet_hours(_jst(hour)) is expected


def test_post_card_holds_in_quiet_hours_without_reserving(monkeypatch):
    """時間帯の中では予約せず quiet_hours を返す（行が無いので明けた後の回収で拾われる）。"""
    monkeypatch.setenv("UNRELEASED_X_POST_ENABLED", "1")
    monkeypatch.setattr(xp, "in_quiet_hours", lambda now=None: True)  # 実行時刻に依らず時間帯の中にする
    for k in xp._X_CRED_ENVS:
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)
    card = {"id": 1, "name": "テストカード", "status": "approved", "hidden": False,
            "approved_at": "2026-10-21T14:00:00+00:00", "product_name": "P", "release_date": "2099-01-01",
            "source_url": ""}
    monkeypatch.setattr(xp, "_fetch_card", lambda sb, cid: card)
    monkeypatch.setattr(xp, "_fetch_visible_image_url", lambda sb, cid: "https://example.com/a.png")
    monkeypatch.setattr(xp, "is_postable", lambda c, has_img, today: (True, ""))
    monkeypatch.setattr(xp, "_is_released_name", lambda name: False)
    monkeypatch.setattr(xp, "reserve", lambda sb, cid: pytest.fail("時間帯の中で予約してはいけない"))
    sent = []
    assert xp.post_card(_Recorder(), 1, post_fn=lambda *a, **k: sent.append(a)) == "quiet_hours"
    assert sent == []
    assert "quiet_hours" in xp._STOP_RESULTS


class _Recorder:
    def __init__(self):
        self.calls = []

    def table(self, name):
        rec = self

        class _Q:
            def insert(self, row):
                rec.calls.append((name, "insert", row))
                return self

            def delete(self):
                rec.calls.append((name, "delete", None))
                return self

            def eq(self, k, v):
                rec.calls.append((name, "eq", (k, v)))
                return self

            def execute(self):
                return None
        return _Q()


def test_log_tweet_records_content_type():
    sb = _Recorder()
    xp._log_tweet(sb, 123)
    name, op, row = sb.calls[0]
    assert (name, op) == ("tweet_log", "insert")
    assert row["tweet_id"] == "123" and row["content_type"] == "unreleased_card"


def test_log_tweet_failure_does_not_raise():
    class _Broken:
        def table(self, name):
            raise RuntimeError("db down")
    xp._log_tweet(_Broken(), 1)  # 例外を投げない


def test_delete_posts_also_removes_tweet_log(monkeypatch):
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)
    sb = _Recorder()
    counts = xp.delete_posts(sb, [{"card_id": 1, "tweet_id": "999", "name": "n", "status": "posted"}],
                             delete_fn=lambda tid: (True, None))
    assert counts == {"deleted": 1}
    assert ("tweet_log", "eq", ("tweet_id", "999")) in sb.calls
