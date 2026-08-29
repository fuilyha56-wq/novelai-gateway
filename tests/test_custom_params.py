"""自定义参数透传的单测：一等公民 V5 参数、extra_params 透传、透明通道保留。"""

import base64
import io
import json
import unittest
import zipfile

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from src.proxy import openai as openai_module
from src.proxy.app import app
from src.proxy.config import settings
from src.proxy.openai import (
    _absorb_extra_params,
    _apply_custom_params,
    _build_generation_payload,
    _extract_pngs_from_response,
    _flatten_to_rgb,
)


def _absorb_and_build(body: dict, header: str | None = None):
    """模拟 handler 真实流程：入口 absorb → 载荷构建。"""
    _absorb_extra_params(body, header)
    return _build_generation_payload(body)


def _rgba_png_bytes(min_alpha: int) -> bytes:
    """生成一张 RGBA PNG，其中一个像素的 alpha 为 min_alpha，其余为 255。"""
    img = Image.new("RGBA", (32, 32), (255, 0, 0, 255))
    pixels = img.load()
    pixels[0, 0] = (255, 0, 0, min_alpha)
    output = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


class FirstClassV5ParamsTests(unittest.TestCase):
    """一等公民参数：straight_alpha / transparent_background / tag_hint_*。"""

    def test_v5_defaults_straight_alpha_true(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {"model": "nai-diffusion-5-full", "prompt": "test"}
        )
        self.assertIs(nai_payload["parameters"]["straight_alpha"], True)

    def test_v4_family_does_not_inject_straight_alpha(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {"model": "nai-diffusion-4-5-full", "prompt": "test"}
        )
        self.assertNotIn("straight_alpha", nai_payload["parameters"])

    def test_straight_alpha_body_overrides_default(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {"model": "nai-diffusion-5-full", "prompt": "test", "straight_alpha": False}
        )
        self.assertIs(nai_payload["parameters"]["straight_alpha"], False)

    def test_transparent_background_maps_to_tag_hint(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {"model": "nai-diffusion-5-full", "prompt": "test", "transparent_background": True}
        )
        params = nai_payload["parameters"]
        self.assertIs(params["tag_hint_transparent_background"], True)
        self.assertNotIn("transparent_background", params)  # 不透传不存在的字段名

    def test_tag_hint_fields_passthrough(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "test",
                "tag_hint_qt": 1,
                "tag_hint_uc_preset": 2,
            }
        )
        params = nai_payload["parameters"]
        self.assertEqual(params["tag_hint_qt"], 1)
        self.assertEqual(params["tag_hint_uc_preset"], 2)

    def test_first_class_params_via_helper_only(self) -> None:
        """helper 独立调用（img2img 等端点的路径）同样生效。"""
        params: dict = {}
        _apply_custom_params(
            {"transparent_background": True, "tag_hint_qt": 0}, params, "nai-diffusion-5-full"
        )
        self.assertIs(params["tag_hint_transparent_background"], True)
        self.assertIs(params["straight_alpha"], True)  # V5 默认注入
        self.assertEqual(params["tag_hint_qt"], 0)


