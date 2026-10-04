"""画像生成入口の入力・負荷制御と、失敗後の枠解放を検証する。"""
from unittest.mock import Mock
import pytest
import app as server
import deck_image


VALID = {"name": "確認デッキ", "cards": [{"name": "青眼の白龍", "qty": 1}], "total": 20}


@pytest.fixture
def client(monkeypatch):
    server._reset_rate_limits()
    monkeypatch.setattr(deck_image, "generate_deck_image", Mock(return_value=b"png"))
    return server.app.test_client()


@pytest.mark.parametrize("body", [[1], None, {**VALID, "total": "文字"}, {**VALID, "total": True}, {**VALID, "total": -1}, {**VALID, "cards": []}, {**VALID, "cards": VALID["cards"] * 61}, {**VALID, "cards": [{"name": "a", "qty": 0}]}, {**VALID, "cards": [{"name": "a", "qty": 1.5}]}, {**VALID, "cards": [{"name": "a" * 51}]}, {**VALID, "name": []}])
def test_invalid_input_never_starts_generation(client, body):
    response = client.post("/api/deck-image", json=body)
    assert response.status_code == 400
    assert response.get_json()["error"]
    deck_image.generate_deck_image.assert_not_called()


def test_valid_request_and_rate_limit(client):
    assert client.post("/api/deck-image", json=VALID).status_code == 200
    assert client.post("/api/deck-image", json=VALID).status_code == 429
    assert deck_image.generate_deck_image.call_count == 1


def test_busy_generation_returns_retryable_error(client):
    assert server._deck_image_slot.acquire(blocking=False)
    try:
        response = client.post("/api/deck-image", json=VALID)
        assert response.status_code == 503
        assert response.headers["Retry-After"]
        deck_image.generate_deck_image.assert_not_called()
    finally:
        server._deck_image_slot.release()


def test_failure_releases_generation_slot(client):
    deck_image.generate_deck_image.side_effect = RuntimeError("テスト用合成エラー")
    assert client.post("/api/deck-image", json=VALID).status_code == 500
    assert server._deck_image_slot.acquire(blocking=False)
    server._deck_image_slot.release()
