"""x_poster.post_tweet の require_media（画像必須）の挙動テスト。tweepy はダミーに差し替える。"""

import sys
import types

import pytest

import x_poster


class _FakeClient:
    created = []

    def __init__(self, **kwargs):
        pass

    def create_tweet(self, **kwargs):
        _FakeClient.created.append(kwargs)
        return types.SimpleNamespace(data={"id": "777"})


def _install_fake_tweepy(monkeypatch, upload_ok: bool):
    class _API:
        def __init__(self, auth, **kwargs):
            pass

        def media_upload(self, path):
            if not upload_ok:
                raise RuntimeError("upload failed")
            return types.SimpleNamespace(media_id=12345)

    fake = types.SimpleNamespace(
        __version__="test",
        OAuth1UserHandler=lambda *a, **k: object(),
        API=_API,
        Client=_FakeClient,
    )
    monkeypatch.setitem(sys.modules, "tweepy", fake)
    _FakeClient.created = []


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET"):
        monkeypatch.setenv(k, "x")
    monkeypatch.delenv("X_POST_DRY_RUN", raising=False)


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "a.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0test")
    return str(p)


def test_default_posts_text_only_when_upload_fails(monkeypatch, img):
    """既存の挙動: 画像が付かなくても本文だけで投稿する（変更しない）"""
    _install_fake_tweepy(monkeypatch, upload_ok=False)
    assert x_poster.post_tweet("本文", [img]) == "777"
    assert "media_ids" not in _FakeClient.created[0]


def test_require_media_blocks_when_upload_fails(monkeypatch, img):
    _install_fake_tweepy(monkeypatch, upload_ok=False)
    assert x_poster.post_tweet("本文", [img], require_media=True) is None
    assert _FakeClient.created == []


def test_require_media_blocks_when_no_image_paths(monkeypatch):
    _install_fake_tweepy(monkeypatch, upload_ok=True)
    assert x_poster.post_tweet("本文", None, require_media=True) is None
    assert _FakeClient.created == []


def test_require_media_posts_with_media(monkeypatch, img):
    _install_fake_tweepy(monkeypatch, upload_ok=True)
    assert x_poster.post_tweet("本文", [img], require_media=True) == "777"
    assert _FakeClient.created[0]["media_ids"] == ["12345"]