class ExtraParamsTests(unittest.TestCase):
    """extra_params / X-NovelAI-Extra-Params：全量透传 + 关键参数校验。"""

    def test_extra_params_merged_into_parameters(self) -> None:
        nai_payload, _, _ = _absorb_and_build(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "test",
                "extra_params": {"color_correct": True, "autoSmea": False},
            }
        )
        params = nai_payload["parameters"]
        self.assertIs(params["color_correct"], True)
        self.assertIs(params["autoSmea"], False)

    def test_extra_params_non_dict_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _absorb_and_build(
                {"model": "nai-diffusion-5-full", "prompt": "t", "extra_params": ["steps=50"]}
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_extra_params_reserved_keys_rejected(self) -> None:
        """协议字段（模型/响应形态/参考图协议）不允许经 extra_params 覆盖。"""
        for reserved in ("model", "stream", "image_format", "response_format", "prompt", "size"):
            with self.assertRaises(HTTPException) as ctx:
                _absorb_and_build(
                    {
                        "model": "nai-diffusion-5-full",
                        "prompt": "t",
                        "extra_params": {reserved: "x"},
                    }
                )
            self.assertEqual(ctx.exception.status_code, 400, reserved)

    def test_extra_params_critical_override_allowed_and_validated(self) -> None:
        """计费/限额关键参数允许覆盖，合法值生效，非法值 400。"""
        nai_payload, _, _ = _absorb_and_build(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "extra_params": {"steps": 50, "width": 832, "scale": 9},
            }
        )
        params = nai_payload["parameters"]
        self.assertEqual(params["steps"], 50)
        self.assertEqual(params["width"], 832)
        self.assertEqual(params["scale"], 9)

        for bad in ({"steps": 999}, {"width": 833}, {"scale": 12}, {"seed": -1}):
            with self.assertRaises(HTTPException) as ctx:
                _absorb_and_build(
                    {"model": "nai-diffusion-5-full", "prompt": "t", "extra_params": bad}
                )
            self.assertEqual(ctx.exception.status_code, 400, bad)

    def test_uc_preset_and_quality_toggle_overridable(self) -> None:
        """预设提示类字段允许通过 extra_params 覆盖。"""
        nai_payload, _, _ = _absorb_and_build(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "extra_params": {"ucPreset": 3, "qualityToggle": False},
            }
        )
        self.assertEqual(nai_payload["parameters"]["ucPreset"], 3)
        self.assertIs(nai_payload["parameters"]["qualityToggle"], False)

    def test_header_extra_params_merges_and_wins(self) -> None:
        header_json = json.dumps({"color_correct": True, "tag_hint_qt": 2})
        nai_payload, _, _ = _absorb_and_build(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "extra_params": {"color_correct": False},
            },
            header_json,
        )
        # header 优先于 body（与 X-Sampler 等既有约定一致）
        self.assertIs(nai_payload["parameters"]["color_correct"], True)
        self.assertEqual(nai_payload["parameters"]["tag_hint_qt"], 2)

    def test_header_extra_params_invalid_json_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _absorb_and_build(
                {"model": "nai-diffusion-5-full", "prompt": "t"},
                "{not json",
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_header_extra_params_non_object_rejected(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            _absorb_and_build(
                {"model": "nai-diffusion-5-full", "prompt": "t"},
                '["steps"]',
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_transparent_background_via_extra_params(self) -> None:
        nai_payload, _, _ = _absorb_and_build(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "extra_params": {"transparent_background": True},
            }
        )
        self.assertIs(nai_payload["parameters"]["tag_hint_transparent_background"], True)
        self.assertNotIn("transparent_background", nai_payload["parameters"])  # 别名不泄漏


class ModelGatingTests(unittest.TestCase):
    """模型感知门控：V5 专属参数仅 V5 放行；未知字段全量透传。"""

    def test_v5_only_params_pass_on_v5(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "straight_alpha": False,
                "tag_hint_qt": 3,
                "tag_hint_uc_preset": 2,
            }
        )
        params = nai_payload["parameters"]
        self.assertIs(params["straight_alpha"], False)
        self.assertEqual(params["tag_hint_qt"], 3)
        self.assertEqual(params["tag_hint_uc_preset"], 2)

    def test_v5_only_params_stripped_on_v4(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {
                "model": "nai-diffusion-4-5-full",
                "prompt": "t",
                "straight_alpha": True,
                "transparent_background": True,
                "tag_hint_qt": 1,
                "tag_hint_uc_preset": 2,
            }
        )
        params = nai_payload["parameters"]
        for key in ("straight_alpha", "tag_hint_transparent_background", "tag_hint_qt", "tag_hint_uc_preset"):
            self.assertNotIn(key, params, key)

    def test_unknown_body_fields_passthrough_without_wrapper(self) -> None:
        """未知字段直接写 body 顶层也会透传，无需 extra_params 包装。"""
        nai_payload, _, _ = _build_generation_payload(
            {
                "model": "nai-diffusion-4-5-full",
                "prompt": "t",
                "color_correct": True,
            }
        )
        self.assertIs(nai_payload["parameters"]["color_correct"], True)

    def test_reserved_body_keys_do_not_leak_into_params(self) -> None:
        nai_payload, _, _ = _build_generation_payload(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "size": "512x512",
                "user": "u-123",
                "quality_tags": "",
                "response_format": "b64_json",
                "service_tier": "priority",
            }
        )
        params = nai_payload["parameters"]
        for key in ("user", "size", "quality_tags", "response_format", "service_tier"):
            self.assertNotIn(key, params, key)

    def test_explicit_fields_not_duplicated_by_passthrough(self) -> None:
        """显式构建的字段不被透传覆盖（透传只补缺）。"""
        nai_payload, _, _ = _build_generation_payload(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "steps": 30,
                "negative_prompt": "custom uc",
            }
        )
        params = nai_payload["parameters"]
        self.assertEqual(params["steps"], 30)
        self.assertEqual(params["negative_prompt"], "custom uc")


