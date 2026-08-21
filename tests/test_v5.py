"""V5 模型支持的单测：params_version、计费倍率、每日/每周双限额。"""

import base64
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from fastapi import HTTPException
from PIL import Image

from src.proxy.openai import (
    _build_generation_payload,
    _calc_anlas_cost,
    _is_v4_family,
    _is_v5_model,
    _v5_price_multiplier,
)
from src.proxy import v5_quota


def _png_bytes(color: str = "red") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return output.getvalue()


class V5ModelHelpersTests(unittest.TestCase):
    """V5 模型识别与销售倍率。"""

    def test_is_v5_model_detects_diffusion_5(self) -> None:
        self.assertTrue(_is_v5_model("nai-diffusion-5-full"))
        self.assertTrue(_is_v5_model("nai-diffusion-5-curated-inpainting"))
        self.assertFalse(_is_v5_model("nai-diffusion-4-5-full"))
        self.assertFalse(_is_v5_model("nai-diffusion-4-curated-preview"))
        self.assertFalse(_is_v5_model(""))

    def test_is_v4_family_covers_v4_v45_v5(self) -> None:
        self.assertTrue(_is_v4_family("nai-diffusion-5-full"))
        self.assertTrue(_is_v4_family("nai-diffusion-4-5-full"))
        self.assertTrue(_is_v4_family("nai-diffusion-4-curated-preview"))
        self.assertFalse(_is_v4_family("nai-diffusion-3"))
        self.assertFalse(_is_v4_family(""))

    def test_v5_price_multiplier_is_two(self) -> None:
        self.assertEqual(_v5_price_multiplier("nai-diffusion-5-full"), 2.0)
        self.assertEqual(_v5_price_multiplier("nai-diffusion-5-curated-inpainting"), 2.0)
        self.assertEqual(_v5_price_multiplier("nai-diffusion-4-5-full"), 1.0)
        self.assertEqual(_v5_price_multiplier("nai-diffusion-3"), 1.0)


class V5GenerationPayloadTests(unittest.TestCase):
    """V5 载荷：params_version=4，且保留 v4_prompt 结构。"""

    def test_v5_payload_uses_params_version_4(self) -> None:
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {"model": "nai-diffusion-5-full", "prompt": "test"}
        )
        self.assertEqual(nai_payload["model"], "nai-diffusion-5-full")
        self.assertEqual(nai_payload["parameters"]["params_version"], 4)
        self.assertIn("v4_prompt", nai_payload["parameters"])
        # V5 同样强制关闭 SMEA
        self.assertFalse(nai_payload["parameters"]["sm"])
        self.assertFalse(nai_payload["parameters"]["sm_dyn"])

    def test_v45_payload_keeps_params_version_3(self) -> None:
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {"model": "nai-diffusion-4-5-full", "prompt": "test"}
        )
        self.assertEqual(nai_payload["parameters"]["params_version"], 3)

    def test_v5_inpainting_payload_uses_params_version_4(self) -> None:
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {"model": "nai-diffusion-5-full-inpainting", "prompt": "test"}
        )
        self.assertEqual(nai_payload["parameters"]["params_version"], 4)

    def test_v5_precise_reference_allowed(self) -> None:
        """Precise Reference 应放行 V5 模型。"""
        nai_payload, _prompt, _fmt = _build_generation_payload(
            {
                "model": "nai-diffusion-5-full",
                "prompt": "test",
                "references": [
                    {
                        "reference_image": base64.b64encode(_png_bytes()).decode(),
                        "reference_type": "character",
                    }
                ],
            },
            operation="precise-reference",
        )
        self.assertEqual(nai_payload["parameters"]["params_version"], 4)


class V5AnlasCostTests(unittest.TestCase):
    """V5 销售定价 = V4.5 × 2。"""

    def test_v5_non_limit_is_double_v45(self) -> None:
        for width, height, steps in [
            (1024, 1024, 28),
            (512, 512, 28),
            (832, 1216, 28),
            (1024, 1024, 10),
            (1600, 1600, 28),
        ]:
            v45 = _calc_anlas_cost(
                width=width, height=height, steps=steps, n_samples=1,
                price_multiplier=1.0,
            )
            v5 = _calc_anlas_cost(
                width=width, height=height, steps=steps, n_samples=1,
                price_multiplier=2.0,
            )
            self.assertEqual(v5, v45 * 2, f"{width}x{height} steps={steps}")

    def test_v5_multiplier_applies_to_reference_fees(self) -> None:
        v45 = _calc_anlas_cost(
            width=1024, height=1024, steps=28, n_samples=1,
            reference_image_count=1,
            price_multiplier=1.0,
        )
        v5 = _calc_anlas_cost(
            width=1024, height=1024, steps=28, n_samples=1,
            reference_image_count=1,
            price_multiplier=2.0,
        )
        self.assertEqual(v5, v45 * 2)

    def test_v5_limit_stays_free(self) -> None:
        """-limit 免费额度路径 total=0，倍率不产生额外费用。"""
        cost = _calc_anlas_cost(
            width=1024, height=1024, steps=28, n_samples=1,
            is_opus=True, price_multiplier=2.0,
        )
        self.assertEqual(cost, 0)

    def test_v5_default_tokens_are_double(self) -> None:
        """V5 非 limit 基础图（40 销售 Anlas）应返回 2000 token。"""
        from src.proxy.openai import _anlas_to_tokens

        v5_anlas = _calc_anlas_cost(
            width=1024, height=1024, steps=28, n_samples=1,
            price_multiplier=2.0,
        )
        self.assertEqual(v5_anlas, 40)
        self.assertEqual(_anlas_to_tokens(v5_anlas), 2000)


