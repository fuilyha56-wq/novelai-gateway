"""原生流式透传（/ai/generate-image-stream）回归测试。

背景：性能优化重构后 `_proxy_native_nai` 直接返回 StreamingResponse，
但 app.py 漏 import，导致每次 LFN 网页流式生图 502（NameError）。
旧测试从未覆盖该分支，这里补上最小编排防止回归。
"""

import unittest
import json

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

    def test_medium_rejects_overrides_before_forwarding(self) -> None:
        """Native endpoints must reject High settings before contacting NovelAI."""
        settings.allow_unauthenticated_access = True
        settings.gateway_password = ""
        from unittest.mock import AsyncMock, patch

        for path in ("/ai/generate-image-stream", "/_api/ai/generate-image"):
            with self.subTest(path=path), patch("src.proxy.app.forward", new_callable=AsyncMock) as forwarded:
                with TestClient(app) as client:
                    response = client.post(path, json={
                        "model": "nai-diffusion-5-full-medium",
                        "parameters": {"width": 512, "height": 512, "steps": 28},
                    })
                self.assertEqual(response.status_code, 400)
                forwarded.assert_not_called()


        def test_native_multipart_request_part_is_read_as_json_file(self) -> None:
            """Aaalice Launcher sends the native request JSON as a multipart file part."""
            payload = {
                "input": "launcher multipart",
                "model": "nai-diffusion-5-full",
                "action": "generate",
                "parameters": {"width": 512, "height": 512, "steps": 28},
            }
            sent: dict[str, object] = {}

            async def fake_forward(request, target_url):
                sent["content_type"] = request.headers.get("content-type", "")
                sent["body"] = await request.body()
                return _zip_response()

            with patch("src.proxy.app.forward", fake_forward), TestClient(app) as client:
                response = client.post(
                    "/ai/generate-image",
                    files={"request": ("blob", json.dumps(payload), "application/json")},
                    headers={"Authorization": "Bearer test"},
                )

            self.assertEqual(response.status_code, 200)
            self.assertIn("multipart/form-data", str(sent["content_type"]))
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
        self.assertEqual(response.headers.get("x-anlas-cost"), "40")
        self.assertEqual(response.headers.get("x-prompt-tokens"), "25")
        self.assertTrue(sent["url"].endswith("/ai/generate-image-stream"))

    def test_malformed_multipart_request_is_transparent(self) -> None:
        """Malformed multipart JSON must reach the native upstream unchanged."""
        settings.allow_unauthenticated_access = True
        settings.gateway_password = ""
        settings.shared_api_keys = ""
        sent: dict[str, object] = {}

        async def fake_forward(request, target_url):
            sent["body"] = await request.body()
            sent["content_type"] = request.headers.get("content-type", "")
            return httpx.Response(
                200,
                content=b"upstream-response",
                headers={"content-type": "application/zip"},
            )

        from unittest.mock import patch

        with patch("src.proxy.app.forward", fake_forward), TestClient(app) as client:
            response = client.post(
                "/ai/generate-image",
                files={"request": ("blob", b"not-json", "application/json")},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"upstream-response")
        self.assertIn(b"not-json", sent["body"])
        self.assertIn("multipart/form-data", str(sent["content_type"]))

    def test_medium_fixed_parameters_are_forwarded_in_json_and_multipart(self) -> None:
        """Both native transports must forward normalized settings, not the original body."""
        from unittest.mock import patch
        from src.proxy.openai import MEDIUM_NEGATIVE_PROMPT

        settings.allow_unauthenticated_access = True
        settings.gateway_password = ""
        settings.shared_api_keys = ""
        payload = {"model": "nai-v5-full-medium", "parameters": {"width": 512, "height": 512}}
        sent = {}

        async def fake_forward(request, target_url):
            if "multipart/form-data" in request.headers.get("content-type", ""):
                from starlette.requests import Request
                body = await request.body()

                async def receive():
                    return {"type": "http.request", "body": body, "more_body": False}

                parsed = await Request(request.scope, receive).form()
                sent["payload"] = json.loads(str(parsed["request"]))
                sent["image"] = await parsed["image"].read()
            else:
                sent["payload"] = json.loads(await request.body())
            return httpx.Response(200, content=b"result", headers={"content-type": "application/octet-stream"})

        for multipart in (False, True):
            with self.subTest(multipart=multipart), patch("src.proxy.app.forward", fake_forward), TestClient(app) as client:
                response = client.post("/ai/generate-image", files={"request": (None, json.dumps(payload)), "image": ("image.png", b"image-data", "image/png")}) if multipart else client.post("/ai/generate-image", json=payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(sent["payload"]["model"], "nai-diffusion-5-full-medium")
            self.assertEqual(sent["payload"]["parameters"]["steps"], 14)
            self.assertEqual(sent["payload"]["parameters"]["negative_prompt"], MEDIUM_NEGATIVE_PROMPT)
            if multipart:
                self.assertEqual(sent["image"], b"image-data")
