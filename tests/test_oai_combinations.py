"""Feature combinations use one generation, one account and the final payload's price."""
import base64
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from src.proxy import app as app_module, openai
from src.proxy.config import settings


def png():
    stream = io.BytesIO()
    Image.new('RGBA', (64, 64), (255, 0, 0, 0)).save(stream, format='PNG')
    return stream.getvalue()


IMAGE = base64.b64encode(png()).decode()
VIBE = base64.b64encode(bytes(2000)).decode()


class Gate:
    def __init__(self):
        self.active = 0

    def for_request(self, request):
        return self

    async def __aenter__(self):
        self.active += 1

    async def __aexit__(self, *args):
        self.active -= 1


@pytest.fixture
def adapter(monkeypatch):
    captured = []
    gate = Gate()
    monkeypatch.setattr(settings, 'gateway_password', '')
    monkeypatch.setattr(settings, 'allow_unauthenticated_access', True)
    monkeypatch.setattr(openai, 'gate', gate)
    monkeypatch.setattr(openai, 'get_registry', lambda: None)
    monkeypatch.setattr(openai, '_preflight_account', lambda *args, **kwargs: None)
    monkeypatch.setattr(openai, 'check_v5_quota', lambda *args: None)
    monkeypatch.setattr(openai, 'record_generation', Mock())
    monkeypatch.setattr(openai, 'log_generation', Mock())
    encoder = AsyncMock(return_value=([VIBE], 1))
    monkeypatch.setattr(openai, '_encode_vibe_batch', encoder)

    async def send(request, payload, **kwargs):
        assert gate.active == 1
        captured.append(payload)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('image_0.png', png())
        return stream.getvalue()

    monkeypatch.setattr(openai, '_send_nai_request', send)
    with TestClient(app_module.app) as client:
        yield SimpleNamespace(client=client, captured=captured, gate=gate, encoder=encoder)
    assert gate.active == 0


@pytest.mark.parametrize('route,action', [
    ('generations', 'img2img'), ('img2img', 'img2img'),
    ('inpainting', 'infill'), ('edits', 'infill'), ('precise-reference', 'img2img'),
])
def test_precise_reference_and_image_share_one_generation(adapter, route, action):
    body = {'model': 'nai-diffusion-5-full', 'prompt': 'a hero', 'image': IMAGE,
            'references': [{'reference_image': IMAGE, 'reference_type': 'character', 'strength': 0.8, 'fidelity': 0.7}],
            'width': 2048, 'height': 1536, 'strength': 0.5, 'n': 2}
    if action == 'infill':
        body['mask'] = IMAGE
    response = adapter.client.post('/v1/images/' + route, json=body)
    assert response.status_code == 200, response.text
    assert len(adapter.captured) == 1
    payload = adapter.captured[0]
    assert payload['action'] == action
    params = payload['parameters']
    assert params['image'] == IMAGE
    assert len(params['director_reference_images']) == 1
    assert params['director_reference_information_extracted'] == [1.0]
    assert params['director_reference_secondary_strength_values'][0] == pytest.approx(0.3)
    assert len(params['v4_negative_prompt']['caption']['char_captions']) == len(params['v4_prompt']['caption']['char_captions'])
    expected = openai._calc_anlas_cost(2048, 1536, 28, n_samples=2, strength=0.5 if action == 'img2img' else 1, precise_reference_image_count=1, price_multiplier=1.5)
    assert int(response.headers['X-Anlas-Cost']) == expected
    assert response.json()['usage']['prompt_tokens'] == expected * 50


def test_multicharacter_vibe_precise_and_controlnet_compose(adapter):
    body = {'model': 'nai-diffusion-5-full', 'prompt': 'two heroes', 'image': IMAGE,
            'reference_images': [VIBE], 'references': [{'reference_image': IMAGE}],
            'characters': [{'prompt': 'red hair', 'negative_prompt': 'hat', 'center': {'x': 0.2, 'y': 0.5}}, {'prompt': 'blue hair'}],
            'controlnet_condition': IMAGE, 'controlnet_model': 'hed'}
    response = adapter.client.post('/v1/images/generations', json=body)
    assert response.status_code == 200, response.text
    params = adapter.captured[0]['parameters']
    assert params['image'] == IMAGE and params['controlnet_condition'] == IMAGE
    assert params['reference_image_multiple'] == [VIBE]
    assert len(params['director_reference_images']) == 1
    assert [c['char_caption'] for c in params['v4_prompt']['caption']['char_captions']] == ['red hair', 'blue hair']
    assert params['v4_negative_prompt']['caption']['char_captions'][0]['char_caption'] == 'hat'


def test_native_parameters_and_extra_params_carry_images_without_protocol_bypass(adapter):
    for source in ('parameters', 'extra_params'):
        response = adapter.client.post('/v1/images/generations', json={
            'model': 'nai-diffusion-5-full', 'prompt': 'hero',
            source: {'image': IMAGE, 'references': [{'reference_image': IMAGE}], 'steps': 40},
        })
        assert response.status_code == 200, response.text
        assert adapter.captured[-1]['action'] == 'img2img'
        assert adapter.captured[-1]['parameters']['steps'] == 40
        assert 'parameters' not in adapter.captured[-1]['parameters']
    for source in ('parameters', 'extra_params'):
        response = adapter.client.post('/v1/images/generations', json={'model': 'nai-v5-full-limit', source: {'image': IMAGE, 'steps': 40}})
        assert response.status_code == 400