class V5QuotaTests(unittest.TestCase):
    """V5 双限额：每日 190 张（官方补充速率）+ 每周 1730 张硬顶。"""

    def setUp(self) -> None:
        self._fd, self._tmp_path = tempfile.mkstemp(suffix=".json")
        os.close(self._fd)
        os.unlink(self._tmp_path)
        self._orig = v5_quota.V5_USAGE_JSON
        v5_quota.V5_USAGE_JSON = self._tmp_path

    def tearDown(self) -> None:
        v5_quota.V5_USAGE_JSON = self._orig
        if os.path.exists(self._tmp_path):
            os.unlink(self._tmp_path)

    def test_quota_constants(self) -> None:
        self.assertEqual(v5_quota.V5_WEEKLY_LIMIT, 1730)
        self.assertEqual(v5_quota.V5_DAILY_LIMIT, 190)

    def test_non_v5_models_always_pass(self) -> None:
        v5_quota.check_v5_quota("nai-diffusion-4-5-full", 100)
        v5_quota.record_v5_generation("nai-diffusion-4-5-full", 100)
        self.assertEqual(v5_quota.get_usage()["used_today"], 0)

    def test_check_and_record_roundtrip(self) -> None:
        v5_quota.check_v5_quota("nai-diffusion-5-full", 1)
        v5_quota.record_v5_generation("nai-diffusion-5-full", 1)
        v5_quota.record_v5_generation("nai-diffusion-5-full", 2)
        self.assertEqual(v5_quota.get_usage()["used_today"], 3)
        self.assertEqual(v5_quota.get_usage()["remaining_today"], 190 - 3)

    def test_check_rejects_when_over_daily_limit(self) -> None:
        v5_quota.record_v5_generation("nai-diffusion-5-full", 190)
        with self.assertRaises(ValueError) as raised:
            v5_quota.check_v5_quota("nai-diffusion-5-full", 1)
        self.assertIn("今日免费额度已用完", str(raised.exception))

    def test_partial_remaining_rejects_batch(self) -> None:
        v5_quota.record_v5_generation("nai-diffusion-5-full", 189)
        with self.assertRaises(ValueError):
            v5_quota.check_v5_quota("nai-diffusion-5-full", 2)
        # 剩 1 张时，单张请求仍可放行
        v5_quota.check_v5_quota("nai-diffusion-5-full", 1)

    def test_weekly_limit_blocks_when_daily_under(self) -> None:
        """单日不足 190 但滚动 7 天达 1730 时，仍应拒绝。"""
        today = v5_quota._today()
        day = datetime.strptime(today, "%Y-%m-%d").date()
        usage: dict[str, int] = {}
        # 近 6 天每天 288 张（1728），今天再生成 2 张即达周顶 1730
        for i in range(1, 7):
            usage[(day - timedelta(days=i)).isoformat()] = 288
        usage[today] = 0
        with open(v5_quota.V5_USAGE_JSON, "w", encoding="utf-8") as f:
            json.dump(usage, f)
        v5_quota.record_v5_generation("nai-diffusion-5-full", 2)
        # 今日 2/190，但周窗口 1730/1730 → 拒绝
        self.assertEqual(v5_quota.get_usage()["used_today"], 2)
        self.assertEqual(v5_quota.get_usage()["used_this_week"], 1730)
        with self.assertRaises(ValueError) as raised:
            v5_quota.check_v5_quota("nai-diffusion-5-full", 1)
        self.assertIn("本周免费额度已用完", str(raised.exception))

    def test_limit_variant_also_counted(self) -> None:
        """-limit 变体同样计入 V5 限额。"""
        v5_quota.check_v5_quota("nai-diffusion-5-full-inpainting", 1)
        v5_quota.record_v5_generation("nai-diffusion-5-full-inpainting", 1)
        self.assertEqual(v5_quota.get_usage()["used_today"], 1)

    def test_record_with_params_logs_and_counts(self) -> None:
        """带请求参数计数：不影响计数，日志含尺寸/步数/sampler。"""
        v5_quota.record_v5_generation(
            "nai-diffusion-5-full",
            1,
            params={"width": 1024, "height": 1024, "steps": 28, "sampler": "k_euler"},
        )
        self.assertEqual(v5_quota.get_usage()["used_today"], 1)

    def test_fmt_params_skips_missing_fields(self) -> None:
        self.assertEqual(
            v5_quota._fmt_params({"width": 832, "height": 1216, "steps": 28}),
            "832×1216 · 28步",
        )
        self.assertEqual(v5_quota._fmt_params({"sampler": "k_euler_ancestral"}), "k_euler_ancestral")
        self.assertEqual(v5_quota._fmt_params(None), "")
        self.assertEqual(v5_quota._fmt_params({}), "")

    def test_log_generation_v45_no_count(self) -> None:
        """V4.5 走 log_generation 只打日志，不计数。"""
        v5_quota.log_generation(
            "nai-diffusion-4-5-full",
            2,
            params={"width": 1024, "height": 1024, "steps": 28},
        )
        self.assertEqual(v5_quota.get_usage()["used_today"], 0)

    def test_log_generation_v5_counts(self) -> None:
        """V5 走 log_generation 正常计数（复用 record_v5_generation）。"""
        v5_quota.log_generation(
            "nai-diffusion-5-full",
            3,
            params={"width": 832, "height": 1216, "steps": 28, "sampler": "k_euler"},
        )
        self.assertEqual(v5_quota.get_usage()["used_today"], 3)

    def test_log_generation_invalid_skipped(self) -> None:
        v5_quota.log_generation(None, 1)
        v5_quota.log_generation("nai-diffusion-5-full", 0)
        self.assertEqual(v5_quota.get_usage()["used_today"], 0)


if __name__ == "__main__":
    unittest.main()
