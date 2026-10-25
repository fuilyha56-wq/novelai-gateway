import base64
import io
import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Response
from PIL import Image
from starlette.requests import Request

from src.proxy.openai import (
    _build_upscale_payload,
    handle_openai_generations,
)


def png(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(output, format="PNG")
    return output.getvalue()


def request(body: dict) -> Request:
    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    return Request({"type": "http", "method": "POST", "path": "/v1/images/generations", "headers": []}, receive)


class UpscaleBillingTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_images_route_returns_pixel_tier_usage_for_newapi(self):
        output = png(128, 128)
        for width, height, anlas in [(832, 1216, 1), (1024, 1536, 2), (1472, 1472, 3), (1536, 2048, 4)]:
            with self.subTest(width=width, height=height):
                async def handler(incoming):
                    payload = _build_upscale_payload(await incoming.json())
                    self.assertEqual(payload["model"], "nai-diffusion-5-full")
                    return Response(output, media_type="image/png")
                body = {"model": "nai-v5-full", "image": base64.b64encode(png(width, height)).decode(), "novelai_operation": "upscale"}
                with patch("src.proxy.openai.handle_upscale", side_effect=handler) as upstream:
                    response = await handle_openai_generations(request(body))
                result = json.loads(response.body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(result["usage"]["prompt_tokens"], anlas * 50)
                self.assertEqual(result["usage"]["completion_tokens"], 0)
                self.assertEqual(response.headers["x-anlas-cost"], str(anlas))
                self.assertEqual(base64.b64decode(result["data"][0]["b64_json"]), output)
                upstream.assert_awaited_once()

    async def test_invalid_source_is_rejected_before_calling_upstream(self):
        with patch("src.proxy.openai.handle_upscale") as upstream:
            with self.assertRaises(HTTPException) as error:
                await handle_openai_generations(request({"model": "nai-v5-full", "image": "broken", "novelai_operation": "upscale"}))
            self.assertEqual(error.exception.status_code, 400)
            upstream.assert_not_called()

    def test_aliases_resolve_to_the_corresponding_native_upscale_model(self):
        for variant in ("full", "curated"):
            with self.subTest(variant=variant):
                self.assertEqual(_build_upscale_payload({"model": "nai-v5-" + variant, "image": "image"})["model"], "nai-diffusion-5-" + variant)

    def test_limit_and_other_model_families_cannot_get_paid_upscale(self):
        for model in ("nai-v5-full-limit", "nai-v5-full-medium", "nai-v4.5-full"):
            with self.subTest(model=model):
                with self.assertRaises(HTTPException):
                    _build_upscale_payload({"model": model, "image": "image"})

    async def test_annotate_remains_blocked_without_a_billing_mapping(self):
        with self.assertRaises(HTTPException) as error:
            await handle_openai_generations(request({"model": "nai-v5-full", "novelai_operation": "annotate"}))
        self.assertEqual(error.exception.status_code, 400)
