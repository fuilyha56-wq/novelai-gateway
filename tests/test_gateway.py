import base64
import io
import json
import unittest
import zipfile

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from starlette.requests import Request

from src.proxy.app import app
from src.proxy.config import settings
from src.proxy.openai import (
    _build_generation_payload,
    _build_image_response_v2,
    _extract_pngs_from_response,
)


def _png_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


class GatewayRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._settings = {
            "shared_api_key": settings.shared_api_key,
            "shared_api_keys": settings.shared_api_keys,
            "shared_token": settings.shared_token,
            "gateway_password": settings.gateway_password,
            "allow_unauthenticated_access": settings.allow_unauthenticated_access,
        }

    def tearDown(self) -> None:
        for name, value in self._settings.items():
            setattr(settings, name, value)

    def test_shared_credentials_require_gateway_authentication(self) -> None:
        settings.shared_api_key = "upstream-secret"
        settings.shared_api_keys = ""
        settings.shared_token = ""
        settings.gateway_password = "downstream-secret"
        settings.allow_unauthenticated_access = False

        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/models").status_code, 401)
            self.assertEqual(
                client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer wrong-secret"},
                ).status_code,
                401,
            )
            response = client.get(
                "/v1/models",
                headers={"Authorization": "Bearer downstream-secret"},
            )
            self.assertEqual(response.status_code, 200)

    def test_shared_credentials_fail_closed_without_password(self) -> None:
        settings.shared_api_key = "upstream-secret"
        settings.shared_api_keys = ""
        settings.shared_token = ""
        settings.gateway_password = ""
        settings.allow_unauthenticated_access = False

        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/models").status_code, 503)

    def test_cors_preflight_does_not_require_credentials(self) -> None:
        settings.shared_api_key = "upstream-secret"
        settings.shared_api_keys = ""
        settings.shared_token = ""
        settings.gateway_password = "downstream-secret"
        settings.allow_unauthenticated_access = False

        with TestClient(app) as client:
            response = client.options("/_api/ai/generate-image")
            self.assertEqual(response.status_code, 204)

    def test_extracts_every_png_from_zip(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("0.png", _png_bytes("red"))
            bundle.writestr("1.png", _png_bytes("blue"))

        images = _extract_pngs_from_response(archive.getvalue())
        self.assertEqual(len(images), 2)

    def test_openai_response_contains_every_generated_image(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("0.png", _png_bytes("red"))
            bundle.writestr("1.png", _png_bytes("blue"))
        request = Request({
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/v1/images/generations",
            "headers": [],
        })

        response = _build_image_response_v2(
            request,
            archive.getvalue(),
            "test prompt",
            "b64_json",
            anlas_cost=10,
        )
        body = json.loads(response.body)
        self.assertEqual(len(body["data"]), 2)
        for item in body["data"]:
            self.assertTrue(base64.b64decode(item["b64_json"]).startswith(b"\x89PNG"))

    def test_openai_n_cannot_bypass_sample_limit(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _build_generation_payload({
                "model": "nai-diffusion-4-5-full",
                "prompt": "test",
                "n": 7,
            })
        self.assertEqual(raised.exception.status_code, 400)

    def test_settings_routing_helpers_are_available(self) -> None:
        self.assertTrue(settings.is_heavy("/ai/generate-image"))
        self.assertEqual(
            settings.get_upstream_url("/ai/generate-image"),
            "https://image.novelai.net/ai/generate-image",
        )


if __name__ == "__main__":
    unittest.main()
