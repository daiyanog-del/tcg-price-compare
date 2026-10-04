"""既存カードの枠提案はDB由来の画像だけを使用し、保存しない。"""
import base64
import io
from types import SimpleNamespace

import pytest
from flask import Flask
from PIL import Image

import admin_unreleased as admin
import crop_suggestion_service as service


class Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_args):
        return self

    def eq(self, key, value):
        self.rows = [row for row in self.rows if row.get(key) == value]
        return self

    def is_(self, key, _value):
        self.rows = [row for row in self.rows if row.get(key) is None]
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(admin, '_ADMIN_KEY', 'test-crop-key')
    monkeypatch.setattr(admin, '_check_global_auth_rate_limit', lambda: False)
    monkeypatch.setattr(admin, '_check_auth_rate_limit', lambda _ip: False)
    tables = {'unreleased_cards': [{'id': 1, 'name': '対象カード'}],
              'official_card_images': [{'unreleased_card_id': 1, 'source_image_url': 'https://pbs.twimg.com/media/test.jpg', 'hidden': False}]}
    monkeypatch.setattr(admin, '_supabase', SimpleNamespace(table=lambda key: Query(tables[key])))
    app = Flask(__name__)
    app.register_blueprint(admin.admin_bp)
    return app.test_client(), tables


def test_route_uses_db_only_without_writes(client, monkeypatch):
    http, _ = client
    calls = []
    monkeypatch.setattr(service, 'suggest_crop', lambda name, url: calls.append((name, url)) or {'left': .1})
    response = http.post('/api/admin/unreleased/1/suggest-crop', json={}, headers={'X-Admin-Key': 'test-crop-key'})
    assert response.status_code == 200
    assert calls == [('対象カード', 'https://pbs.twimg.com/media/test.jpg')]
    response = http.post('/api/admin/unreleased/1/suggest-crop', json={'url': 'https://evil.test/'}, headers={'X-Admin-Key': 'test-crop-key'})
    assert response.status_code == 400
    assert len(calls) == 1


def test_route_auth_missing_card_and_missing_image(client, monkeypatch):
    http, tables = client
    monkeypatch.setattr(service, 'suggest_crop', lambda *_: pytest.fail('API呼出は禁止'))
    assert http.post('/api/admin/unreleased/1/suggest-crop', json={}).status_code == 401
    headers = {'X-Admin-Key': 'test-crop-key'}
    assert http.post('/api/admin/unreleased/2/suggest-crop', json={}, headers=headers).status_code == 404
    tables['official_card_images'].clear()
    assert http.post('/api/admin/unreleased/1/suggest-crop', json={}, headers=headers).status_code == 422


def test_service_generates_box_with_configured_model(monkeypatch):
    import anthropic
    import unreleased_extractor as extractor
    output = io.BytesIO()
    Image.new('RGB', (100, 150), 'white').save(output, 'PNG')
    url = 'https://pbs.twimg.com/media/test.jpg'
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test')
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **_kwargs: object())
    monkeypatch.setattr(extractor, '_download_and_encode_images', lambda urls: [{
        'url': urls[0], 'data': base64.b64encode(output.getvalue()).decode(), 'media_type': 'image/png', 'width': 100, 'height': 150}])
    calls = []
    monkeypatch.setattr(service, 'structured_request', lambda client, **kwargs: calls.append(kwargs) or ({'bbox': [0, 0, 100, 150]}, '', {}))
    result = service.suggest_crop('対象カード', url)
    assert result['source_image_url'] == url
    assert result['model'] == extractor.EXTRACTOR_MODEL
    assert result['needs_review'] is True
    assert result['right'] == 1
    assert calls[0]['model'] == extractor.EXTRACTOR_MODEL
    array_schema = calls[0]['schema']['properties']['bbox']['anyOf'][0]
    assert 'minItems' not in array_schema
    assert 'maxItems' not in array_schema
    for invalid_bbox in (None, [], [0, 0, 100], [0, 0, 100, 150, 200]):
        monkeypatch.setattr(service, 'structured_request', lambda *args, **kwargs: ({'bbox': invalid_bbox}, '', {}))
        with pytest.raises(ValueError, match='特定できません'):
            service.suggest_crop('対象カード', url)


@pytest.mark.parametrize('failure, status', [(ValueError('枠がありません'), 422), (RuntimeError('内部情報'), 502)])
def test_route_failure_is_explicit(client, monkeypatch, failure, status):
    http, _ = client
    def fail(*_args):
        raise failure
    monkeypatch.setattr(service, 'suggest_crop', fail)
    response = http.post('/api/admin/unreleased/1/suggest-crop', json={}, headers={'X-Admin-Key': 'test-crop-key'})
    assert response.status_code == status
    assert 'error' in response.json
    assert '内部情報' not in response.json['error']
