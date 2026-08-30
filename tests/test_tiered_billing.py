"""两档计费单测：Opus 免费额度档位判定、档内固定价、usage 注入。

-limit 模型的固定价并入完整版模型的两档计费：
- 档内（Opus 免费额度边界内）→ usage = 档内固定价（V4.5=0 / V5=8），
  NewAPI 侧 tier("limit", ...) 分支 1:1 落账；
- 档外（超边界或付费操作）→ usage = None，沿用 Anlas 动态换算旧口径，
  NewAPI 按 tier("full", p * 130000 / p * 100000) 动态计费。
"""

import base64
import io
import json
import unittest
import zipfile

from starlette.requests import Request

from src.proxy.openai import (
    _billing_prompt_tokens,
    _build_image_response_v2,
    _build_png_image_response,
    _in_opus_free_envelope,
    _tiered_billing_units,
)


def _png_bytes(color: str = "red") -> bytes:
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


def _zip_bytes() -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("0.png", _png_bytes("red"))
    return archive.getvalue()


def _request_stub() -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "server": ("testserver", 80),
        "path": "/v1/images/generations",
        "headers": [],
    })


def _encoded_vibe_b64() -> str:
    # 2000 字节的零数据：足够长（>1024）且不含图片文件头，命中已编码 vibe 启发式
    return base64.b64encode(b"\x00" * 2000).decode("ascii")


def _raw_png_b64() -> str:
    return base64.b64encode(_png_bytes("blue")).decode("ascii")


class TieredBillingUnitsTests(unittest.TestCase):
    """模型 → 档内固定价，接受上游名与网关名。"""

    def test_v5_family(self) -> None:
        self.assertEqual(_tiered_billing_units("nai-diffusion-5-full"), 8)
        self.assertEqual(_tiered_billing_units("nai-diffusion-5-full-inpainting"), 8)
        self.assertEqual(_tiered_billing_units("nai-v5-full"), 8)
        self.assertEqual(_tiered_billing_units("nai-v5-inpaint"), 8)

    def test_v45_family(self) -> None:
        self.assertEqual(_tiered_billing_units("nai-diffusion-4-5-full"), 0)
        self.assertEqual(_tiered_billing_units("nai-diffusion-4-5-full-inpainting"), 0)
        self.assertEqual(_tiered_billing_units("nai-v4.5-curated"), 0)

    def test_other_models_excluded(self) -> None:
        self.assertIsNone(_tiered_billing_units("nai-diffusion-4-curated-preview"))
        self.assertIsNone(_tiered_billing_units("nai-diffusion-3"))
        self.assertIsNone(_tiered_billing_units("nai-diffusion-furry-3"))
        self.assertIsNone(_tiered_billing_units(""))
        self.assertIsNone(_tiered_billing_units("xialong"))


class OpusFreeEnvelopeTests(unittest.TestCase):
    """档位判定与 _enforce_limit_model 边界一致。"""

    def test_plain_text2img_in_envelope(self) -> None:
        self.assertTrue(_in_opus_free_envelope({"size": "832x1216", "steps": 28}))
        self.assertTrue(_in_opus_free_envelope({}))
        self.assertTrue(_in_opus_free_envelope({"size": "1024x1024", "steps": 28, "n": 1}))

    def test_steps_over_28_out(self) -> None:
        self.assertFalse(_in_opus_free_envelope({"steps": 29}))
        self.assertFalse(_in_opus_free_envelope({"steps": 50}))
        # 字符串数字同样要判出来
        self.assertFalse(_in_opus_free_envelope({"steps": "29"}))

    def test_area_over_1024_squared_out(self) -> None:
        self.assertFalse(_in_opus_free_envelope({"size": "1536x1024"}))
        self.assertFalse(_in_opus_free_envelope({"size": "1216x1216"}))
        self.assertTrue(_in_opus_free_envelope({"size": "1216x832"}))
        self.assertFalse(_in_opus_free_envelope({"width": 1024, "height": 1200}))

    def test_multiple_samples_out(self) -> None:
        self.assertFalse(_in_opus_free_envelope({"n": 2}))
        self.assertFalse(_in_opus_free_envelope({"n_samples": 3}))

    def test_priority_out(self) -> None:
        self.assertFalse(_in_opus_free_envelope({"service_tier": "priority"}))
        self.assertTrue(_in_opus_free_envelope({"service_tier": "auto"}))

    def test_generate_with_image_out_but_img2img_ok(self) -> None:
        body = {"image": "aGVsbG8="}
        self.assertFalse(_in_opus_free_envelope(body))
        self.assertTrue(_in_opus_free_envelope(body, action="img2img"))
        self.assertTrue(_in_opus_free_envelope(body, action="infill"))

    def test_unsupported_action_out(self) -> None:
        self.assertFalse(_in_opus_free_envelope({"action": "upscale"}))

    def test_precise_reference_out(self) -> None:
        self.assertFalse(_in_opus_free_envelope({"references": ["abc"]}))

    def test_raw_reference_image_out_encoded_vibe_ok(self) -> None:
        raw = {"reference_image": _raw_png_b64()}
        self.assertFalse(_in_opus_free_envelope(raw))
        encoded = {"reference_images": [_encoded_vibe_b64()]}
        self.assertTrue(_in_opus_free_envelope(encoded))

    def test_too_many_encoded_vibes_out(self) -> None:
        vibes = [_encoded_vibe_b64()] * 5
        self.assertFalse(_in_opus_free_envelope({"reference_images": vibes}))


