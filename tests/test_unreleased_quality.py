"""抽出失敗の区別、画像とカードの対応、再読と日付根拠を検証する。"""
import base64
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unreleased_quality as quality
import unreleased_extractor as extractor


def test_structured_schema_is_closed():
    _, result = extractor._get_pydantic_models()
    schema = quality.strict_schema(result.model_json_schema())
    card = schema['$defs']['ExtractedCard']
    assert card['additionalProperties'] is False
    assert set(card['required']) == set(card['properties'])
    assert 'card_bbox' in card['required']
    assert 'def' in card['properties']


def test_incomplete_response_is_failure():
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: SimpleNamespace(stop_reason='max_tokens')))
    with pytest.raises(ValueError, match='未完了'):
        quality.structured_request(client, model='test', system='', content=[], schema={})


def _row():
    return {'name': '対象', 'card_type': '効果モンスター', 'level': 9, 'rank': None, 'link_val': None,
            'product_name': '商品', 'release_date': '2025-10-31', 'confidence': 'high',
            'extraction_raw': {'source_image_url': 'https://pbs.twimg.com/test.png', 'card_bbox': [0, 0, 100, 145]}}


def _setup(monkeypatch, *, verified=True, answer=None):
    import card_crop
    import release_date_resolver
    monkeypatch.setattr(release_date_resolver, 'resolve_release_date', lambda p: {
        'status': 'verified' if verified else 'unverified', 'release_date': '2026-10-31' if verified else None,
        'reason': '公式日付' if verified else '商品不明', 'source_url': 'https://www.yugioh-card.com/japan/products/test/'})
    monkeypatch.setattr(card_crop, 'refine_card_box', lambda data, box: {'left': 0, 'top': 0, 'right': 1, 'bottom': 1})
    monkeypatch.setattr(card_crop, 'make_level_zoom', lambda data, box: b'zoom')
    monkeypatch.setattr(quality, 'structured_request', lambda *a, **kw: ({'cards': answer if answer is not None else [
        {'id': 0, 'level': 8, 'rank': None, 'link_val': None, 'readable': True}]}, '{}', {'input_tokens': 1, 'output_tokens': 1}))
    return [{'url': 'https://pbs.twimg.com/test.png', 'media_type': 'image/png', 'data': base64.b64encode(b'original').decode()}]


def test_second_read_corrects_count_and_date_with_evidence(monkeypatch):
    images = _setup(monkeypatch)
    row = quality.enrich_rows([_row()], client=None, model='test', encoded_images=images)[0]
    assert row['level'] == 8
    assert row['release_date'] == '2026-10-31'
    assert row['extraction_raw']['release_date_evidence']['candidate_date'] == '2025-10-31'
    assert row['extraction_raw']['crop_suggestion']['source_image_url'] == images[0]['url']


def test_unknown_date_is_not_guessed(monkeypatch):
    images = _setup(monkeypatch, verified=False)
    row = quality.enrich_rows([_row()], client=None, model='test', encoded_images=images)[0]
    assert row['release_date'] is None
    assert row['extraction_raw']['release_date_evidence']['status'] == 'unverified'


def test_duplicate_review_ids_fail(monkeypatch):
    images = _setup(monkeypatch, answer=[{'id': 0}, {'id': 0}])
    with pytest.raises(ValueError, match='欠落または重複'):
        quality.enrich_rows([_row()], client=None, model='test', encoded_images=images)


def test_unreadable_review_keeps_unknown_and_warns(monkeypatch):
    images = _setup(monkeypatch, answer=[{'id': 0, 'level': None, 'rank': None, 'link_val': None, 'readable': False}])
    row = quality.enrich_rows([_row()], client=None, model='test', encoded_images=images)[0]
    assert row['level'] is None
    assert row['confidence'] == 'low'
    assert row['extraction_raw']['recognition_review']['status'] == 'needs_review'


def test_xyz_cannot_be_verified_with_level(monkeypatch):
    images = _setup(monkeypatch, answer=[{'id': 0, 'level': 7, 'rank': None, 'link_val': None, 'readable': True}])
    row = _row()
    row['card_type'] = 'エクシーズモンスター'
    row = quality.enrich_rows([row], client=None, model='test', encoded_images=images)[0]
    assert row['level'] is None
    assert row['confidence'] == 'low'
    assert row['extraction_raw']['recognition_review']['status'] == 'needs_review'


def test_missing_images_do_not_become_successful_empty_result(monkeypatch):
    monkeypatch.setattr(extractor, '_download_and_encode_images', lambda urls: [])
    with pytest.raises(ValueError, match='一部を取得できません'):
        extractor.extract_cards_from_tweet('本文', ['https://pbs.twimg.com/test.png'])


def test_invalid_image_reference_cannot_fall_back_to_first(monkeypatch):
    import anthropic
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'fake')
    monkeypatch.setattr(anthropic, 'Anthropic', lambda **kw: None)
    monkeypatch.setattr(quality, 'structured_request', lambda *a, **kw: ({'cards': [
        {'name': '対象', 'card_type': '効果モンスター', 'image_url': 'https://wrong/image.png'}]}, '{}', {}))
    with pytest.raises(ValueError, match='対応を確認できません'):
        extractor._extract_cards('本文', [{'url': 'https://pbs.twimg.com/test.png', 'data': 'abc', 'media_type': 'image/png'}], 'https://x.com/test', from_x=True)