class FlattenAlphaTests(unittest.TestCase):
    """_flatten_to_rgb：实质透明保留 RGBA，近不透明照旧压平。"""

    def test_transparent_image_preserved_byte_for_byte(self) -> None:
        raw = _rgba_png_bytes(min_alpha=0)
        self.assertEqual(_flatten_to_rgb(raw), raw)

    def test_opaque_rgba_still_flattened(self) -> None:
        raw = _rgba_png_bytes(min_alpha=255)
        flattened = _flatten_to_rgb(raw)
        self.assertNotEqual(flattened, raw)
        self.assertEqual(Image.open(io.BytesIO(flattened)).mode, "RGB")

    def test_near_opaque_rgba_still_flattened(self) -> None:
        """V4.5 的 alpha 254-255 属于噪声范围，照旧压平。"""
        raw = _rgba_png_bytes(min_alpha=254)
        self.assertEqual(Image.open(io.BytesIO(_flatten_to_rgb(raw))).mode, "RGB")

    def test_rgb_passthrough(self) -> None:
        output = io.BytesIO()
        Image.new("RGB", (32, 32), (0, 255, 0)).save(output, format="PNG")
        raw = output.getvalue()
        self.assertEqual(_flatten_to_rgb(raw), raw)

    def test_transparent_image_survives_response_extraction(self) -> None:
        """完整响应路径（zip 提取 + flatten）不丢透明通道。"""
        raw = _rgba_png_bytes(min_alpha=10)
        buf = io.BytesIO()
        import zipfile

        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("image_0.png", raw)
        pngs = _extract_pngs_from_response(buf.getvalue())
        self.assertEqual(len(pngs), 1)
        self.assertEqual(pngs[0], raw)

    def test_opaque_image_survives_response_extraction_flattened(self) -> None:
        import zipfile

        raw = _rgba_png_bytes(min_alpha=255)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("image_0.png", raw)
        pngs = _extract_pngs_from_response(buf.getvalue())
        self.assertEqual(Image.open(io.BytesIO(pngs[0])).mode, "RGB")


class B64RoundTripTests(unittest.TestCase):
    """透明图以 b64_json 返回时 alpha 仍在（模拟客户端视角）。"""

    def test_b64_roundtrip_keeps_alpha(self) -> None:
        raw = _rgba_png_bytes(min_alpha=0)
        png = _flatten_to_rgb(raw)
        img = Image.open(io.BytesIO(base64.b64decode(base64.b64encode(png))))
        self.assertEqual(img.mode, "RGBA")
        alpha_lo, _ = img.getchannel("A").getextrema()
        self.assertLess(alpha_lo, 250)


