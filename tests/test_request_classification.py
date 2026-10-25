import pytest

from src.proxy.openai import _requires_anlas_for_account


@pytest.mark.parametrize("model,overrides,needs_anlas", [
    ("nai-diffusion-5-full", {}, False),
    ("nai-diffusion-4-5-full", {}, False),
    ("nai-diffusion-5-full", {"size": "1600x1600"}, True),
    ("nai-diffusion-5-full", {"steps": 50}, True),
    ("nai-diffusion-5-full", {"references": [{"image": "reference"}]}, True),
])
def test_model_name_without_limit_alias_still_uses_actual_free_envelope(model, overrides, needs_anlas):
    body = {"model": "nai-v5-full", "size": "832x1216", "steps": 28, "n": 1, **overrides}
    assert _requires_anlas_for_account(model, body) is needs_anlas
