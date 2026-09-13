"""原生流式透传（/ai/generate-image-stream）回归测试。

背景：性能优化重构后 `_proxy_native_nai` 直接返回 StreamingResponse，
但 app.py 漏 import，导致每次 LFN 网页流式生图 502（NameError）。
旧测试从未覆盖该分支，这里补上最小编排防止回归。
"""

import unittest

import httpx
from fastapi.testclient import TestClient

from src.proxy import queue as queue_module
from src.proxy.app import app
from src.proxy.config import settings


class _NullGate:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeUpstreamStream(httpx.AsyncByteStream):
    """模拟上游 msgpack 流：is_stream_consumed 初始为 False。"""

    async def __aiter__(self):
        yield b"\x81\xa1image"
        yield b"\xc4\x04data"


class NativeStreamPassthroughTests(unittest.TestCase):
    def setUp(self) -> None:
        self._settings = {
            "shared_api_key": settings.shared_api_key,
            "shared_api_keys": settings.shared_api_keys,
            "shared_token": settings.shared_token,
            "gateway_password": settings.gateway_password,
            "allow_unauthenticated_access": settings.allow_unauthenticated_access,
        }
        self._original_gate = queue_module.gate
        queue_module.gate = _NullGate()

    def tearDown(self) -> None:
        queue_module.gate = self._original_gate
        for name, value in self._settings.items():
            setattr(settings, name, value)

    def test_generate_image_stream_passthrough_streams_body(self) -> None:
        settings.allow_unauthenticated_access = True
        settings.gateway_password = ""
        settings.shared_api_keys = ""
        settings.shared_api_key = "upstream-secret"

        payload = b"\x81\xa1image\xc4\x04data"
        upstream = httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            stream=_FakeUpstreamStream(),
        )
        self.assertFalse(upstream.is_stream_consumed)
        sent: dict[str, str] = {}

        async def fake_forward(request, target_url):
            sent["url"] = target_url
            return upstream

        from src.proxy import app as app_module

        original_forward = app_module.forward
        app_module.forward = fake_forward
        try:
            with TestClient(app) as client:
                response = client.post(
                    "/ai/generate-image-stream",
                    json={"input": "cat", "model": "nai-diffusion-5-full"},
                )
        finally:
            app_module.forward = original_forward

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload)
        self.assertTrue(sent["url"].endswith("/ai/generate-image-stream"))
