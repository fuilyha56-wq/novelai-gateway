import unittest

from fastapi import HTTPException

from src.proxy.openai import _build_generation_payload, _calc_anlas_cost, validate_nai_canvas, native_anlas_quote, _enforce_limit_model, _build_multipart_form
import base64
import json


class OfficialCanvasPricingTests(unittest.TestCase):
    def test_legal_large_canvas_is_not_silently_resized(self):
        for width, height in [(2048, 1536), (4096, 768)]:
            payload, _, _ = _build_generation_payload({"model": "nai-v5-full", "width": width, "height": height, "steps": 23})
            self.assertEqual((payload["parameters"]["width"], payload["parameters"]["height"]), (width, height))

    def test_area_and_cost_boundary_fail_before_upstream(self):
        validate_nai_canvas("nai-v5-full", {"width": 2048, "height": 1536, "steps": 46})
        for width, height, steps in [(2048, 2048, 23), (2048, 1563, 23), (2048, 1536, 47)]:
            with self.subTest(width=width, height=height, steps=steps), self.assertRaises(HTTPException) as raised:
                validate_nai_canvas("nai-v5-full", {"width": width, "height": height, "steps": steps})
            self.assertEqual(raised.exception.status_code, 400)

    def test_limit_keeps_legal_portrait_and_rejects_paid_canvas(self):
        validate_nai_canvas("nai-v5-full-limit", {"width": 832, "height": 1216, "steps": 28})
        with self.assertRaises(HTTPException):
            validate_nai_canvas("nai-v5-full-limit", {"width": 2048, "height": 1536, "steps": 28})

    def test_medium_can_price_and_submit_a_large_batch(self):
        payload, _, _ = _build_generation_payload({"model": "nai-v5-full-medium", "width": 2048, "height": 1536, "n": 2})
        self.assertEqual(payload["parameters"]["n_samples"], 2)
        self.assertEqual(payload["parameters"]["steps"], 14)
        self.assertEqual(_calc_anlas_cost(2048, 1536, 14, n_samples=2, medium=True), 102)

    def test_native_operation_quotes_match_official_strength_and_reference_rules(self):
        encoded = base64.b64encode(bytes(2000)).decode()
        cases = [
            ("generate", {"strength": 0.2}, 90),
            ("img2img", {}, 63),
            ("img2img", {"strength": 0.55}, 50),
            ("img2img", {"strength": 0}, 2),
            ("infill", {"strength": 0.2}, 90),
            ("infill", {"inpaintImg2ImgStrength": 0.55}, 50),
            ("img2img", {"n_samples": 2}, 126),
            ("generate", {"director_reference_images": ["ref"], "n_samples": 2}, 190),
            ("generate", {"reference_image_multiple": ["raw"], "n_samples": 2}, 182),
            ("generate", {"director_reference_images": ["ref"], "reference_image_multiple": ["raw"]}, 97),
            ("generate", {"reference_image_multiple": [encoded] * 5, "width": 1024, "height": 1024, "steps": 23}, 28),
            ("generate", {"reference_image_multiple_cached": [{"cache_secret_key": "cached"}]}, 90),
            ("generate", {"reference_image_multiple": ["raw"], "reference_image_multiple_cached": [{"cache_secret_key": "cached"}]}, 92),
        ]
        for action, overrides, expected in cases:
            with self.subTest(action=action, overrides=list(overrides)):
                params = {"width": 2048, "height": 1536, "steps": 28, "n_samples": 1, **overrides}
                self.assertEqual(native_anlas_quote("/ai/generate-image", {"model": "nai-diffusion-5-full", "action": action, "parameters": params}), (expected, expected * 50))

    def test_native_cached_references_do_not_double_count_for_the_fixed_envelope(self):
        params = {"width": 1024, "height": 1024, "steps": 28, "reference_image_multiple_cached": [{"cache_secret_key": "cached"}] * 4}
        self.assertEqual(native_anlas_quote("/ai/generate-image", {"model": "nai-diffusion-5-full", "parameters": params}), (30, 25))
        params["reference_image_multiple_cached"].append({"cache_secret_key": "fifth"})
        self.assertEqual(native_anlas_quote("/ai/generate-image", {"model": "nai-diffusion-5-full", "parameters": params}), (32, 1600))
        with self.assertRaises(HTTPException):
            _enforce_limit_model("nai-v5-full-limit", params)

    def test_multipart_keeps_existing_cached_references_alongside_new_references(self):
        encoded = base64.b64encode(bytes(2000)).decode()
        _, files = _build_multipart_form({"parameters": {"reference_image_multiple": [encoded], "reference_image_multiple_cached": [{"cache_secret_key": "existing"}]}})
        request = json.loads(next(item[1][1] for item in files if item[0] == "request"))
        self.assertEqual(len(request["parameters"]["reference_image_multiple_cached"]), 2)
        self.assertEqual(request["parameters"]["reference_image_multiple_cached"][1], {"cache_secret_key": "existing"})
