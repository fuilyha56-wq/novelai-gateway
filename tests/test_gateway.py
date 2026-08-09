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
    _calc_anlas_cost,
    _extract_pngs_from_response,
    _is_encoded_vibe,
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

    def test_cfg_rescale_passed_through_generation_payload(self) -> None:
        """回归测试：`-limit` 模型主生成路径必须把 cfg_rescale/noise 传到参数字典。"""
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {
                "model": "nai-v4.5-full-limit",
                "prompt": "test",
                "cfg_rescale": 0.4,
                "noise": 0.1,
            }
        )
        params = nai_payload["parameters"]
        self.assertAlmostEqual(params["cfg_rescale"], 0.4)
        self.assertAlmostEqual(params["noise"], 0.1)

    def test_cfg_rescale_defaults_to_zero_when_missing(self) -> None:
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {"model": "nai-v4.5-full", "prompt": "test"}
        )
        self.assertEqual(nai_payload["parameters"]["cfg_rescale"], 0.0)
        self.assertEqual(nai_payload["parameters"]["noise"], 0.0)

    def test_generation_payload_accepts_up_to_four_vibe_references(self) -> None:
        """通用生图入口应把 reference_images 映射为上游多图 Vibe 字段。"""
        reference_images = [f"image-{index}" for index in range(4)]
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {
                "model": "nai-v4.5-full",
                "prompt": "test",
                "reference_images": reference_images,
                "reference_strength_multiple": [0.2, 0.4, 0.6, 0.8],
                "reference_information_extracted_multiple": [0.1, 0.3, 0.5, 0.7],
            }
        )
        params = nai_payload["parameters"]
        self.assertEqual(params["reference_image_multiple"], reference_images)
        self.assertEqual(params["reference_strength_multiple"], [0.2, 0.4, 0.6, 0.8])
        self.assertEqual(
            params["reference_information_extracted_multiple"],
            [0.1, 0.3, 0.5, 0.7],
        )

    def test_generation_payload_rejects_more_than_four_vibe_references(self) -> None:
        """Vibe 参考图数量不能超过 4 张。"""
        with self.assertRaises(HTTPException) as raised:
            _build_generation_payload(
                {
                    "model": "nai-v4.5-full",
                    "prompt": "test",
                    "reference_images": [f"image-{index}" for index in range(5)],
                }
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("最多 4 张", str(raised.exception.detail))

    def test_precise_reference_cannot_combine_multiple_vibe_references(self) -> None:
        """Precise Reference 与 Vibe 多图必须互斥。"""
        with self.assertRaises(HTTPException) as raised:
            _build_generation_payload(
                {
                    "model": "nai-v4.5-full",
                    "prompt": "test",
                    "reference_images": ["vibe-image"],
                    "references": [
                        {
                            "reference_image": "precise-image",
                            "reference_type": "character",
                        }
                    ],
                },
                operation="precise-reference",
            )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("cannot be combined", str(raised.exception.detail))

    def test_calc_anlas_cost_vibe_reuse_reduces_encoding_fee(self) -> None:
        """复用已编码氛围不应再收 2 Anlas 编码费。"""
        # 512x512, 28 steps, 单张, 无参考图: 5 Anlas
        base = _calc_anlas_cost(width=512, height=512, steps=28, n_samples=1)
        self.assertEqual(base, 5)

        # 单张全新 Vibe 参考图: 5 + 2 = 7
        single_new = _calc_anlas_cost(
            width=512, height=512, steps=28, n_samples=1,
            reference_image_count=1, reference_mode="vibe",
        )
        self.assertEqual(single_new, 7)

        # 单张已编码复用 Vibe 参考图: 5 + 0 = 5 (不收编码费)
        single_reuse = _calc_anlas_cost(
            width=512, height=512, steps=28, n_samples=1,
            reference_image_count=1, reference_mode="vibe",
            reference_image_encoded_count=1,
        )
        self.assertEqual(single_reuse, 5)

        # 3 张全新 + 2 张复用 = 5 张参考图, 但只对 3 张收编码费: 5 + 2*3 = 11
        mixed = _calc_anlas_cost(
            width=512, height=512, steps=28, n_samples=1,
            reference_image_count=5, reference_mode="vibe",
            reference_image_encoded_count=2,
        )
        self.assertEqual(mixed, 5 + 2 * 3)

        # 全部 5 张复用: 编码费为 0
        all_reuse = _calc_anlas_cost(
            width=512, height=512, steps=28, n_samples=1,
            reference_image_count=5, reference_mode="vibe",
            reference_image_encoded_count=5,
        )
        self.assertEqual(all_reuse, 5)

    def test_calc_anlas_cost_precise_mode_ignores_encoded_count(self) -> None:
        """Precise Reference 模式下 reference_image_encoded_count 应被忽略。"""
        precise = _calc_anlas_cost(
            width=512, height=512, steps=28, n_samples=1,
            reference_image_count=1, reference_mode="precise",
            reference_image_encoded_count=1,
        )
        # 5 (base) + 5 * 1 * 1 (precise) = 10
        self.assertEqual(precise, 10)

    def test_is_encoded_vibe_detects_binary_not_image(self) -> None:
        """已编码氛围 (NAI 私有二进制) 应被识别为 True，PNG/JPEG 为 False。"""
        # NAI 私有二进制 (非图片文件头)
        fake_vibe = base64.b64encode(b"\x00\x01\x02\x03" + b"\x42" * 2048).decode()
        self.assertTrue(_is_encoded_vibe(fake_vibe))

        # PNG 图片应为 False
        png_b64 = base64.b64encode(_png_bytes("red")).decode()
        self.assertFalse(_is_encoded_vibe(png_b64))

        # JPEG 文件头应为 False
        jpeg_b64 = base64.b64encode(b"\xff\xd8\xff\xe0" + b"\x00" * 2048).decode()
        self.assertFalse(_is_encoded_vibe(jpeg_b64))

        # 过短字符串应为 False
        self.assertFalse(_is_encoded_vibe("short"))
        self.assertFalse(_is_encoded_vibe(""))

    def test_build_image_response_v2_includes_vibe_field(self) -> None:
        """带 encoded_vibes 时响应 data[i] 应包含 vibe 字段。"""
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("0.png", _png_bytes("red"))
        request = Request({
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("testserver", 80),
            "path": "/v1/images/generations",
            "headers": [],
        })
        fake_vibe = base64.b64encode(b"\x00\x01\x02\x03" + b"\x42" * 2048).decode()
        response = _build_image_response_v2(
            request,
            archive.getvalue(),
            "test prompt",
            "b64_json",
            anlas_cost=7,
            encoded_vibes=[fake_vibe],
            reference_strengths=[0.6],
            reference_information_extracted=[1.0],
        )
        body = json.loads(response.body)
        self.assertEqual(len(body["data"]), 1)
        item = body["data"][0]
        self.assertIn("vibe", item)
        self.assertEqual(len(item["vibe"]), 1)
        self.assertEqual(item["vibe"][0]["reference_image"], fake_vibe)
        self.assertAlmostEqual(item["vibe"][0]["reference_strength"], 0.6)
        self.assertAlmostEqual(item["vibe"][0]["reference_information_extracted"], 1.0)

    def test_build_image_response_v2_omits_vibe_field_when_none(self) -> None:
        """无 encoded_vibes 时响应不应包含 vibe 字段。"""
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("0.png", _png_bytes("red"))
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
            anlas_cost=5,
        )
        body = json.loads(response.body)
        self.assertEqual(len(body["data"]), 1)
        self.assertNotIn("vibe", body["data"][0])


if __name__ == "__main__":
    unittest.main()
