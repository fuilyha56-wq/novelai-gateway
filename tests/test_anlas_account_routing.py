"""Account permissions must match the request that actually reaches NovelAI."""

import asyncio
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from src.proxy import app as app_module, openai, queue
from src.proxy.account_pool import account_pool
from src.proxy.config import get_request_auth_token, settings

_check_v5_account_quota = openai._check_v5_account_quota


@pytest.fixture
def routing(monkeypatch):
    original = account_pool._source
    accounts = [
        {"id": "free", "key": "free-key", "allow_v5": True, "allow_anlas": False},
        {"id": "paid", "key": "paid-key", "allow_v5": True, "allow_anlas": True},
    ]
    account_pool.configure(json.dumps(accounts))
    monkeypatch.setattr(account_pool, "_persistence_path", None)
    monkeypatch.setattr(settings, "shared_api_keys", json.dumps(accounts))
    monkeypatch.setattr(settings, "gateway_password", "test-gateway")
    monkeypatch.setattr(settings, "cloudflare_tunnel_token", "")
    monkeypatch.setattr(settings, "v5_quota_enabled", False)
    gate = queue.ConcurrencyGate(1, timeout=1, cooldown_min=0, cooldown_max=0)
    for module in (queue, app_module, openai):
        monkeypatch.setattr(module, "gate", gate)
    monkeypatch.setattr(openai, "_check_v5_account_quota", AsyncMock())
    monkeypatch.setattr(openai, "record_generation", lambda *a, **kw: None)
    monkeypatch.setattr(app_module, "record_generation", lambda *a, **kw: None)
    monkeypatch.setattr(openai, "log_generation", lambda *a, **kw: None)
    seen = []

    async def forward(request, target):
        seen.append((request.state.gateway_account_id, get_request_auth_token(request)))
        return httpx.Response(200, content=b"result", headers={"content-type": "application/zip"})

    monkeypatch.setattr(app_module, "forward", forward)
    yield seen
    account_pool.configure(original)


def _payload(**overrides):
    return {
        "model": "nai-diffusion-5-full", "action": "generate",
        "parameters": {"width": 832, "height": 1216, "steps": 28, "n_samples": 1},
        **overrides,
    }


@pytest.mark.parametrize("path", ["/ai/generate-image", "/ai/generate-image-stream", "/_api/ai/generate-image"])
@pytest.mark.parametrize("multipart", [False, True])
@pytest.mark.parametrize("priority", [False, True])
def test_native_priority_is_paid_and_normal_generation_remains_free(routing, path, multipart, priority):
    payload = _payload(**({"service_tier": "priority"} if priority else {}))
    kwargs = {"files": {"request": ("blob", json.dumps(payload), "application/json")}} if multipart else {"json": payload}
    with TestClient(app_module.app) as client:
        response = client.post(path, headers={"Authorization": "Bearer test-gateway"}, **kwargs)
    assert response.status_code == 200, response.text
    assert routing == [("paid", "paid-key") if priority else ("free", "free-key")]


@pytest.mark.parametrize("path", ["/ai/encode-vibe", "/ai/augment-image", "/_api/ai/encode-vibe"])
def test_native_paid_tools_never_use_free_account(routing, path):
    with TestClient(app_module.app) as client:
        response = client.post(path, json=_payload(), headers={"Authorization": "Bearer test-gateway"})
    assert response.status_code == 200, response.text
    assert routing == [("paid", "paid-key")]


def test_no_paid_account_rejects_priority_before_forwarding(routing):
    account_pool.set_enabled("paid", False)
    with TestClient(app_module.app) as client:
        response = client.post("/ai/generate-image", json=_payload(service_tier="priority"), headers={"Authorization": "Bearer test-gateway"})
    assert response.status_code == 409
    assert routing == []


def test_paid_subrequest_cannot_reuse_cached_free_account(routing):
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    assert get_request_auth_token(request) == "free-key"
    with pytest.raises(HTTPException) as error:
        get_request_auth_token(request, need_anlas=True)
    assert error.value.status_code == 409


def test_revoking_anlas_permission_invalidates_cached_paid_selection(routing):
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    assert get_request_auth_token(request, need_anlas=True) == "paid-key"
    account_pool.set_flag("paid", "allow_anlas", False)
    with pytest.raises(HTTPException) as error:
        get_request_auth_token(request)
    assert error.value.status_code == 409


def test_final_send_rechecks_paid_parameters_before_any_upstream_call(routing, monkeypatch):
    from src.proxy import forwarder

    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    assert get_request_auth_token(request) == "free-key"
    get_client = AsyncMock()
    monkeypatch.setattr(forwarder, "get_client", get_client)
    payload = _payload()
    payload["parameters"]["steps"] = 50
    with pytest.raises(HTTPException) as error:
        asyncio.run(openai._send_nai_request(request, payload))
    assert error.value.status_code == 409
    get_client.assert_not_awaited()


def test_free_v5_request_requires_opus_entitlement(routing):
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    get_request_auth_token(request, need_v5=True)
    client = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(
        200, json={"tier": 2}, request=httpx.Request("GET", "https://upstream.test/user/subscription"),
    )))
    with pytest.raises(HTTPException) as error:
        asyncio.run(_check_v5_account_quota(request, "nai-diffusion-5-full", client))
    assert error.value.status_code == 409


@pytest.mark.parametrize("reuse", [False, True])
def test_character_reference_is_classified_before_paid_encoding(routing, monkeypatch, reuse):
    output = io.BytesIO()
    Image.new("RGB", (64, 64)).save(output, format="PNG")
    png = output.getvalue()
    encoded = base64.b64encode(b"encoded-vibe" * 100).decode()

    async def encode(request, *args, **kwargs):
        routing.append((request.state.gateway_account_id, get_request_auth_token(request)))
        return [encoded], 0

    async def generate(request, *args, **kwargs):
        return png

    monkeypatch.setattr(openai, "_encode_vibe_batch", encode)
    monkeypatch.setattr(openai, "_send_nai_request", generate)
    with TestClient(app_module.app) as client:
        response = client.post("/v1/images/character-reference", headers={"Authorization": "Bearer test-gateway"}, json={
            "model": "nai-v5-full", "prompt": "test", "response_format": "b64_json",
            "characters": [{"reference_image": encoded if reuse else base64.b64encode(png).decode(), "prompt": "test"}],
        })
    assert response.status_code == 200, response.text
    assert routing == [("free", "free-key") if reuse else ("paid", "paid-key")]


@pytest.mark.parametrize("parameters,needs_anlas", [
    ({}, False),
    ({"width": 1536, "height": 1536}, True),
    ({"steps": 50}, True),
    ({"n_samples": 2}, True),
    ({"service_tier": "priority"}, True),
    ({"director_reference_images_cached": [{"cache_secret_key": "ref"}]}, True),
    ({"reference_image_multiple_cached": [{"cache_secret_key": "ref"}]}, False),
    ({"reference_image_multiple_cached": [{"data": "raw-image"}]}, True),
    ({"reference_image_multiple_cached": [{"cache_secret_key": "ref"}] * 5}, True),
    ({"reference_images": [], "reference_image_multiple": ["raw-image"]}, True),
])
def test_final_payload_classifies_paid_overrides(parameters, needs_anlas):
    payload = _payload()
    payload["parameters"].update(parameters)
    assert openai._requires_anlas_for_account(payload["model"], payload) is needs_anlas