class HandlerIntegrationTests(unittest.TestCase):
    """端到端：stub 上游后打真实路由，验证各 handler 的参数合并运行时生效。"""

    def setUp(self) -> None:
        # 本地 .env 可能配置了共享凭据，测试需放行下游鉴权（沿用 test_gateway 模式）
        self._settings = {
            "gateway_password": settings.gateway_password,
            "allow_unauthenticated_access": settings.allow_unauthenticated_access,
        }
        settings.gateway_password = ""
        settings.allow_unauthenticated_access = True

        self.captured: list[dict] = []
        transparent_png = _rgba_png_bytes(min_alpha=0)

        def fake_zip(png: bytes) -> bytes:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("image_0.png", png)
            return buf.getvalue()

        original_send = openai_module._send_nai_request

        async def fake_send(request, payload, target_url=None, accept_format="zip"):
            self.captured.append(payload)
            return fake_zip(transparent_png)

        openai_module._send_nai_request = fake_send
        self._original_send = original_send
        self._client = TestClient(app)

    def tearDown(self) -> None:
        openai_module._send_nai_request = self._original_send
        for name, value in self._settings.items():
            setattr(settings, name, value)
        self._client.close()

    def _last_params(self) -> dict:
        return self.captured[-1]["parameters"]

    def test_generations_merges_custom_params(self) -> None:
        resp = self._client.post(
            "/v1/images/generations",
            json={
                "model": "nai-diffusion-5-full",
                "prompt": "1girl, transparent background",
                "transparent_background": True,
                "extra_params": {"color_correct": True},
            },
            headers={"X-NovelAI-Extra-Params": '{"tag_hint_qt": 1}'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        params = self._last_params()
        self.assertIs(params["tag_hint_transparent_background"], True)
        self.assertIs(params["color_correct"], True)
        self.assertEqual(params["tag_hint_qt"], 1)
        self.assertIs(params["straight_alpha"], True)
        # 透明 PNG 原样返回，b64 解码后 alpha 仍在
        img = Image.open(io.BytesIO(base64.b64decode(resp.json()["data"][0]["b64_json"])))
        self.assertEqual(img.mode, "RGBA")

    def test_generations_extra_params_reserved_key_rejected(self) -> None:
        resp = self._client.post(
            "/v1/images/generations",
            json={
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "extra_params": {"stream": "msgpack"},
            },
        )
        self.assertEqual(resp.status_code, 400)
        self.assertIn("stream", resp.json()["detail"])

    def test_generations_critical_override_applied(self) -> None:
        resp = self._client.post(
            "/v1/images/generations",
            json={
                "model": "nai-diffusion-5-full",
                "prompt": "t",
                "extra_params": {"steps": 50},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertEqual(self._last_params()["steps"], 50)

    def test_limit_model_cannot_bypass_free_tier_via_extra_params(self) -> None:
        """-limit 免费额度边界：extra_params 覆盖 steps 后仍被入口校验拦截。"""
        resp = self._client.post(
            "/v1/images/generations",
            json={
                "model": "nai-v5-full-limit",
                "prompt": "t",
                "extra_params": {"steps": 50},
            },
        )
        self.assertEqual(resp.status_code, 400, resp.text)

    def test_img2img_merges_custom_params(self) -> None:
        image_b64 = base64.b64encode(_rgba_png_bytes(min_alpha=0)).decode()
        resp = self._client.post(
            "/v1/images/img2img",
            json={
                "model": "nai-diffusion-5-full",
                "prompt": "same but transparent",
                "image": image_b64,
                "extra_params": {"color_correct": True},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        params = self._last_params()
        self.assertIs(params["color_correct"], True)
        self.assertIs(params["straight_alpha"], True)

    def test_inpainting_merges_custom_params(self) -> None:
        image_b64 = base64.b64encode(_rgba_png_bytes(min_alpha=0)).decode()
        resp = self._client.post(
            "/v1/images/inpainting",
            json={
                "model": "nai-diffusion-5-full",
                "prompt": "fill background",
                "image": image_b64,
                "mask": image_b64,
                "extra_params": {"color_correct": True},
            },
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(self._last_params()["color_correct"], True)

    def test_edits_merges_custom_params_via_header(self) -> None:
        image_b64 = base64.b64encode(_rgba_png_bytes(min_alpha=0)).decode()
        resp = self._client.post(
            "/v1/images/edits",
            json={
                "model": "nai-diffusion-5-full",
                "prompt": "remove background",
                "image": image_b64,
                "mask": image_b64,
            },
            headers={"X-NovelAI-Extra-Params": '{"color_correct": true}'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(self._last_params()["color_correct"], True)

    def test_edits_multipart_with_extra_params_header(self) -> None:
        """multipart 分支同样支持 extra_params（回归：body 变量未定义导致 500）。"""
        png = _rgba_png_bytes(min_alpha=0)
        resp = self._client.post(
            "/v1/images/edits",
            files={
                "image": ("image.png", png, "image/png"),
                "mask": ("mask.png", png, "image/png"),
            },
            data={"model": "nai-diffusion-5-full", "prompt": "remove background"},
            headers={"X-NovelAI-Extra-Params": '{"color_correct": true}'},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIs(self._last_params()["color_correct"], True)


if __name__ == "__main__":
    unittest.main()