def test_multipart_edits_keeps_json_references_and_characters(adapter):
    response = adapter.client.post('/v1/images/edits', files={'image': ('image.png', png(), 'image/png'), 'mask': ('mask.png', png(), 'image/png')}, data={
        'model': 'nai-diffusion-5-full', 'prompt': 'hero', 'references': json.dumps([{'reference_image': IMAGE}]),
        'characters': json.dumps([{'prompt': 'red hair'}]), 'qualityToggle': 'false', 'n': '2',
    })
    assert response.status_code == 200, response.text
    params = adapter.captured[0]['parameters']
    assert params['n_samples'] == 2
    assert params['qualityToggle'] is False
    assert len(params['director_reference_images']) == 1
    assert len(params['v4_prompt']['caption']['char_captions']) == 1


@pytest.mark.parametrize('ending', ['final', 'error', 'truncated'])
def test_stream_reserves_account_until_final_or_failure(adapter, monkeypatch, ending):
    closed = []

    class Stream:
        async def aiter_lines(self):
            assert adapter.gate.active == 1
            yield 'event: intermediate'
            yield 'data: ' + json.dumps({'image': IMAGE, 'samp_ix': 0})
            yield ''
            assert adapter.gate.active == 1
            if ending != 'truncated':
                yield 'event: ' + ending
                yield 'data: ' + json.dumps({'image': IMAGE} if ending == 'final' else {'message': 'upstream failed'})
                yield ''

        async def aclose(self):
            closed.append(True)

    async def send(request, payload):
        assert adapter.gate.active == 1
        assert payload['action'] == 'img2img'
        assert payload['parameters']['image'] == IMAGE
        assert len(payload['parameters']['director_reference_images']) == 1
        return Stream()

    monkeypatch.setattr(openai, '_send_nai_image_stream_request', send)
    response = adapter.client.post('/v1/images/generations', json={
        'model': 'nai-diffusion-5-full', 'image': IMAGE, 'references': [{'reference_image': IMAGE}], 'stream': True,
    })
    assert response.status_code == 200, response.text
    assert closed == [True]
    assert adapter.gate.active == 0
    assert ('event: image_generation.completed' in response.text) is (ending == 'final')
    assert ('event: error' in response.text) is (ending != 'final')
    if ending == 'final':
        assert '"usage"' in response.text
        openai.log_generation.assert_called_once()


def test_chat_preserves_native_sampling_parameters(adapter, monkeypatch):
    seen = []

    async def text(request, payload, stream=False):
        seen.append(payload)
        return b'{"output":"hello"}'

    monkeypatch.setattr(openai, '_send_nai_text_request', text)
    monkeypatch.setattr(openai, 'get_registry', lambda: SimpleNamespace(is_enabled=lambda kind: True, lookup=lambda model: SimpleNamespace(type='chat', name='llama-3-erato-v1')))
    response = adapter.client.post('/v1/chat/completions', json={
        'model': 'nai-chat-erato', 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 300,
        'parameters': {'repetition_penalty': 1.2, 'stop_sequences': [[1, 2]], 'order': [0, 1]}, 'min_p': 0.1,
    })
    assert response.status_code == 200, response.text
    assert seen[0]['parameters']['repetition_penalty'] == 1.2
    assert seen[0]['parameters']['stop_sequences'] == [[1, 2]]
    assert seen[0]['parameters']['min_p'] == 0.1
    assert seen[0]['parameters']['max_length'] == 300


def test_character_prompts_negative_caption_and_webp_are_preserved(adapter, monkeypatch):
    webp = io.BytesIO()
    Image.new('RGBA', (64, 64), (255, 0, 0, 100)).save(webp, format='WEBP')

    async def send(request, payload, **kwargs):
        assert payload['parameters']['image_format'] == 'webp'
        assert payload['parameters']['v4_negative_prompt']['caption']['char_captions'][0]['char_caption'] == 'hat'
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('image_0.webp', webp.getvalue())
        return archive.getvalue()

    monkeypatch.setattr(openai, '_send_nai_request', send)
    response = adapter.client.post('/v1/images/generations', json={
        'model': 'nai-diffusion-5-full', 'image_format': 'webp',
        'characterPrompts': [{'prompt': 'red hair', 'negativePrompt': 'hat'}],
    })
    assert response.status_code == 200, response.text
    assert base64.b64decode(response.json()['data'][0]['b64_json']) == webp.getvalue()


def test_stream_opening_failure_releases_account(adapter, monkeypatch):
    from fastapi import HTTPException

    async def send(request, payload):
        raise HTTPException(status_code=502, detail='offline')

    monkeypatch.setattr(openai, '_send_nai_image_stream_request', send)
    response = adapter.client.post('/v1/images/generations', json={'model': 'nai-diffusion-5-full', 'stream': True})
    assert response.status_code == 502
    assert adapter.gate.active == 0
