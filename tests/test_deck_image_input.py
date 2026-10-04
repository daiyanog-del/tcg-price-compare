"""画像生成入口の入力・負荷制御と、失敗後の枠解放を検証する。"""
from unittest.mock import Mock
import json
import logging
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


def test_generation_metrics_do_not_include_user_input(client, caplog):
    with caplog.at_level(logging.INFO, logger=server.__name__):
        assert client.post('/api/deck-image', json=VALID).status_code == 200
    messages = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith('deck_image_metrics ')]
    assert len(messages) == 1
    data = json.loads(messages[0].split(' ', 1)[1])
    assert data['event'] == 'completed'
    assert data['duration_sec'] >= 0
    assert data['pid'] > 0
    assert VALID['name'] not in messages[0]
    assert VALID['cards'][0]['name'] not in messages[0]


def test_metrics_failure_preserves_success_and_capacity_response(client, monkeypatch):
    monkeypatch.setattr(server, '_deck_image_metrics_log_errors', 0)
    monkeypatch.setattr(server.logger, 'info', Mock(side_effect=RuntimeError('ログ出力失敗')))
    assert client.post('/api/deck-image', json=VALID).status_code == 200
    server._reset_rate_limits()
    assert server._deck_image_slot.acquire(blocking=False)
    try:
        assert client.post('/api/deck-image', json=VALID).status_code == 503
    finally:
        server._deck_image_slot.release()
    assert server._deck_image_metrics_log_errors == 2