class BillingPromptTokensTests(unittest.TestCase):
    """档内固定价 / 档外回落动态口径 / 不参与两档计费的模型。"""

    def test_v5_in_envelope_is_8(self) -> None:
        self.assertEqual(_billing_prompt_tokens("nai-diffusion-5-full", {"steps": 28}), 8)

    def test_v5_out_of_envelope_falls_back_to_legacy(self) -> None:
        self.assertIsNone(_billing_prompt_tokens("nai-diffusion-5-full", {"steps": 50}))
        self.assertIsNone(_billing_prompt_tokens("nai-v5-full", None))

    def test_v45_in_envelope_is_0(self) -> None:
        self.assertEqual(_billing_prompt_tokens("nai-diffusion-4-5-full", {}), 0)

    def test_v45_out_of_envelope_falls_back_to_legacy(self) -> None:
        self.assertIsNone(_billing_prompt_tokens("nai-diffusion-4-5-full", {"size": "1536x1024"}))
        self.assertIsNone(_billing_prompt_tokens("nai-v4.5-full", None))

    def test_other_models_return_none(self) -> None:
        self.assertIsNone(_billing_prompt_tokens("nai-diffusion-3", {}))
        self.assertIsNone(_billing_prompt_tokens("nai-diffusion-3", None))

    def test_gateway_identifier_accepted(self) -> None:
        self.assertEqual(_billing_prompt_tokens("nai-v5-curated", {"steps": 28}), 8)


class ResponseUsageInjectionTests(unittest.TestCase):
    """响应 usage 注入：档内固定价优先，档外沿用 Anlas 换算旧口径。"""

    def _usage_of(self, **kwargs) -> dict:
        response = _build_image_response_v2(
            _request_stub(), _zip_bytes(), "test prompt", "b64_json", **kwargs
        )
        return json.loads(response.body)["usage"]

    def test_limit_tier_usage_is_8_for_v5(self) -> None:
        usage = self._usage_of(anlas_cost=17, billing_prompt_tokens=8)
        self.assertEqual(usage["prompt_tokens"], 8)
        self.assertEqual(usage["total_tokens"], 8)
        self.assertEqual(usage["completion_tokens"], 0)

    def test_v45_free_tier_usage_is_zero(self) -> None:
        usage = self._usage_of(anlas_cost=0, billing_prompt_tokens=0)
        self.assertEqual(usage["prompt_tokens"], 0)

    def test_legacy_usage_without_billing(self) -> None:
        usage = self._usage_of(anlas_cost=17)
        self.assertEqual(usage["prompt_tokens"], 850)

    def test_legacy_usage_zero_anlas_falls_back_to_base(self) -> None:
        usage = self._usage_of()
        self.assertEqual(usage["prompt_tokens"], 1000)

    def test_png_response_billing_override(self) -> None:
        response = _build_png_image_response(
            _png_bytes("red"), "test", anlas_cost=5, billing_prompt_tokens=8
        )
        self.assertEqual(json.loads(response.body)["usage"]["prompt_tokens"], 8)

    def test_png_response_legacy(self) -> None:
        response = _build_png_image_response(_png_bytes("red"), "test", anlas_cost=5)
        self.assertEqual(json.loads(response.body)["usage"]["prompt_tokens"], 250)
        response = _build_png_image_response(_png_bytes("red"), "test", anlas_cost=0)
        self.assertEqual(json.loads(response.body)["usage"]["prompt_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
