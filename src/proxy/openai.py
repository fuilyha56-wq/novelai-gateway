"""
OpenAI API 兼容适配器。

将 OpenAI 格式的请求转换为 NovelAI 格式，并将响应转回 OpenAI 格式。
支持: 图像生成(4种返回格式)、文本Chat、TTS、图像工具端点。
"""

import json
import time
import io
import uuid
import zipfile
import base64
import logging
import math
from typing import Any

from starlette.datastructures import UploadFile as StarletteUploadFile
from fastapi import Request, Response, HTTPException
from fastapi.responses import StreamingResponse

try:
    from PIL import Image, UnidentifiedImageError
except ImportError:
    Image = None  # PIL 可选，用于未来扩展
    UnidentifiedImageError = Exception

from .config import get_request_auth_token, settings
from .queue import gate
from .stats import record_generation

logger = logging.getLogger("gateway")

# ── 常量 ─────────────────────────────────────────────────────

# 默认负面提示词
DEFAULT_NEGATIVE_PROMPT = (
    "lowres, {bad}, error, fewer, extra, missing, worst quality, low quality, "
    "normal quality, jpeg artifacts, signature, watermark, username, blurry, "
    "bad anatomy, bad hands, bad feet, bad proportions, {extra digits}, lowres, "
    "{bad}, error, missing fingers, extra digit, fewer digits, bad hands, "
    "lower quality, normal quality, jpeg artifacts, signature, watermark, "
    "username, blurry, text, logo"
)

# 官方支持的画幅
VALID_SIZES = {(1024, 1024), (1216, 832), (832, 1216)}

# NovelAI SDK 默认质量标签
QUALITY_TAGS = ", very aesthetic, masterpiece, no text"

# NovelAI 图片 API 尺寸限制
MIN_IMAGE_DIM = 64
MAX_IMAGE_DIM = 1600
MIN_STEPS = 1
MAX_STEPS = 50
MIN_N_SAMPLES = 1
MAX_N_SAMPLES = 6

# Opus 免费额度边界（超出任一即消耗 Anlas）
OPUS_FREE_MAX_STEPS = 28
OPUS_FREE_MAX_PIXELS = 1024 * 1024  # 1024×1024

# -limit 后缀模型：限制版，只允许走 Opus 免费额度的文生图路径
LIMIT_MODEL_SUFFIX = "-limit"
VALID_SAMPLERS = {
    "k_euler", "k_euler_ancestral", "k_dpmpp_2s_ancestral", "k_dpmpp_2m",
    "k_dpmpp_2m_sde", "k_dpmpp_sde", "ddim_v3",
}
VALID_NOISE_SCHEDULES = {"native", "karras", "exponential", "polyexponential"}
VALID_RESPONSE_FORMATS = {"b64_json", "url", "raw", "nai_json", "auto"}
VALID_TTS_VERSIONS = {"v1", "v2"}
VALID_TTS_VOICE_IDS = {-1, 0, 1}
TTS_RESPONSE_FORMAT_TO_OPUS = {"mp3": False, "opus": True}


def _validate_image_params(
    width: int,
    height: int,
    steps: int,
    n_samples: int = 1,
    scale: float = 5.0,
    strength: float | None = None,
    seed: int | None = None,
    sampler: str | None = None,
    noise_schedule: str | None = None,
    response_format: str | None = None,
) -> None:
    """统一校验图像生成参数。

    数值类（width/height/steps/n_samples/scale/strength/seed）越界直接抛 400，
    枚举类（sampler/noise_schedule/response_format）不在白名单只记 warning，仍透传给 NAI，
    由 NAI 自己决定是否拒绝。
    """
    if not (MIN_IMAGE_DIM <= width <= MAX_IMAGE_DIM):
        raise HTTPException(status_code=400, detail=f"width 必须在 {MIN_IMAGE_DIM}-{MAX_IMAGE_DIM} 之间，当前 {width}")
    if not (MIN_IMAGE_DIM <= height <= MAX_IMAGE_DIM):
        raise HTTPException(status_code=400, detail=f"height 必须在 {MIN_IMAGE_DIM}-{MAX_IMAGE_DIM} 之间，当前 {height}")
    if width % 64 != 0:
        raise HTTPException(status_code=400, detail=f"width 必须是 64 的倍数，当前 {width}")
    if height % 64 != 0:
        raise HTTPException(status_code=400, detail=f"height 必须是 64 的倍数，当前 {height}")
    if not (MIN_STEPS <= steps <= MAX_STEPS):
        raise HTTPException(status_code=400, detail=f"steps 必须在 {MIN_STEPS}-{MAX_STEPS} 之间，当前 {steps}")
    if not (MIN_N_SAMPLES <= n_samples <= MAX_N_SAMPLES):
        raise HTTPException(status_code=400, detail=f"n_samples 必须在 {MIN_N_SAMPLES}-{MAX_N_SAMPLES} 之间，当前 {n_samples}")
    if not (0 <= scale <= 10):
        raise HTTPException(status_code=400, detail=f"scale 必须在 0-10 之间，当前 {scale}")
    if strength is not None and not (0 <= strength <= 1):
        raise HTTPException(status_code=400, detail=f"strength 必须在 0-1 之间，当前 {strength}")
    if seed is not None and not (0 <= seed <= 4294967295):
        raise HTTPException(status_code=400, detail=f"seed 必须在 0-4294967295 之间，当前 {seed}")
    if sampler is not None and sampler not in VALID_SAMPLERS:
        logger.warning(f"sampler 不在白名单: {sampler}，合法值: {VALID_SAMPLERS}（仍透传给 NAI）")
    if noise_schedule is not None and noise_schedule not in VALID_NOISE_SCHEDULES:
        logger.warning(f"noise_schedule 不在白名单: {noise_schedule}，合法值: {VALID_NOISE_SCHEDULES}（仍透传给 NAI）")
    if response_format is not None and response_format not in VALID_RESPONSE_FORMATS:
        raise HTTPException(status_code=400, detail=f"response_format 非法，当前 {response_format}，合法值: {VALID_RESPONSE_FORMATS}")


# ── -limit 模型校验 ─────────────────────────────────────────────

def is_limit_model(model_identifier: str | None) -> bool:
    """判断 model_identifier 是否为 -limit 限制版模型。"""
    return isinstance(model_identifier, str) and model_identifier.endswith(LIMIT_MODEL_SUFFIX)


def _enforce_limit_model(model: str, body: dict[str, Any]) -> None:
    """-limit 模型限制：只允许走 Opus 免费额度的生成、图生图或重绘路径。

    Opus 免费额度需同时满足：
      - n_samples == 1
    - 无参考图；generate 不允许输入图片
      - width * height <= 1024*1024
      - steps <= 28
    - action 为 generate、img2img 或 infill
      - service_tier != priority
    任一超出即抛 400。非 -limit 模型直接通过。
    """
    if not is_limit_model(model):
        return

    # 1. 不允许参考图；文生图也不允许携带输入图片。
    # img2img/infill 的输入图片不额外收费，符合其余边界时仍可使用免费额度。
    action = body.get("action", "generate")
    forbidden_image_keys = ["reference_image", "reference_images"]
    if action == "generate":
        forbidden_image_keys.append("image")
    forbidden_image_keys.append("references")
    for key in forbidden_image_keys:
        if body.get(key):
            raise HTTPException(
                status_code=400,
                detail=f"-limit 模型 '{model}' 不允许消耗 Anlas，请移除 {key} 参数或改用原版模型（去掉 -limit 后缀）",
            )

    # 2. action 必须是可使用免费额度的生成模式。
    if action not in {"generate", "img2img", "infill"}:
        raise HTTPException(
            status_code=400,
            detail=f"-limit 模型 '{model}' 不允许 action='{action}'，仅支持 generate、img2img 或 infill",
        )

    # 3. n_samples 必须为 1。OpenAI 兼容入口允许用 n 指定样本数。
    n_samples = _safe_int(body.get("n_samples", body.get("n", 1)), 1)
    if n_samples != 1:
        raise HTTPException(
            status_code=400,
            detail=f"-limit 模型 '{model}' 不允许消耗 Anlas，n_samples 必须 = 1，当前 {n_samples}",
        )

    # 4. steps ≤ 28
    steps = _safe_int(body.get("steps", OPUS_FREE_MAX_STEPS), OPUS_FREE_MAX_STEPS)
    if steps > OPUS_FREE_MAX_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"-limit 模型 '{model}' 不允许消耗 Anlas，steps 必须 ≤ {OPUS_FREE_MAX_STEPS}，当前 {steps}",
        )

    # 5. 尺寸 ≤ 1024×1024（width*height ≤ 1048576）
    size_str = body.get("size", "")
    width, height = _parse_size(size_str, 1024, 1024)
    width = _safe_int(body.get("width", width), width)
    height = _safe_int(body.get("height", height), height)
    if width * height > OPUS_FREE_MAX_PIXELS:
        raise HTTPException(
            status_code=400,
            detail=f"-limit 模型 '{model}' 不允许消耗 Anlas，尺寸必须 ≤ 1024×1024（当前 {width}×{height} 超限）",
        )

    # 6. service_tier 不允许为 priority
    if body.get("service_tier") == "priority":
        raise HTTPException(
            status_code=400,
            detail=f"-limit 模型 '{model}' 不允许 service_tier='priority'（priority 会扣 Anlas）",
        )


def _reject_limit_model_for_paid_endpoint(model: str) -> None:
    """对不具备 Opus 免费生成资格的端点拒绝 -limit 模型。

    单张、28 steps 以内且不超过 1024x1024 的图生图或重绘也可使用 Opus
    免费额度，因此由各端点转换为通用生成参数后调用 ``_enforce_limit_model``。
    Vibe、参考图、放大和 Director 工具仍必然产生额外 Anlas 费用。
    """
    if is_limit_model(model):
        raise HTTPException(
            status_code=400,
            detail=f"-limit 模型 '{model}' 不允许此端点（该操作必然消耗 Anlas），请改用原版模型（去掉 -limit 后缀）",
        )

# NAI 请求头模板
_NAI_HEADERS_TEMPLATE = {
    "Content-Type": "application/json",
    "Accept": "application/zip",
    "Origin": "https://novelai.net",
    "Referer": "https://novelai.net/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


# ── Registry 引用（由 app.py 启动时设置） ───────────────────────

_registry = None


def set_registry(registry):
    """由 app.py 在启动时调用，注入 ModelRegistry 实例。"""
    global _registry
    _registry = registry


def get_registry():
    """获取模型注册表。"""
    return _registry


# ── 工具函数 ──────────────────────────────────────────────────

def _parse_size(size_str: str, default_w: int = 1024, default_h: int = 1024) -> tuple[int, int]:
    """解析 'WxH' 格式的尺寸字符串。"""
    if not size_str:
        return default_w, default_h
    try:
        parts = size_str.lower().split("x")
        if len(parts) == 2:
            return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        pass
    return default_w, default_h


def _clamp_dimensions(width: int, height: int) -> tuple[int, int]:
    """将宽高限制在 NAI 允许范围内，并确保为 64 的倍数。"""
    width = max(MIN_IMAGE_DIM, min(MAX_IMAGE_DIM, width))
    height = max(MIN_IMAGE_DIM, min(MAX_IMAGE_DIM, height))
    width = (width // 64) * 64
    height = (height // 64) * 64
    return width, height


def _safe_int(value, default: int) -> int:
    """安全地将值转换为 int，转换失败时返回默认值。"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _normalize_token(raw_token: str) -> str:
    """清理共享凭据字符串，移除空白字符与外层引号。"""
    return raw_token.strip().strip("'\"")


def _get_auth_token(request: Request) -> str:
    """获取当前请求的共享凭据，多 API Key 时复用本请求已轮到的 Key。"""
    return get_request_auth_token(request)


def _get_image_url(request: Request, filename: str) -> str:
    """生成图片的访问 URL。"""
    if settings.image_base_url:
        base = settings.image_base_url.rstrip("/")
        return f"{base}/images/{filename}"
    return f"{request.url.scheme}://{request.url.netloc}/images/{filename}"


def _extract_pngs_from_response(response_bytes: bytes) -> list[bytes]:
    """从上游响应中提取全部 PNG 图片，并统一转换为 RGB。"""
    return [_flatten_to_rgb(raw) for raw in _extract_raw_pngs(response_bytes)]


def _extract_png_from_zip(zip_bytes: bytes) -> bytes:
    """兼容旧调用：提取上游响应中的第一张 PNG。"""
    return _extract_pngs_from_response(zip_bytes)[0]


def _extract_raw_pngs(response_bytes: bytes) -> list[bytes]:
    """从响应中提取全部原始 PNG 字节。

    支持三种上游响应格式：
    1. ZIP 压缩包（v3 模型，Accept: application/zip）
    2. JSON（v4/v4.5 模型，Accept: application/json）：解析 {"images":[{"image":"<b64>"}]}
    3. 裸 PNG bytes（fallback）
    """
    # 1. 尝试 ZIP
    try:
        with zipfile.ZipFile(io.BytesIO(response_bytes)) as zf:
            images = [
                zf.read(name)
                for name in zf.namelist()
                if name.lower().endswith(".png")
            ]
            if images:
                return images
    except (zipfile.BadZipFile, Exception):
        pass

    # 2. 尝试 JSON（v4/v4.5 返回格式）
    try:
        body = json.loads(response_bytes.decode("utf-8"))
        images = body.get("images") if isinstance(body, dict) else None
        if isinstance(images, list) and images:
            decoded: list[bytes] = []
            for item in images:
                if not isinstance(item, dict):
                    continue
                b64 = item.get("image") or item.get("b64_json")
                if isinstance(b64, str) and b64:
                    decoded.append(base64.b64decode(b64))
            if decoded:
                return decoded
    except (ValueError, UnicodeDecodeError, TypeError):
        pass

    # 3. fallback: 可能直接就是 PNG
    return [response_bytes]


def _extract_raw_png(zip_bytes: bytes) -> bytes:
    """兼容旧调用：提取上游响应中的第一张原始 PNG。"""
    return _extract_raw_pngs(zip_bytes)[0]


def _flatten_to_rgb(png_bytes: bytes) -> bytes:
    """将 PNG 统一为 RGB 模式（flatten alpha 到白色背景），保留 NAI 元数据。

    NAI v4.5 返回 RGBA PNG，alpha 通道几乎全不透明（254-255）。
    保留 alpha 会导致：
    - 浏览器/客户端放大时 alpha 与 RGB 插值不一致 → 边缘噪点
    - JPEG 转换时 alpha 被误当颜色通道 → 红色噪点
    - NewAPI 等中转站二次处理时偏色

    如果图片已经是 RGB 则原样返回；否则 flatten 后重新编码为 PNG。
    重新编码时保留原始 PNG 的 tEXt 元数据（NAI 的 Comment/Description/Software/Source 等），
    以便下游工具读取 NAI 元数据反推提示词。
    """
    try:
        from PIL import Image
        from PIL.PngImagePlugin import PngInfo

        img = Image.open(io.BytesIO(png_bytes))
        if img.mode == "RGB":
            return png_bytes  # 已经是 RGB，无需转换

        # 提取原始 PNG 元数据（tEXt/zTXt/iTXt chunks）
        # img.info 里包含 Comment, Description, Software, Source 等 NAI 元数据字段
        original_info = dict(img.info) if img.info else {}

        # flatten alpha 到白色背景
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode != "RGBA":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1])  # 用 alpha 通道作为 mask
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # 重新编码时保留元数据
        pnginfo = PngInfo()
        for key, value in original_info.items():
            # 跳过 PIL 内部字段，只保留 tEXt 类型的元数据
            if key in ("icc_profile", "gamma", "aspect", "physical"):
                continue
            if isinstance(value, (str, bytes)):
                pnginfo.add_text(key, value)

        buf = io.BytesIO()
        img.save(buf, format="PNG", pnginfo=pnginfo)
        return buf.getvalue()
    except Exception:
        # PIL 处理失败时返回原始数据（不阻断流程）
        return png_bytes


def _strip_data_prefix(b64_str: str) -> str:
    """剥离 data:image/...;base64, 前缀。"""
    if b64_str.startswith("data:"):
        try:
            return b64_str.split(",", 1)[1]
        except IndexError:
            return b64_str
    return b64_str


def _normalize_precise_reference_image(image_b64: str) -> str:
    """规范化 Director Reference 图片为 NAI 网页端使用的固定 PNG 画布。

    官网会依据原图宽高比选择 1024x1536、1536x1024 或 1472x1472，
    按比例缩放后居中粘贴到黑色背景。未做该处理的普通小图会被
    NAI Director Reference 编码器拒绝。
    """
    if Image is None:
        raise HTTPException(status_code=500, detail="Pillow 未安装，无法规范化 Precise Reference 图片")

    try:
        raw = base64.b64decode(_strip_data_prefix(image_b64))
        source = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as error:
        raise HTTPException(status_code=400, detail=f"无法解析 Precise Reference 图片: {error}") from error

    targets = ((1024, 1536), (1536, 1024), (1472, 1472))
    source_ratio = source.width / source.height
    target_width, target_height = min(
        targets,
        key=lambda target: abs(target[0] / target[1] - source_ratio),
    )
    target_ratio = target_width / target_height
    if source_ratio > target_ratio:
        resized_width = target_width
        resized_height = round(target_width / source_ratio)
    else:
        resized_width = round(target_height * source_ratio)
        resized_height = target_height

    resized = source.resize((resized_width, resized_height), Image.Resampling.BICUBIC)
    canvas = Image.new("RGB", (target_width, target_height), "black")
    canvas.paste(resized, ((target_width - resized_width) // 2, (target_height - resized_height) // 2))
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


def _extract_mask_from_alpha(image_bytes: bytes) -> tuple[str, str]:
    """从带 alpha 通道的 PNG 中提取 mask。

    参考 NAI-WorldPainter 的做法：
    - 输入：image 的重绘区为透明（alpha=0），保留区为不透明（alpha=255）
    - 输出：(image_b64, mask_b64)
      - image: 原图，alpha 通道被填充为 255（不透明），RGB 保留
      - mask: 黑白 PNG，重绘区=白色，保留区=黑色

    NAI infill 接口要求 image 是不透明的，mask 是独立的黑白图。
    """
    if Image is None:
        raise HTTPException(status_code=500, detail="Pillow 未安装，无法处理 alpha 通道")

    try:
        src = Image.open(io.BytesIO(image_bytes))
    except UnidentifiedImageError as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片: {e}")

    if src.mode != "RGBA":
        src = src.convert("RGBA")

    w, h = src.size
    # 拆分 RGBA
    r, g, b, a = src.split()
    # mask: alpha < 128 的区域（重绘区）→ 白色；其他 → 黑色
    mask = Image.eval(a, lambda v: 255 if v < 128 else 0)
    # image: 把 alpha 通道填满 255（不透明），RGB 保留
    image_rgb = Image.merge("RGB", (r, g, b))

    # 输出
    img_buf = io.BytesIO()
    image_rgb.save(img_buf, format="PNG")
    image_b64 = base64.b64encode(img_buf.getvalue()).decode("ascii")

    mask_buf = io.BytesIO()
    mask.save(mask_buf, format="PNG")
    mask_b64 = base64.b64encode(mask_buf.getvalue()).decode("ascii")

    return image_b64, mask_b64


# ── 响应构建 ──────────────────────────────────────────────────

# NovelAI V4.5 Anlas 计费常量
# V4.5 (version=3) 公式: per_sample = ceil(2.951823174884865e-6 * pixels + 5.753298233447344e-7 * pixels * steps)
# V4.5 不支持 SMEA, smea_factor = 1.0
# base case: 1024x1024 (1048576 pixels), steps=28 → 20 Anlas
_BASE_ANLAS = 20.0  # 1024x1024, steps=28 的 Anlas 消耗
_BASE_TOKENS = 1000  # base case 对应的 prompt_tokens（NewAPI tiered_expr 基准）


def _calc_anlas_cost(
    width: int,
    height: int,
    steps: int,
    n_samples: int = 1,
    strength: float | None = None,
    uncond_scale: float = 1.0,
    is_opus: bool = False,
    reference_image_count: int = 0,
    reference_mode: str = "vibe",
) -> int:
    """根据 NovelAI V4.5 计费规则计算 Anlas 消耗。

    公式 (version=3, V4/V4.5):
        per_sample = ceil(2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps)
        per_sample = max(ceil(per_sample * strength), 2)  # img2img 时
        per_sample = ceil(per_sample * uncond_scale)       # uncond_scale != 1.0 时
        opus_discount = is_opus && steps <= 28 && r <= 1024*1024
        total = per_sample * (n_samples - int(opus_discount))

    Vibe Transfer 额外计费 (V4+):
        - 每张参考图首次编码: 2 Anlas
        - 超过 4 张后，每增加 1 张额外 2 Anlas
        - 即: ref_cost = 2 * count + max(0, count - 4) * 2 = 2 * count + 2 * max(0, count - 4)

    Precise Reference (Director Reference) 额外计费 (V4.5+):
        - 每张参考图、每个请求样本: 5 Anlas (不走 encode-vibe)
        - 即: ref_cost = 5 * count * n_samples

    Args:
        width: 图像宽度
        height: 图像高度
        steps: 生成步数
        n_samples: 生成数量
        strength: img2img 强度 (None 或 1.0 表示非 img2img)
        uncond_scale: uncond_scale 值 (默认 1.0)
        is_opus: 是否 Opus 会员 (免费额度折扣)
        reference_image_count: Vibe Transfer / Character Reference 参考图数量
        reference_mode: 参考图模式 - "vibe" (默认) 或 "precise"

    Returns:
        Anlas 消耗量 (整数)
    """
    r = max(width * height, 65536)  # 最小 65536

    # V4.5 per_sample 计算；系数来自 NovelAI 官方前端价格计算器。
    per_sample = math.ceil(
        2.951823174884865e-6 * r + 5.753298233447344e-7 * r * steps
    )

    # img2img strength 折扣
    if strength is not None and strength < 1.0:
        per_sample = max(math.ceil(per_sample * strength), 2)

    # uncond_scale 折扣/加价
    if uncond_scale != 1.0:
        per_sample = math.ceil(per_sample * uncond_scale)

    # Opus 免费额度折扣 (第一张免费)
    opus_discount = 1 if (is_opus and steps <= 28 and r <= 1024 * 1024) else 0

    total = per_sample * (n_samples - opus_discount)

    # 参考图额外计费
    if reference_image_count > 0:
        if reference_mode == "precise":
            # Precise Reference: 每张参考图对每个请求样本收取 5 Anlas。
            total += 5 * reference_image_count * n_samples
        else:
            # Vibe Transfer: 每张 2 Anlas，超过 4 张后每张额外 2 Anlas
            ref_cost = 2 * reference_image_count
            if reference_image_count > 4:
                ref_cost += 2 * (reference_image_count - 4)
            total += ref_cost

    return max(total, 0)


def _anlas_to_tokens(anlas: int) -> int:
    """将 Anlas 消耗映射为 prompt_tokens 供 NewAPI tiered_expr 计费。

    base case: 20 Anlas → 1000 prompt_tokens
    tiered_expr: tier("base", p * 4800)
    → 1000 * 4800 / 1M * 500K = 2,400,000 = $4.8

    Args:
        anlas: Anlas 消耗量

    Returns:
        prompt_tokens 值
    """
    return max(1, round(anlas / _BASE_ANLAS * _BASE_TOKENS))


def _build_image_response_v2(
    request: Request,
    content: bytes,
    prompt: str,
    response_format: str,
    nai_content_type: str = "application/zip",
    n_samples: int = 1,
    anlas_cost: int | None = None,
) -> Response:
    """
    统一图像响应构建，支持 4 种返回格式。

    response_format:
      - b64_json / auto / 空: ZIP→PNG→base64→OpenAI JSON
      - nai_json: 透传 NAI JSON 响应
      - raw: 透传 ZIP bytes
      - url: ZIP→PNG→落盘→返回 URL JSON

    所有 JSON 响应都会附带 ``usage`` 字段，供 NewAPI tiered_expr 计费：
    - ``prompt_tokens`` = 根据 Anlas 消耗动态计算（base 17 Anlas → 1000 tokens → $4.8）
    - ``completion_tokens`` = 0
    - ``total_tokens`` = prompt_tokens

    Anlas 消耗由 gateway 根据 NovelAI V4.5 计费公式计算，
    NewAPI tiered_expr 应按 20 Anlas = 1000 prompt tokens 配置。
    """
    # 规范化 response_format
    if not response_format or response_format == "auto":
        response_format = "b64_json"

    # 根据 Anlas 消耗计算 prompt_tokens
    if anlas_cost is not None and anlas_cost > 0:
        prompt_tokens = _anlas_to_tokens(anlas_cost)
    else:
        prompt_tokens = _BASE_TOKENS

    usage = {"prompt_tokens": prompt_tokens, "completion_tokens": 0, "total_tokens": prompt_tokens}

    if response_format == "raw":
        return Response(
            content=content,
            status_code=200,
            media_type="application/zip",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    if response_format == "nai_json":
        # 直接透传 NAI 的 JSON 响应
        return Response(
            content=content,
            status_code=200,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # 对于 b64_json 和 url，需要从上游响应中提取全部 PNG。
    png_images = _extract_pngs_from_response(content)

    if response_format == "url":
        # 落盘并返回 URL
        data = []
        for png_data in png_images:
            filename = f"{uuid.uuid4().hex}.png"
            filepath = settings.image_dir / filename
            filepath.write_bytes(png_data)
            data.append({"url": _get_image_url(request, filename), "revised_prompt": prompt})
        result = {
            "created": int(time.time()),
            "data": data,
            "usage": usage,
        }
        return Response(
            content=json.dumps(result),
            status_code=200,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # b64_json (默认)
    result = {
        "created": int(time.time()),
        "data": [
            {
                "b64_json": base64.b64encode(png_data).decode("ascii"),
                "revised_prompt": prompt,
            }
            for png_data in png_images
        ],
        "usage": usage,
    }
    return Response(
        content=json.dumps(result),
        status_code=200,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def _build_png_image_response(
    png_data: bytes,
    prompt: str,
    anlas_cost: int = 0,
) -> Response:
    """将工具端点的 PNG 结果包装为 OpenAI b64_json 图像响应。

    PNG 字节不会经过重新编码，因此 NovelAI 写入的图片元数据会随 Base64 一并保留。
    """
    prompt_tokens = _anlas_to_tokens(anlas_cost) if anlas_cost > 0 else 0
    result = {
        "created": int(time.time()),
        "data": [{
            "b64_json": base64.b64encode(png_data).decode("ascii"),
            "revised_prompt": prompt,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": 0,
            "total_tokens": prompt_tokens,
        },
    }
    return Response(
        content=json.dumps(result),
        status_code=200,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── NAI 请求发送 ──────────────────────────────────────────────

# NAI multipart 请求中需要提取为二进制 Blob 的 base64 图片字段路径。
# 格式: (payload 中的路径, FormData 中的字段名)
# 对齐 NAI 网页端 tT 函数的逻辑。
_NAI_IMAGE_FIELD_MAP: list[tuple[str, str]] = [
    # 顶层字段
    ("image", "image"),
    ("mask", "mask"),
    # parameters 下的字段
    ("parameters.image", "image"),
    ("parameters.mask", "mask"),
    ("parameters.reference_image", "reference_image"),
]


def _has_base64_image(payload: dict[str, Any]) -> bool:
    """检测 payload 中是否包含 base64 图片数据。

    检查以下字段：
    - payload.image / payload.mask
    - payload.parameters.image / payload.parameters.mask
    - payload.parameters.reference_image
    - payload.parameters.reference_image_multiple (列表)
    - payload.parameters.reference_image_multiple_cached (列表，每项有 data)
    - payload.parameters.director_reference_images_cached (列表，每项有 data)
    """
    if not isinstance(payload, dict):
        return False

    # 顶层 image / mask
    if payload.get("image") or payload.get("mask"):
        return True

    params = payload.get("parameters", {})
    if not isinstance(params, dict):
        return False

    # parameters 下的简单图片字段
    if params.get("image") or params.get("mask") or params.get("reference_image"):
        return True

    # reference_image_multiple (base64 字符串列表)
    ref_multiple = params.get("reference_image_multiple", [])
    if isinstance(ref_multiple, list) and len(ref_multiple) > 0:
        return True

    # reference_image_multiple_cached (每项有 data 字段)
    ref_cached = params.get("reference_image_multiple_cached", [])
    if isinstance(ref_cached, list):
        for item in ref_cached:
            if isinstance(item, dict) and item.get("data"):
                return True

    # director_reference_images_cached
    dir_cached = params.get("director_reference_images_cached", [])
    if isinstance(dir_cached, list):
        for item in dir_cached:
            if isinstance(item, dict) and item.get("data"):
                return True

    return False


def _build_multipart_form(
    payload: dict[str, Any],
) -> tuple[dict[str, str], list[tuple[str, tuple[str, bytes, str]]]]:
    """将包含 base64 图片的 payload 转换为 NAI multipart/form-data 格式。

    对齐 NAI 网页端 tT 函数的逻辑：
    1. 深拷贝 payload
    2. 遍历所有图片字段，base64 解码为二进制
    3. JSON 中的图片字段值替换为 FormData 字段名（字符串）
    4. 返回 headers（不含 Content-Type，由 httpx 自动设置 boundary）和 files 列表

    Returns:
        (json_headers, files)
        - json_headers: 空 dict（Content-Type 由 httpx 自动设置）
        - files: httpx 格式 [(name, (filename, content_bytes, content_type)), ...]
    """
    import copy

    # 深拷贝，避免修改原始 payload
    body = copy.deepcopy(payload)
    # httpx files 格式: [(name, (filename, content, content_type)), ...]
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    # 用于去重：同一 base64 字符串只发送一次，多个引用指向同一字段名
    seen: dict[str, str] = {}

    def _extract_b64(b64_str: str, field_name: str) -> str:
        """将 base64 字符串转为二进制，加入 files 列表。

        如果同一 base64 已出现过，复用之前的字段名（对齐 NAI 网页端的 Map 去重逻辑）。
        返回字段名（用于替换 JSON 中的值）。
        """
        if b64_str in seen:
            return seen[b64_str]
        try:
            raw = base64.b64decode(b64_str)
        except Exception:
            # 如果不是有效 base64，保持原值不处理
            return b64_str
        # httpx 格式: (name, (filename, content, content_type))
        files.append((field_name, (field_name, raw, "image/png")))
        seen[b64_str] = field_name
        return field_name

    # 1. 顶层 image / mask
    if body.get("image"):
        body["image"] = _extract_b64(body["image"], "image")
    if body.get("mask"):
        body["mask"] = _extract_b64(body["mask"], "mask")

    params = body.get("parameters", {})
    if isinstance(params, dict):
        # 2. parameters.image / parameters.mask
        if params.get("image"):
            params["image"] = _extract_b64(params["image"], "image")
        if params.get("mask"):
            params["mask"] = _extract_b64(params["mask"], "mask")

        # 3. parameters.reference_image
        if params.get("reference_image"):
            params["reference_image"] = _extract_b64(params["reference_image"], "reference_image")

        # 4. parameters.reference_image_multiple → 转成 reference_image_multiple_cached 格式
        # 对齐 NAI 网页端 applyCacheTransformation 的逻辑：
        # 每张图生成 cache_secret_key（这里用字段名作为 key），data 保留原始 base64
        # 然后在 tT 函数中 data 会被替换为 FormData 字段名
        ref_multiple = params.get("reference_image_multiple", [])
        if isinstance(ref_multiple, list) and ref_multiple:
            cached_list = []
            for i, b64_str in enumerate(ref_multiple):
                field_name = f"ref_multiple_{i}"
                if isinstance(b64_str, str) and b64_str:
                    # cache_secret_key 用字段名，data 放原始 base64
                    # _extract_b64 会把 data 替换为字段名，并把二进制加入 files
                    cached_list.append({
                        "cache_secret_key": field_name,
                        "data": b64_str,
                    })
                else:
                    cached_list.append({"cache_secret_key": field_name})
            # 用 cached 格式替换，删除原字段
            params["reference_image_multiple_cached"] = cached_list
            del params["reference_image_multiple"]

        # 5. parameters.reference_image_multiple_cached (已有 cached 格式)
        ref_cached = params.get("reference_image_multiple_cached", [])
        if isinstance(ref_cached, list):
            for i, item in enumerate(ref_cached):
                if isinstance(item, dict) and item.get("data"):
                    field_name = f"ref_multiple_{i}"
                    item["data"] = _extract_b64(item["data"], field_name)

        # 6. parameters.director_reference_images_cached
        dir_cached = params.get("director_reference_images_cached", [])
        if isinstance(dir_cached, list):
            for i, item in enumerate(dir_cached):
                if isinstance(item, dict) and item.get("data"):
                    field_name = f"director_ref_{i}"
                    item["data"] = _extract_b64(item["data"], field_name)

    # 7. 把整个 JSON 作为 request 字段
    json_bytes = json.dumps(body).encode("utf-8")
    files.append(("request", ("request", json_bytes, "application/json")))

    return {}, files


async def _encode_vibe(
    request: Request,
    image_b64: str,
    model: str,
    information_extracted: float = 1.0,
) -> str:
    """调用 NAI /ai/encode-vibe 端点编码参考图。

    NAI V4/V4.5 的 Vibe Transfer / Character Reference 不能直接传原始图片 base64，
    必须先通过 encode-vibe 端点编码，然后在生成请求中使用编码后的数据。

    Args:
        request: 原始请求（用于提取 auth）
        image_b64: 参考图的 base64 字符串
        model: 模型名称（如 nai-diffusion-4-5-full）
        information_extracted: 信息提取量 (0.0-1.0)

    Returns:
        编码后的 vibe 数据的 base64 字符串
    """
    from .forwarder import get_client

    token = _get_auth_token(request)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    payload = {
        "image": image_b64,
        "information_extracted": information_extracted,
        "model": model,
    }

    target_url = settings.get_upstream_url("/ai/encode-vibe")

    client = await get_client()
    try:
        resp = await client.post(
            target_url,
            json=payload,
            headers=headers,
            timeout=settings.upstream_timeout,
        )
    except Exception as e:
        logger.error(f"❌ NAI encode-vibe 请求失败: {e}")
        raise HTTPException(status_code=502, detail=f"encode-vibe 上游请求失败: {e}")

    if resp.status_code != 200:
        error_text = resp.content[:500].decode("utf-8", errors="replace")
        logger.error(f"❌ NAI encode-vibe 返回错误 {resp.status_code}: {error_text}")
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"NovelAI encode-vibe error {resp.status_code}: {error_text}",
        )

    # NAI 返回 application/binary，需要 base64 编码后使用
    vibe_b64 = base64.b64encode(resp.content).decode("ascii")
    logger.info(f"✅ encode-vibe 成功: 原图 {len(image_b64)} chars → vibe {len(vibe_b64)} chars")
    return vibe_b64


async def _encode_vibe_batch(
    request: Request,
    images: list[str],
    model: str,
    information_extracted_list: list[float] | None = None,
) -> list[str]:
    """批量编码多张参考图。

    Args:
        request: 原始请求
        images: 参考图 base64 列表
        model: 模型名称
        information_extracted_list: 每张图的信息提取量列表

    Returns:
        编码后的 vibe 数据 base64 列表
    """
    if information_extracted_list is None:
        information_extracted_list = [1.0] * len(images)

    results = []
    for i, img_b64 in enumerate(images):
        ie = information_extracted_list[i] if i < len(information_extracted_list) else 1.0
        vibe_b64 = await _encode_vibe(request, img_b64, model, ie)
        results.append(vibe_b64)
    return results


async def _send_nai_request(
    request: Request,
    payload: dict[str, Any],
    target_url: str | None = None,
    accept_format: str = "zip",
) -> bytes:
    """
    发送请求到 NAI 上游并返回响应 bytes。

    Args:
        request: 原始请求（用于提取 auth）
        payload: 请求体 JSON
        target_url: 上游 URL，默认图像生成端点
        accept_format: "zip" 或 "json"
    """
    from .forwarder import get_client

    if target_url is None:
        target_url = settings.get_upstream_url("/ai/generate-image")

    token = _get_auth_token(request)
    headers = dict(_NAI_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"

    # v4/v4.5 模型只支持 application/json 响应，强制覆盖 Accept
    nai_model = str(payload.get("model", ""))
    if "diffusion-4" in nai_model:
        headers["Accept"] = "application/json"
    elif accept_format == "json":
        headers["Accept"] = "application/json"
    else:
        headers["Accept"] = "application/zip"

    # 统一参数校验（在发送前最后一道关卡）
    params = payload.get("parameters", {}) if isinstance(payload, dict) else {}
    if isinstance(params, dict) and params:
        _validate_image_params(
            width=_safe_int(params.get("width", 1024), 1024),
            height=_safe_int(params.get("height", 1024), 1024),
            steps=_safe_int(params.get("steps", 28), 28),
            n_samples=_safe_int(params.get("n_samples", 1), 1),
            scale=float(params.get("scale", 5.0)),
            strength=params.get("strength"),
            seed=params.get("seed"),
            sampler=params.get("sampler"),
            noise_schedule=params.get("noise_schedule"),
        )

    client = await get_client()

    # 前置检测：如果 payload 包含 base64 图片数据，自动切换到 multipart/form-data
    # 对齐 NAI 网页端的行为：带图片的请求用 multipart，纯文生图用 JSON
    use_multipart = _has_base64_image(payload)

    try:
        if use_multipart:
            # 转换为 multipart 格式
            mp_headers, files = _build_multipart_form(payload)
            # multipart 时不设 Content-Type，由 httpx 自动设置 boundary
            send_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            resp = await client.post(
                target_url,
                files=files,
                headers=send_headers,
                timeout=settings.upstream_timeout,
            )
        else:
            resp = await client.post(
                target_url,
                json=payload,
                headers=headers,
                timeout=settings.upstream_timeout,
            )
    except Exception as e:
        logger.error(f"❌ NAI 请求失败 ({target_url}): {e}")
        raise HTTPException(status_code=502, detail=f"上游请求失败: {e}")

    if resp.status_code != 200:
        error_text = resp.content[:500].decode("utf-8", errors="replace")
        logger.error(f"❌ NAI 返回错误 {resp.status_code}: {error_text}")
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"NovelAI error {resp.status_code}: {error_text}",
        )

    return resp.content


async def _send_nai_binary_request(
    request: Request,
    payload: dict[str, Any],
    target_url: str,
    force_json: bool = False,
) -> bytes:
    """发送请求到 NAI 上游，期望返回二进制内容（图片/音频）。

    Args:
        force_json: 强制使用 JSON 请求（不走 multipart），用于 annotate 等端点。
    """
    from .forwarder import get_client

    token = _get_auth_token(request)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    client = await get_client()

    # 前置检测：如果 payload 包含 base64 图片数据，自动切换到 multipart/form-data
    # 但某些端点（如 annotate-image）需要纯 JSON，通过 force_json 跳过 multipart
    use_multipart = (not force_json) and _has_base64_image(payload)

    try:
        if use_multipart:
            mp_headers, files = _build_multipart_form(payload)
            send_headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}
            resp = await client.post(
                target_url,
                files=files,
                headers=send_headers,
                timeout=settings.upstream_timeout,
            )
        else:
            resp = await client.post(
                target_url,
                json=payload,
                headers=headers,
                timeout=settings.upstream_timeout,
            )
    except Exception as e:
        logger.error(f"❌ NAI 请求失败 ({target_url}): {e}")
        raise HTTPException(status_code=502, detail=f"上游请求失败: {e}")

    if resp.status_code != 200:
        error_text = resp.content[:500].decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"NovelAI error {resp.status_code}: {error_text}",
        )

    return resp.content


async def _send_nai_text_request(
    request: Request,
    payload: dict[str, Any],
    stream: bool = False,
):
    """
    发送文本生成请求到 NAI。

    非流式: POST api.novelai.net/ai/generate → 返回 bytes
    流式: POST api.novelai.net/ai/generate-stream → 返回 httpx.Response (stream)
    """
    from .forwarder import get_client

    token = _get_auth_token(request)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
    }

    if stream:
        headers["Accept"] = "text/event-stream"
        target_url = settings.get_upstream_url("/ai/generate-stream")
    else:
        target_url = settings.get_upstream_url("/ai/generate")

    client = await get_client()

    if stream:
        # 流式请求
        try:
            req = client.build_request(
                method="POST",
                url=target_url,
                json=payload,
                headers=headers,
            )
            resp = await client.send(req, stream=True)
        except Exception as e:
            logger.error(f"❌ NAI 文本流式请求失败: {e}")
            raise HTTPException(status_code=502, detail=f"上游请求失败: {e}")
        if resp.status_code != 200:
            await resp.aread()
            error_text = resp.content[:500].decode("utf-8", errors="replace")
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"NovelAI error {resp.status_code}: {error_text}",
            )
        return resp  # 返回流式 response
    else:
        # 非流式请求
        try:
            resp = await client.post(
                target_url,
                json=payload,
                headers=headers,
                timeout=settings.upstream_timeout,
            )
        except Exception as e:
            logger.error(f"❌ NAI 文本请求失败: {e}")
            raise HTTPException(status_code=502, detail=f"上游请求失败: {e}")

        if resp.status_code != 200:
            error_text = resp.content[:500].decode("utf-8", errors="replace")
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"NovelAI error {resp.status_code}: {error_text}",
            )
        return resp.content


# ── /v1/models ────────────────────────────────────────────────

async def handle_openai_models() -> Response:
    """返回 models.toml 中启用的模型列表。"""
    registry = get_registry()
    if registry:
        models = registry.list_models()
    else:
        models = []

    return Response(
        content=json.dumps({"object": "list", "data": models}),
        status_code=200,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── 图像生成 Payload 构建 ─────────────────────────────────────

def _build_generation_payload(
    body: dict[str, Any],
    operation: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    """
    从 OpenAI 格式请求体构建 NAI 图像生成 payload。

    返回: (nai_payload, prompt, response_format)
    """
    prompt = body.get("prompt", "")
    model = body.get("model", "nai-diffusion-4-5-full")
    negative_prompt = body.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    response_format = body.get("response_format", "b64_json")

    # 通过 registry 解析模型名
    registry = get_registry()
    if registry:
        nai_model = registry.resolve_image_model(model)
    else:
        nai_model = model

    # 尺寸解析
    size_str = body.get("size", "")
    width, height = _parse_size(size_str, 1024, 1024)
    width = body.get("width", width)
    height = body.get("height", height)
    width, height = _clamp_dimensions(_safe_int(width, 1024), _safe_int(height, 1024))

    # 统一参数校验
    _validate_image_params(
        width=width,
        height=height,
        steps=_safe_int(body.get("steps", 28), 28),
        n_samples=_safe_int(body.get("n_samples", body.get("n", 1)), 1),
        scale=float(body.get("scale", body.get("guidance_scale", 5.0))),
        seed=body.get("seed"),
        sampler=body.get("sampler"),
        noise_schedule=body.get("noise_schedule"),
        response_format=response_format,
    )

    # 质量标签
    quality_tags = body.get("quality_tags", QUALITY_TAGS)
    full_prompt = prompt + quality_tags if quality_tags else prompt

    # 构建参数（对齐原项目 tt-P607/novelai-gateway 的默认值）
    # OpenAI API 用 "n" 表示生成数量，NovelAI 用 "n_samples"
    n_samples = _safe_int(body.get("n_samples", body.get("n", 1)), 1)
    params: dict[str, Any] = {
        "width": width,
        "height": height,
        "n_samples": n_samples,
        "seed": body.get("seed", int(time.time()) % 2**32),
        "steps": body.get("steps", 28),
        "scale": body.get("scale", body.get("guidance_scale", 5.0)),
        "sampler": body.get("sampler", "k_euler_ancestral"),
        "negative_prompt": negative_prompt,
        "ucPreset": 0,
        "qualityToggle": True,
        "noise_schedule": body.get("noise_schedule", "karras"),
        "params_version": 3,
    }

    # v4/v4.5 模型不支持 SMEA（sm/sm_dyn），传 True 会导致 NAI 500
    if "diffusion-4" in nai_model:
        params["sm"] = False
        params["sm_dyn"] = False

    # 可选: uncond_scale / skip_cfg_above_sigma
    if "uncond_scale" in body:
        params["uncond_scale"] = body["uncond_scale"]
    if "skip_cfg_above_sigma" in body:
        params["skip_cfg_above_sigma"] = body["skip_cfg_above_sigma"]

    # 可选: reference_image (vibe transfer in generations)
    if "reference_image" in body:
        params["reference_image_multiple"] = [body["reference_image"]]
        params["reference_strength_multiple"] = [body.get("reference_strength", 0.6)]
        params["reference_information_extracted_multiple"] = [body.get("reference_information_extracted", 1.0)]

    # NewAPI 等中转通常只放行 /v1/images/generations。通过透传请求头显式
    # 选择 Precise Reference，避免把普通扩展字段误解为精密参考请求。
    references = body.get("references")
    if operation == "precise-reference":
        if not isinstance(references, list) or not references:
            raise HTTPException(status_code=400, detail="references must be a non-empty list")
        if "reference_image" in body:
            raise HTTPException(
                status_code=400,
                detail="references cannot be combined with reference_image",
            )
        if "diffusion-4" not in nai_model:
            raise HTTPException(
                status_code=400,
                detail="Precise Reference is only available on V4/V4.5 models",
            )

        reference_images: list[str] = []
        reference_strengths: list[float] = []
        reference_fidelities: list[float] = []
        reference_descriptions: list[dict[str, Any]] = []
        valid_types = {"character", "style", "character&style"}
        for index, reference in enumerate(references):
            if not isinstance(reference, dict):
                raise HTTPException(status_code=400, detail=f"references[{index}] must be an object")
            image = reference.get("reference_image")
            if not isinstance(image, str) or not image:
                raise HTTPException(
                    status_code=400,
                    detail=f"references[{index}] missing reference_image",
                )
            reference_type = reference.get("reference_type", "character&style")
            if reference_type not in valid_types:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"references[{index}].reference_type must be one of: "
                        f"{', '.join(sorted(valid_types))}"
                    ),
                )
            strength = float(reference.get("strength", 1.0))
            fidelity = float(reference.get("fidelity", 1.0))
            if not 0 <= strength <= 1 or not 0 <= fidelity <= 1:
                raise HTTPException(
                    status_code=400,
                    detail=f"references[{index}] strength and fidelity must be between 0 and 1",
                )
            reference_images.append(_normalize_precise_reference_image(image))
            reference_strengths.append(strength)
            reference_fidelities.append(fidelity)
            reference_descriptions.append({
                "caption": {"base_caption": reference_type, "char_captions": []},
                "legacy_uc": False,
            })

        params.update({
            "director_reference_images": reference_images,
            "director_reference_descriptions": reference_descriptions,
            "director_reference_information_extracted": reference_fidelities,
            "director_reference_strength_values": reference_strengths,
            "director_reference_secondary_strength_values": [
                1.0 - fidelity for fidelity in reference_fidelities
            ],
        })
    elif references is not None:
        raise HTTPException(
            status_code=400,
            detail="references requires X-NovelAI-Operation: precise-reference",
        )

    # 可选: controlnet
    if "controlnet_condition" in body:
        params["controlnet_condition"] = body["controlnet_condition"]
        params["controlnet_model"] = body.get("controlnet_model", "hed")
        params["controlnet_strength"] = body.get("controlnet_strength", 1.0)

    # V4 角色位置坐标
    if "characterPrompts" in body:
        params["characterPrompts"] = body["characterPrompts"]
    if "v4_prompt" in body:
        params["v4_prompt"] = body["v4_prompt"]
    if "v4_negative_prompt" in body:
        params["v4_negative_prompt"] = body["v4_negative_prompt"]
    if "use_coords" in body:
        params["use_coords"] = body["use_coords"]

    # 便捷功能：如果用户传了 characterPrompts 但没传 v4_prompt，
    # 自动从 characterPrompts 构造 v4_prompt 的 char_captions
    # characterPrompts 格式: [{"prompt": "1girl, ...", "center": {"x": 0.3, "y": 0.4}}, ...]
    if "characterPrompts" in body and "v4_prompt" not in params:
        char_prompts = body["characterPrompts"]
        if isinstance(char_prompts, list) and char_prompts:
            char_captions = []
            for cp in char_prompts:
                if not isinstance(cp, dict):
                    continue
                center = cp.get("center", cp.get("centers", {}))
                centers_list = []
                if isinstance(center, dict):
                    centers_list = [{"x": center.get("x", 0.5), "y": center.get("y", 0.5)}]
                elif isinstance(center, list):
                    centers_list = center
                char_captions.append({
                    "char_caption": cp.get("prompt", cp.get("char_caption", "")),
                    "centers": centers_list,
                })
            if char_captions:
                params["v4_prompt"] = {
                    "caption": {
                        "base_caption": full_prompt,
                        "char_captions": char_captions,
                    },
                    "use_coords": params.get("use_coords", True),
                    "use_order": True,
                }

    action = body.get("action", "generate")

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": action,
        "parameters": params,
    }

    # debug: 打印客户端传来的采样参数和最终 NAI payload 的 sampler/noise_schedule
    logger.info(
        f"[generations] client body sampler={body.get('sampler')!r} "
        f"noise_schedule={body.get('noise_schedule')!r} | "
        f"final NAI sampler={params.get('sampler')!r} "
        f"noise_schedule={params.get('noise_schedule')!r}"
    )

    # 可选: service_tier（priority 优先调度，会扣 Anlas；仅原版模型生效，-limit 模型已在入口拒绝）
    if "service_tier" in body:
        nai_payload["service_tier"] = body["service_tier"]

    # v4/v4.5 模型必须带 v4_prompt 结构，否则 NAI 返回 500
    # 如果用户没传，自动用 prompt/negative_prompt 构造一个空的 base_caption
    is_v4_model = "diffusion-4" in nai_model
    if is_v4_model and action == "generate":
        # v4/v4.5 不支持 SMEA，强制关闭
        params["sm"] = False
        params["sm_dyn"] = False
        if "v4_prompt" not in params:
            params["v4_prompt"] = {
                "caption": {
                    "base_caption": full_prompt,
                    "char_captions": [],
                },
                # use_coords=True 对齐原项目，避免噪点/偏色
                "use_coords": params.get("use_coords", True),
                "use_order": True,
            }
        if "v4_negative_prompt" not in params:
            # 从 v4_prompt 的 char_captions 中提取角色数量和 centers，
            # 确保 v4_negative_prompt 的 char_captions 与 v4_prompt 一一对应，
            # 否则 NAI 会返回 500（char_captions 数量/结构不匹配）
            v4_char_captions = (
                params.get("v4_prompt", {})
                .get("caption", {})
                .get("char_captions", [])
            )
            neg_char_captions = [
                {"char_caption": "", "centers": cc.get("centers", [])}
                for cc in v4_char_captions
            ]
            params["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                "legacy_uc": False,
            }
        # v4 必需的字段
        params.setdefault("legacy", False)
        params.setdefault("legacy_v3_extend", False)
        params.setdefault("legacy_uc", False)
        params.setdefault("add_original_image", True)
        params.setdefault("controlnet_strength", 1)
        params.setdefault("dynamic_thresholding", False)
        params.setdefault("prefer_brownian", True)
        params.setdefault("normalize_reference_strength_multiple", True)
        params.setdefault("use_coords", True)
        params.setdefault("inpaintImg2ImgStrength", 1)
        params.setdefault("deliberate_euler_ancestral_bug", False)
        params.setdefault("skip_cfg_above_sigma", None)
        params.setdefault("characterPrompts", [])

    return nai_payload, prompt, response_format


# ── /v1/images/generations ────────────────────────────────────

async def handle_openai_generations(request: Request) -> Response:
    """处理 OpenAI 兼容的图像生成请求。

    支持从请求 body 和 HTTP Header 读取采样参数。
    NewAPI 等中转站可能过滤 body 里的非标准 OpenAI 字段（sampler/noise_schedule/scale 等），
    因此同时支持从 Header 读取（X-Sampler / X-Noise-Schedule / X-Scale / X-Steps），
    Header 优先级高于 body。
    """
    body = await request.json()

    # 从 Header 读取采样参数（NewAPI 中转兼容）
    # Header 优先级高于 body，这样用户可以通过 Header 强制指定参数
    # 注意：Header 值都是字符串，需要按目标类型转换
    _header_str_fields = [
        ("x-sampler", "sampler"),
        ("x-noise-schedule", "noise_schedule"),
        ("x-negative-prompt", "negative_prompt"),
        ("x-service-tier", "service_tier"),
    ]
    _header_int_fields = [
        ("x-steps", "steps"),
        ("x-seed", "seed"),
        ("x-n-samples", "n_samples"),
    ]
    _header_float_fields = [
        ("x-scale", "scale"),
    ]
    for header_name, body_key in _header_str_fields:
        header_val = request.headers.get(header_name)
        if header_val is not None:
            body[body_key] = header_val
    for header_name, body_key in _header_int_fields:
        header_val = request.headers.get(header_name)
        if header_val is not None:
            try:
                body[body_key] = int(header_val)
            except ValueError:
                pass
    for header_name, body_key in _header_float_fields:
        header_val = request.headers.get(header_name)
        if header_val is not None:
            try:
                body[body_key] = float(header_val)
            except ValueError:
                pass

    header_operation = request.headers.get("x-novelai-operation", "").strip().lower()
    body_operation = body.pop("novelai_operation", "")
    if not isinstance(body_operation, str):
        raise HTTPException(
            status_code=400,
            detail="novelai_operation must be a string",
        )
    operation = body_operation.strip().lower()
    if header_operation and operation and header_operation != operation:
        raise HTTPException(
            status_code=400,
            detail=(
                "X-NovelAI-Operation conflicts with novelai_operation; "
                "provide only one operation value"
            ),
        )
    if not operation:
        operation = header_operation
    supported_operations = {
        "", "generate", "precise-reference", "img2img", "inpainting", "edits",
        "vibe-transfer", "character-reference", "upscale", "annotate",
        "director-declutter", "director-bg-remover", "director-lineart", "director-sketch",
        "director-colorize", "director-emotion",
    }
    if operation not in supported_operations:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported novelai_operation. See API_REQUEST_DOC.md for supported values."
            ),
        )
    if operation not in {"", "generate", "precise-reference"}:
        if operation in {"upscale", "annotate"}:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"novelai_operation='{operation}' is not available through "
                    "/v1/images/generations because its NewAPI billing mapping is "
                    "not configured; call its dedicated Gateway endpoint instead"
                ),
            )
        return await _dispatch_image_operation(request, body, operation)

    # -limit 模型校验：只允许走 Opus 免费额度的文生图路径
    _enforce_limit_model(body.get("model", ""), body)
    if operation == "precise-reference":
        _reject_limit_model_for_paid_endpoint(body.get("model", ""))
        body["response_format"] = "b64_json"
    nai_payload, prompt, response_format = _build_generation_payload(body, operation)

    # V4/V4.5 模型带参考图时，需要先通过 encode-vibe 编码
    nai_model = str(nai_payload.get("model", ""))
    params = nai_payload.get("parameters", {})
    if "diffusion-4" in nai_model and isinstance(params, dict):
        ref_multiple = params.get("reference_image_multiple", [])
        if isinstance(ref_multiple, list) and ref_multiple:
            ref_infos = params.get("reference_information_extracted_multiple", [1.0] * len(ref_multiple))
            encoded_vibes = await _encode_vibe_batch(request, ref_multiple, nai_model, ref_infos)
            params["reference_image_multiple"] = encoded_vibes

    # 确定 accept_format
    accept_format = "json" if response_format == "nai_json" else "zip"

    # 走排队门控
    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    # 记录统计
    params = nai_payload.get("parameters", {})
    width = params.get("width", 0)
    height = params.get("height", 0)
    record_generation(content, "/ai/generate-image", width, height)

    # 计算 Anlas 消耗 (action=generate, 无 strength 折扣)
    # Vibe 参考图会产生编码费用；Precise Reference 按参考数与样本数计费。
    precise_ref_count = len(params.get("director_reference_images", []))
    ref_count = len(params.get("reference_image_multiple", []))
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=params.get("n_samples", 1),
        uncond_scale=params.get("uncond_scale", 1.0),
        is_opus=is_limit_model(body.get("model", "")),
        reference_image_count=precise_ref_count or ref_count,
        reference_mode="precise" if precise_ref_count else "vibe",
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/inpainting (NAI SDK 格式) ──────────────────────

async def handle_nai_inpainting(request: Request) -> Response:
    """处理 NAI SDK 风格的局部重绘请求。"""
    body = await request.json()

    prompt = body.get("prompt", body.get("input", ""))
    model = body.get("model", "nai-diffusion-4-5-full-inpainting")
    _enforce_limit_model(model, {**body, "action": "infill"})
    negative_prompt = body.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    response_format = body.get("response_format", "b64_json")
    image = body.get("image", "")
    mask = body.get("mask", "")

    if not image or not mask:
        raise HTTPException(status_code=400, detail="image and mask are required")

    # 通过 registry 解析模型名
    registry = get_registry()
    if registry:
        nai_model = registry.resolve_image_model(model)
    else:
        nai_model = model

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)
    width, height = _clamp_dimensions(width, height)

    quality_tags = body.get("quality_tags", QUALITY_TAGS)
    full_prompt = prompt + quality_tags if quality_tags else prompt

    params = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": body.get("seed", int(time.time()) % 2**32),
        "steps": body.get("steps", 28),
        "scale": body.get("scale", 5.0),
        "sampler": body.get("sampler", "k_euler_ancestral"),
        "negative_prompt": negative_prompt,
        "sm": body.get("sm", True),
        "sm_dyn": body.get("sm_dyn", False),
        "noise_schedule": body.get("noise_schedule", "karras"),
        "image": image,
        "mask": mask,
        "add_original_image": body.get("add_original_image", True),
        "cfg_rescale": body.get("cfg_rescale", 0.0),
        "noise": body.get("noise", 0.0),
        "strength": body.get("strength", 0.7),
    }

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": "infill",
        "parameters": params,
    }

    # v4/v4.5 inpaint 模型也需要 v4_prompt 结构
    is_v4_model = "diffusion-4" in nai_model
    if is_v4_model:
        # v4/v4.5 不支持 SMEA，强制关闭
        params["sm"] = False
        params["sm_dyn"] = False
        if "v4_prompt" not in params:
            params["v4_prompt"] = {
                "caption": {"base_caption": full_prompt, "char_captions": []},
                "use_coords": params.get("use_coords", False),
                "use_order": True,
            }
        if "v4_negative_prompt" not in params:
            # 从 v4_prompt 的 char_captions 中提取角色数量和 centers，
            # 确保 v4_negative_prompt 的 char_captions 与 v4_prompt 一一对应
            v4_char_captions = (
                params.get("v4_prompt", {})
                .get("caption", {})
                .get("char_captions", [])
            )
            neg_char_captions = [
                {"char_caption": "", "centers": cc.get("centers", [])}
                for cc in v4_char_captions
            ]
            params["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                "legacy_uc": False,
            }
        params.setdefault("params_version", 3)
        params.setdefault("legacy", False)
        params.setdefault("legacy_v3_extend", False)
        params.setdefault("legacy_uc", False)
        params.setdefault("add_original_image", True)
        params.setdefault("controlnet_strength", 1)
        params.setdefault("dynamic_thresholding", False)
        params.setdefault("prefer_brownian", True)
        params.setdefault("normalize_reference_strength_multiple", True)
        params.setdefault("use_coords", False)
        params.setdefault("inpaintImg2ImgStrength", 1)
        params.setdefault("deliberate_euler_ancestral_bug", False)
        params.setdefault("skip_cfg_above_sigma", None)
        params.setdefault("characterPrompts", [])

    accept_format = "json" if response_format == "nai_json" else "zip"

    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    record_generation(content, "/ai/generate-image", width, height)

    # 计算 Anlas 消耗 (action=infill, 有 strength 折扣)
    # inpainting 也可能带 reference_image_multiple
    ref_count = len(params.get("reference_image_multiple", []))
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=1,
        strength=params.get("inpaintImg2ImgStrength", 1.0),
        uncond_scale=params.get("uncond_scale", 1.0),
        is_opus=is_limit_model(model),
        reference_image_count=ref_count,
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/edits (OpenAI 兼容格式) ────────────────────────

async def handle_openai_image_edits(request: Request) -> Response:
    """处理 OpenAI 兼容格式的图像编辑（局部重绘）请求。支持 multipart/form-data。"""
    content_type = request.headers.get("content-type", "")

    if "multipart/form-data" in content_type:
        form = await request.form()
        prompt = form.get("prompt", "")
        model = form.get("model", "nai-diffusion-4-5-full-inpainting")
        negative_prompt = form.get("negative_prompt", "") or DEFAULT_NEGATIVE_PROMPT
        response_format = form.get("response_format", "b64_json")
        size_str = form.get("size", "1024x1024")
        width, height = _parse_size(size_str)
        width, height = _clamp_dimensions(width, height)

        # 读取图片文件
        image_file = form.get("image")
        mask_file = form.get("mask")

        if not image_file:
            raise HTTPException(status_code=400, detail="image file is required")

        if isinstance(image_file, StarletteUploadFile):
            image_bytes = await image_file.read()
        else:
            image_bytes = image_file.encode() if isinstance(image_file, str) else bytes(image_file)

        if mask_file:
            if isinstance(mask_file, StarletteUploadFile):
                mask_bytes = await mask_file.read()
            else:
                mask_bytes = mask_file.encode() if isinstance(mask_file, str) else bytes(mask_file)
            mask_b64 = base64.b64encode(mask_bytes).decode("ascii")
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        else:
            # 省略 mask：从 image alpha 通道提取 mask（透明=重绘）
            image_b64, mask_b64 = _extract_mask_from_alpha(image_bytes)
        await form.close()
    else:
        body = await request.json()
        prompt = body.get("prompt", "")
        model = body.get("model", "nai-diffusion-4-5-full-inpainting")
        _enforce_limit_model(model, {**body, "action": "infill"})
        negative_prompt = body.get("negative_prompt", "") or DEFAULT_NEGATIVE_PROMPT
        response_format = body.get("response_format", "b64_json")
        size_str = body.get("size", "1024x1024")
        width, height = _parse_size(size_str)
        width = body.get("width", width)
        height = body.get("height", height)
        width, height = _clamp_dimensions(_safe_int(width, 1024), _safe_int(height, 1024))
        image_b64 = body.get("image", "")
        mask_b64 = body.get("mask", "")

        if not image_b64:
            raise HTTPException(status_code=400, detail="image is required")

        if not mask_b64:
            # 省略 mask：从 image alpha 通道提取
            try:
                image_bytes = base64.b64decode(_strip_data_prefix(image_b64))
                image_b64_clean, mask_b64 = _extract_mask_from_alpha(image_bytes)
                image_b64 = image_b64_clean
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"从 image alpha 通道提取 mask 失败: {e}")

    # 通过 registry 解析模型名
    registry = get_registry()
    if registry:
        nai_model = registry.resolve_image_model(model)
    else:
        nai_model = model

    quality_tags = QUALITY_TAGS
    full_prompt = prompt + quality_tags if quality_tags else prompt

    params = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": int(time.time()) % 2**32,
        "steps": 28,
        "scale": 5.0,
        "sampler": "k_euler_ancestral",
        "negative_prompt": negative_prompt,
        "sm": True,
        "sm_dyn": False,
        "noise_schedule": "karras",
        "image": image_b64,
        "mask": mask_b64,
        "add_original_image": True,
        "cfg_rescale": 0.0,
        "noise": 0.0,
        "strength": 0.7,
    }

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": "infill",
        "parameters": params,
    }

    # v4/v4.5 inpaint 模型也需要 v4_prompt 结构
    is_v4_model = "diffusion-4" in nai_model
    if is_v4_model:
        # v4/v4.5 不支持 SMEA，强制关闭
        params["sm"] = False
        params["sm_dyn"] = False
        if "v4_prompt" not in params:
            params["v4_prompt"] = {
                "caption": {"base_caption": full_prompt, "char_captions": []},
                "use_coords": params.get("use_coords", False),
                "use_order": True,
            }
        if "v4_negative_prompt" not in params:
            # 从 v4_prompt 的 char_captions 中提取角色数量和 centers，
            # 确保 v4_negative_prompt 的 char_captions 与 v4_prompt 一一对应
            v4_char_captions = (
                params.get("v4_prompt", {})
                .get("caption", {})
                .get("char_captions", [])
            )
            neg_char_captions = [
                {"char_caption": "", "centers": cc.get("centers", [])}
                for cc in v4_char_captions
            ]
            params["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                "legacy_uc": False,
            }
        params.setdefault("params_version", 3)
        params.setdefault("legacy", False)
        params.setdefault("legacy_v3_extend", False)
        params.setdefault("legacy_uc", False)
        params.setdefault("add_original_image", True)
        params.setdefault("controlnet_strength", 1)
        params.setdefault("dynamic_thresholding", False)
        params.setdefault("prefer_brownian", True)
        params.setdefault("normalize_reference_strength_multiple", True)
        params.setdefault("use_coords", False)
        params.setdefault("inpaintImg2ImgStrength", 1)
        params.setdefault("deliberate_euler_ancestral_bug", False)
        params.setdefault("skip_cfg_above_sigma", None)
        params.setdefault("characterPrompts", [])

    accept_format = "json" if response_format == "nai_json" else "zip"

    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    record_generation(content, "/ai/generate-image", width, height)

    # 计算 Anlas 消耗 (action=infill, 有 strength 折扣)
    # image edits 也可能带 reference_image_multiple
    ref_count = len(params.get("reference_image_multiple", []))
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=1,
        strength=params.get("inpaintImg2ImgStrength", 1.0),
        uncond_scale=params.get("uncond_scale", 1.0),
        is_opus=is_limit_model(model),
        reference_image_count=ref_count,
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/img2img ────────────────────────────────────────

async def handle_img2img(request: Request) -> Response:
    """处理图生图请求 (action=img2img)。"""
    body = await request.json()

    prompt = body.get("prompt", "")
    model = body.get("model", "nai-diffusion-4-5-full")
    _enforce_limit_model(model, {**body, "action": "img2img"})
    negative_prompt = body.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    response_format = body.get("response_format", "b64_json")
    image = body.get("image", "")

    if not image:
        raise HTTPException(status_code=400, detail="image (base64) is required")

    registry = get_registry()
    nai_model = registry.resolve_image_model(model) if registry else model

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)
    width, height = _clamp_dimensions(width, height)

    quality_tags = body.get("quality_tags", QUALITY_TAGS)
    full_prompt = prompt + quality_tags if quality_tags else prompt

    params = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": body.get("seed", int(time.time()) % 2**32),
        "steps": body.get("steps", 28),
        "scale": body.get("scale", 5.0),
        "sampler": body.get("sampler", "k_euler_ancestral"),
        "negative_prompt": negative_prompt,
        "sm": body.get("sm", True),
        "sm_dyn": body.get("sm_dyn", False),
        "noise_schedule": body.get("noise_schedule", "karras"),
        "image": image,
        "strength": body.get("strength", 0.7),
        "noise": body.get("noise", 0.0),
        "cfg_rescale": body.get("cfg_rescale", 0.0),
        "extra_noise_seed": body.get("extra_noise_seed", body.get("seed", int(time.time()) % 2**32)),
    }

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": "img2img",
        "parameters": params,
    }

    # v4/v4.5 模型 img2img 也需要 v4_prompt 结构
    is_v4_model = "diffusion-4" in nai_model
    if is_v4_model:
        # v4/v4.5 不支持 SMEA，强制关闭
        params["sm"] = False
        params["sm_dyn"] = False
        if "v4_prompt" not in params:
            params["v4_prompt"] = {
                "caption": {"base_caption": full_prompt, "char_captions": []},
                "use_coords": params.get("use_coords", False),
                "use_order": True,
            }
        if "v4_negative_prompt" not in params:
            # 从 v4_prompt 的 char_captions 中提取角色数量和 centers，
            # 确保 v4_negative_prompt 的 char_captions 与 v4_prompt 一一对应
            v4_char_captions = (
                params.get("v4_prompt", {})
                .get("caption", {})
                .get("char_captions", [])
            )
            neg_char_captions = [
                {"char_caption": "", "centers": cc.get("centers", [])}
                for cc in v4_char_captions
            ]
            params["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                "legacy_uc": False,
            }
        params.setdefault("params_version", 3)
        params.setdefault("legacy", False)
        params.setdefault("legacy_v3_extend", False)
        params.setdefault("legacy_uc", False)
        params.setdefault("add_original_image", True)
        params.setdefault("controlnet_strength", 1)
        params.setdefault("dynamic_thresholding", False)
        params.setdefault("prefer_brownian", True)
        params.setdefault("normalize_reference_strength_multiple", True)
        params.setdefault("use_coords", False)
        params.setdefault("inpaintImg2ImgStrength", 1)
        params.setdefault("deliberate_euler_ancestral_bug", False)
        params.setdefault("skip_cfg_above_sigma", None)
        params.setdefault("characterPrompts", [])

    accept_format = "json" if response_format == "nai_json" else "zip"

    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    record_generation(content, "/ai/generate-image", width, height)

    # 计算 Anlas 消耗 (action=img2img, 有 strength 折扣)
    # img2img 也可能带 reference_image_multiple
    ref_count = len(params.get("reference_image_multiple", []))
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=1,
        strength=params.get("strength", 0.7),
        uncond_scale=params.get("uncond_scale", 1.0),
        is_opus=is_limit_model(model),
        reference_image_count=ref_count,
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/vibe-transfer ──────────────────────────────────

async def handle_vibe_transfer(request: Request) -> Response:
    """处理 Vibe Transfer 风格迁移请求。"""
    body = await request.json()

    prompt = body.get("prompt", "")
    model = body.get("model", "nai-diffusion-4-5-full")
    _reject_limit_model_for_paid_endpoint(model)
    negative_prompt = body.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    response_format = body.get("response_format", "b64_json")

    # reference_image 可以是单个字符串或列表
    ref_images = body.get("reference_image", body.get("reference_images", []))
    if isinstance(ref_images, str):
        ref_images = [ref_images]
    if not ref_images:
        raise HTTPException(status_code=400, detail="At least one reference_image is required")

    ref_strength = body.get("reference_strength", 0.6)
    ref_info_extracted = body.get("reference_information_extracted", 1.0)

    # 如果是列表形式的 strength/info
    ref_strengths = body.get("reference_strength_multiple", [ref_strength] * len(ref_images))
    ref_infos = body.get("reference_information_extracted_multiple", [ref_info_extracted] * len(ref_images))

    registry = get_registry()
    nai_model = registry.resolve_image_model(model) if registry else model

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)
    width, height = _clamp_dimensions(width, height)

    quality_tags = body.get("quality_tags", QUALITY_TAGS)
    full_prompt = prompt + quality_tags if quality_tags else prompt

    # V4/V4.5 模型需要先通过 encode-vibe 编码参考图
    is_v4_model = "diffusion-4" in nai_model
    if is_v4_model:
        encoded_vibes = await _encode_vibe_batch(request, ref_images, nai_model, ref_infos)
        ref_images = encoded_vibes

    params = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": body.get("seed", int(time.time()) % 2**32),
        "steps": body.get("steps", 28),
        "scale": body.get("scale", 5.0),
        "sampler": body.get("sampler", "k_euler_ancestral"),
        "negative_prompt": negative_prompt,
        "sm": body.get("sm", True),
        "sm_dyn": body.get("sm_dyn", False),
        "noise_schedule": body.get("noise_schedule", "karras"),
        "cfg_rescale": body.get("cfg_rescale", 0.0),
        "reference_image_multiple": ref_images,
        "reference_strength_multiple": ref_strengths,
        "reference_information_extracted_multiple": ref_infos,
    }

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": "generate",
        "parameters": params,
    }

    # v4/v4.5 模型 vibe-transfer 也需要 v4_prompt 结构
    if is_v4_model:
        params["sm"] = False
        params["sm_dyn"] = False
        if "v4_prompt" not in params:
            params["v4_prompt"] = {
                "caption": {"base_caption": full_prompt, "char_captions": []},
                "use_coords": params.get("use_coords", False),
                "use_order": True,
            }
        if "v4_negative_prompt" not in params:
            # 从 v4_prompt 的 char_captions 中提取角色数量和 centers，
            # 确保 v4_negative_prompt 的 char_captions 与 v4_prompt 一一对应
            v4_char_captions = (
                params.get("v4_prompt", {})
                .get("caption", {})
                .get("char_captions", [])
            )
            neg_char_captions = [
                {"char_caption": "", "centers": cc.get("centers", [])}
                for cc in v4_char_captions
            ]
            params["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                "legacy_uc": False,
            }
        params.setdefault("params_version", 3)
        params.setdefault("legacy", False)
        params.setdefault("legacy_v3_extend", False)
        params.setdefault("legacy_uc", False)
        params.setdefault("add_original_image", True)
        params.setdefault("controlnet_strength", 1)
        params.setdefault("dynamic_thresholding", False)
        params.setdefault("prefer_brownian", True)
        params.setdefault("normalize_reference_strength_multiple", True)
        params.setdefault("use_coords", False)
        params.setdefault("inpaintImg2ImgStrength", 1)
        params.setdefault("deliberate_euler_ancestral_bug", False)
        params.setdefault("skip_cfg_above_sigma", None)
        params.setdefault("characterPrompts", [])

    accept_format = "json" if response_format == "nai_json" else "zip"

    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    record_generation(content, "/ai/generate-image", width, height)

    # 计算 Anlas 消耗 (action=generate + Vibe Transfer 参考图编码费用)
    ref_count = len(ref_images)
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=1,
        uncond_scale=params.get("uncond_scale", 1.0),
        reference_image_count=ref_count,
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/character-reference ───────────────────────────

async def handle_character_reference(request: Request) -> Response:
    """处理角色精确参考 (Character Reference / Precise Reference) 请求。

    这是 Vibe Transfer 的高级用法：每张参考图对应一个角色，
    配合 v4_prompt.char_captions 指定角色在画面中的位置。

    请求体格式:
        {
            "prompt": "1girl, 1boy, ...",
            "model": "nai-diffusion-4-5-full",
            "negative_prompt": "lowres, ...",
            "width": 832,
            "height": 1216,
            "steps": 28,
            "scale": 6.0,
            "sampler": "k_dpmpp_2m",
            "noise_schedule": "karras",
            "seed": 12345,
            "response_format": "b64_json",
            "characters": [
                {
                    "reference_image": "<base64>",
                    "prompt": "1girl, red hair, blue eyes, ...",
                    "center": {"x": 0.3, "y": 0.4},
                    "reference_strength": 0.6,
                    "reference_information_extracted": 1.0
                },
                {
                    "reference_image": "<base64>",
                    "prompt": "1boy, black hair, ...",
                    "center": {"x": 0.7, "y": 0.5},
                    "reference_strength": 0.6,
                    "reference_information_extracted": 1.0
                }
            ]
        }

    也支持直接传 v4_prompt / v4_negative_prompt 进行高级控制。
    """
    body = await request.json()

    prompt = body.get("prompt", "")
    model = body.get("model", "nai-diffusion-4-5-full")
    _reject_limit_model_for_paid_endpoint(model)
    negative_prompt = body.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    response_format = body.get("response_format", "b64_json")

    # characters 列表（每项包含参考图、角色提示词、位置）
    characters = body.get("characters", [])
    if not characters:
        raise HTTPException(status_code=400, detail="At least one character with reference_image is required")

    # 提取参考图和参数
    ref_images = []
    ref_strengths = []
    ref_infos = []
    char_captions = []

    for i, char in enumerate(characters):
        if not isinstance(char, dict):
            continue
        ref_img = char.get("reference_image", "")
        if not ref_img:
            raise HTTPException(status_code=400, detail=f"characters[{i}] missing reference_image")

        ref_images.append(ref_img)
        ref_strengths.append(char.get("reference_strength", 0.6))
        ref_infos.append(char.get("reference_information_extracted", 1.0))

        # 构造 char_caption
        char_prompt = char.get("prompt", char.get("char_caption", ""))
        center = char.get("center", char.get("centers", {}))
        centers_list = []
        if isinstance(center, dict):
            centers_list = [{"x": center.get("x", 0.5), "y": center.get("y", 0.5)}]
        elif isinstance(center, list):
            centers_list = center

        char_captions.append({
            "char_caption": char_prompt,
            "centers": centers_list,
        })

    if not ref_images:
        raise HTTPException(status_code=400, detail="No valid character reference images found")

    registry = get_registry()
    nai_model = registry.resolve_image_model(model) if registry else model

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)
    width, height = _clamp_dimensions(width, height)

    quality_tags = body.get("quality_tags", QUALITY_TAGS)
    full_prompt = prompt + quality_tags if quality_tags else prompt

    # V4/V4.5 模型需要先通过 encode-vibe 编码参考图
    is_v4_model = "diffusion-4" in nai_model
    if is_v4_model:
        encoded_vibes = await _encode_vibe_batch(request, ref_images, nai_model, ref_infos)
        ref_images = encoded_vibes

    params: dict[str, Any] = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": body.get("seed", int(time.time()) % 2**32),
        "steps": body.get("steps", 28),
        "scale": body.get("scale", 5.0),
        "sampler": body.get("sampler", "k_euler_ancestral"),
        "negative_prompt": negative_prompt,
        "sm": body.get("sm", True),
        "sm_dyn": body.get("sm_dyn", False),
        "noise_schedule": body.get("noise_schedule", "karras"),
        "cfg_rescale": body.get("cfg_rescale", 0.0),
        "reference_image_multiple": ref_images,
        "reference_strength_multiple": ref_strengths,
        "reference_information_extracted_multiple": ref_infos,
    }

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": "generate",
        "parameters": params,
    }

    # v4/v4.5 模型必须带 v4_prompt 结构
    if is_v4_model:
        params["sm"] = False
        params["sm_dyn"] = False

        # 如果用户传了 v4_prompt，优先使用用户的
        if "v4_prompt" in body:
            params["v4_prompt"] = body["v4_prompt"]
        elif char_captions:
            # 自动从 characters 构造 v4_prompt
            params["v4_prompt"] = {
                "caption": {
                    "base_caption": full_prompt,
                    "char_captions": char_captions,
                },
                "use_coords": body.get("use_coords", True),
                "use_order": True,
            }
        else:
            params["v4_prompt"] = {
                "caption": {"base_caption": full_prompt, "char_captions": []},
                "use_coords": body.get("use_coords", False),
                "use_order": True,
            }

        # v4_negative_prompt
        if "v4_negative_prompt" in body:
            params["v4_negative_prompt"] = body["v4_negative_prompt"]
        else:
            v4_char_captions = (
                params.get("v4_prompt", {})
                .get("caption", {})
                .get("char_captions", [])
            )
            neg_char_captions = [
                {"char_caption": "", "centers": cc.get("centers", [])}
                for cc in v4_char_captions
            ]
            params["v4_negative_prompt"] = {
                "caption": {"base_caption": negative_prompt, "char_captions": neg_char_captions},
                "legacy_uc": False,
            }

        params.setdefault("params_version", 3)
        params.setdefault("legacy", False)
        params.setdefault("legacy_v3_extend", False)
        params.setdefault("legacy_uc", False)
        params.setdefault("add_original_image", True)
        params.setdefault("controlnet_strength", 1)
        params.setdefault("dynamic_thresholding", False)
        params.setdefault("prefer_brownian", True)
        params.setdefault("normalize_reference_strength_multiple", True)
        params.setdefault("use_coords", True)
        params.setdefault("inpaintImg2ImgStrength", 1)
        params.setdefault("deliberate_euler_ancestral_bug", False)
        params.setdefault("skip_cfg_above_sigma", None)
        params.setdefault("characterPrompts", [])

    accept_format = "json" if response_format == "nai_json" else "zip"

    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    record_generation(content, "/ai/generate-image", width, height)

    # 计算 Anlas 消耗 (生成费用 + 角色参考图编码费用)
    ref_count = len(ref_images)
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=1,
        uncond_scale=params.get("uncond_scale", 1.0),
        reference_image_count=ref_count,
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/precise-reference ──────────────────────────────

async def handle_precise_reference(request: Request) -> Response:
    """处理 Precise Reference（Director Reference）请求。

    这是 NovelAI V4.5 的 Precise Reference 功能，与 Vibe Transfer 不同：
    - 不走 encode-vibe 编码，直接传原图
    - 使用 director_reference_images_cached 字段
    - 支持三种类型：character / style / character&style
    - 不绑定角色提示词框（v4_prompt.char_captions 为空）
    - 每张参考图 5 Anlas（官方文档确认）

    请求体格式:
        {
            "prompt": "1girl, ...",
            "model": "nai-v4.5-full",
            "negative_prompt": "lowres, ...",
            "width": 832,
            "height": 1216,
            "steps": 28,
            "scale": 5.0,
            "sampler": "k_dpmpp_2m",
            "noise_schedule": "karras",
            "seed": 12345,
            "response_format": "b64_json",
            "references": [
                {
                    "reference_image": "<base64>",
                    "reference_type": "character",  // character / style / character&style
                    "strength": 1.0,
                    "fidelity": 1.0
                }
            ]
        }
    """
    body = await request.json()

    prompt = body.get("prompt", "")
    model = body.get("model", "nai-diffusion-4-5-full")
    _reject_limit_model_for_paid_endpoint(model)
    negative_prompt = body.get("negative_prompt", DEFAULT_NEGATIVE_PROMPT)
    response_format = body.get("response_format", "b64_json")

    # references 列表
    references = body.get("references", [])
    if not references:
        raise HTTPException(status_code=400, detail="At least one reference with reference_image is required")

    # 提取参考图和参数
    ref_images: list[str] = []
    ref_strengths: list[float] = []
    ref_infos: list[float] = []
    ref_descriptions: list[dict[str, Any]] = []

    for i, ref in enumerate(references):
        if not isinstance(ref, dict):
            continue
        ref_img = ref.get("reference_image", "")
        if not ref_img:
            raise HTTPException(status_code=400, detail=f"references[{i}] missing reference_image")

        ref_images.append(_normalize_precise_reference_image(ref_img))
        ref_strengths.append(float(ref.get("strength", 1.0)))
        fidelity = float(ref.get("fidelity", 1.0))
        # NovelAI 当前上游仅接受精密参考的信息提取值恰为 1.0。
        if fidelity != 1.0:
            raise HTTPException(
                status_code=400,
                detail=f"references[{i}].fidelity must be exactly 1.0",
            )
        ref_infos.append(fidelity)

        # 构造 director_reference_description
        ref_type = ref.get("reference_type", "character&style")
        # 校验类型
        valid_types = {"character", "style", "character&style"}
        if ref_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail=f"references[{i}].reference_type must be one of: {', '.join(sorted(valid_types))}",
            )

        ref_descriptions.append({
            "caption": {
                "base_caption": ref_type,
                "char_captions": [],
            },
            "legacy_uc": False,
        })

    if not ref_images:
        raise HTTPException(status_code=400, detail="No valid reference images found")

    registry = get_registry()
    nai_model = registry.resolve_image_model(model) if registry else model

    # Precise Reference 仅支持 V4.5
    if "diffusion-4-5" not in nai_model and "diffusion-4" not in nai_model:
        raise HTTPException(
            status_code=400,
            detail="Precise Reference is only available on V4.5 models",
        )

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)
    width, height = _clamp_dimensions(width, height)

    quality_tags = body.get("quality_tags", QUALITY_TAGS)
    full_prompt = prompt + quality_tags if quality_tags else prompt

    params: dict[str, Any] = {
        "width": width,
        "height": height,
        "n_samples": 1,
        "seed": body.get("seed", int(time.time()) % 2**32),
        "steps": body.get("steps", 28),
        "scale": body.get("scale", 5.0),
        "sampler": body.get("sampler", "k_euler_ancestral"),
        "negative_prompt": negative_prompt,
        "sm": False,
        "sm_dyn": False,
        "noise_schedule": body.get("noise_schedule", "karras"),
        "cfg_rescale": body.get("cfg_rescale", 0.0),
        # Director Reference 核心字段
        "director_reference_descriptions": ref_descriptions,
        "director_reference_information_extracted": ref_infos,
        "director_reference_strength_values": ref_strengths,
        "director_reference_secondary_strength_values": [1.0 - ri for ri in ref_infos],
        "director_reference_images": ref_images,
        # V4.5 必需字段
        "params_version": 3,
        "legacy": False,
        "legacy_v3_extend": False,
        "legacy_uc": False,
        "add_original_image": True,
        "controlnet_strength": 1,
        "dynamic_thresholding": False,
        "prefer_brownian": True,
        "normalize_reference_strength_multiple": True,
        "use_coords": False,
        "inpaintImg2ImgStrength": 1,
        "deliberate_euler_ancestral_bug": False,
        "skip_cfg_above_sigma": None,
        "characterPrompts": [],
        # v4_prompt（char_captions 为空，不绑定角色框）
        "v4_prompt": {
            "caption": {
                "base_caption": full_prompt,
                "char_captions": [],
            },
            "use_coords": False,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {
                "base_caption": negative_prompt,
                "char_captions": [],
            },
            "legacy_uc": False,
        },
    }

    nai_payload = {
        "input": full_prompt,
        "model": nai_model,
        "action": "generate",
        "parameters": params,
    }

    accept_format = "json" if response_format == "nai_json" else "zip"

    async with gate:
        content = await _send_nai_request(request, nai_payload, accept_format=accept_format)

    record_generation(content, "/ai/generate-image", width, height)

    # Precise Reference 计费：每张参考图 5 Anlas（官方文档确认）
    ref_count = len(ref_images)
    anlas_cost = _calc_anlas_cost(
        width=width,
        height=height,
        steps=params.get("steps", 28),
        n_samples=1,
        uncond_scale=params.get("uncond_scale", 1.0),
        reference_image_count=ref_count,
        reference_mode="precise",
    )

    return _build_image_response_v2(request, content, prompt, response_format, anlas_cost=anlas_cost)


# ── /v1/images/upscale ────────────────────────────────────────

async def handle_upscale(request: Request) -> Response:
    """处理图像放大请求 (2x/4x)。"""
    body = await request.json()
    _reject_limit_model_for_paid_endpoint(body.get("model", ""))

    image = body.get("image", "")
    if not image:
        raise HTTPException(status_code=400, detail="image (base64) is required")

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)
    scale = _safe_int(body.get("scale", 4), 4)

    payload = {
        "image": image,
        "width": width,
        "height": height,
        "scale": scale,
    }

    target_url = settings.get_upstream_url("/ai/upscale")

    async with gate:
        content = await _send_nai_binary_request(request, payload, target_url, force_json=True)
    png_data = _extract_png_from_zip(content)

    # NAI upscale 返回 ZIP，网关统一对外返回 PNG。
    return Response(
        content=png_data,
        status_code=200,
        media_type="image/png",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── /v1/images/annotate ───────────────────────────────────────

# NAI annotate-image 支持的模型类型
# 通过抓包 NAI 官网 JS 源码确认: model 字段即为 annotate 类型
# 请求格式: {"model": "hed", "parameters": {"image": "base64..."}}
# 响应格式: application/x-zip-compressed (ZIP 包含 image_*.png)
_ANNOTATE_VALID_MODELS = {
    "canny", "hed", "midas", "mlsd", "openpose", "uniformer", "fake_scribble",
}


async def handle_annotate(request: Request) -> Response:
    """处理注释图生成请求 (Canny/HED/OpenPose 等)。

    走 /ai/annotate-image 端点（api.novelai.net，注意不是 image.novelai.net）。
    NAI 官网 JS 中 AnnotateImage = BackendUrl + "/ai/annotate-image"。

    请求格式: {"model": "hed", "parameters": {"image": "base64..."}}
    响应格式: ZIP 文件 (application/x-zip-compressed)，内含 image_*.png

    支持的 model 类型:
    - canny: Canny 边缘检测
    - hed: HED 边缘检测
    - midas: MiDaS 深度图
    - mlsd: MLSD 线段检测
    - openpose: OpenPose 姿态检测
    - uniformer: Uniformer 场景分割
    - fake_scribble: 伪手绘
    """
    body = await request.json()

    image = body.get("image", "")
    if not image:
        raise HTTPException(status_code=400, detail="image (base64) is required")

    # model 字段（兼容旧的 req_type 字段）
    model = body.get("model", body.get("req_type", "hed"))

    if model not in _ANNOTATE_VALID_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid model '{model}'. Must be one of: {sorted(_ANNOTATE_VALID_MODELS)}",
        )

    # NAI annotate-image 请求格式: {model, parameters: {image}}
    payload = {
        "model": model,
        "parameters": {
            "image": image,
        },
    }

    target_url = settings.get_upstream_url("/ai/annotate-image")

    # 不走排队（轻负载），annotate 端点需要纯 JSON
    content = await _send_nai_binary_request(request, payload, target_url, force_json=True)

    # NAI 返回 ZIP 文件，解压提取第一张 PNG
    try:
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            png_names = [n for n in zf.namelist() if n.endswith(".png")]
            if not png_names:
                raise ValueError("ZIP 中未找到 PNG 文件")
            # 取第一张 PNG
            png_data = zf.read(png_names[0])
    except Exception as e:
        logger.error(f"❌ annotate ZIP 解压失败: {e}")
        raise HTTPException(status_code=502, detail=f"响应解压失败: {e}")

    return Response(
        content=png_data,
        status_code=200,
        media_type="image/png",
        headers={
            "Access-Control-Allow-Origin": "*",
            "X-Annotate-Model": model,
        },
    )


# ── /v1/images/suggest-tags ───────────────────────────────────

async def handle_suggest_tags(request: Request) -> Response:
    """处理标签建议请求。

    NAI suggest-tags 是 GET 请求，需要 model 和 prompt 两个 query 参数。
    端点: image.novelai.net/ai/generate-image/suggest-tags
    返回 JSON 格式的标签建议列表。
    """
    body = await request.json()

    prompt = body.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    model = body.get("model", "nai-diffusion-3")

    from .forwarder import get_client

    token = _get_auth_token(request)
    headers = {
        "Authorization": f"Bearer {token}",
        "Origin": "https://novelai.net",
        "Referer": "https://novelai.net/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    target_url = settings.get_upstream_url("/ai/generate-image/suggest-tags")

    client = await get_client()
    try:
        resp = await client.get(
            target_url,
            params={"model": model, "prompt": prompt},
            headers=headers,
            timeout=15.0,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"上游请求失败: {e}")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.content[:500].decode("utf-8", errors="replace"))

    return Response(
        content=resp.content,
        status_code=200,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


# ── /v1/images/director/* (导演工具) ──────────────────────────

# Director Tools 各功能的 req_type 和计费信息
# 抓取自 NovelAI 官方网页端 POST /ai/augment-image
_DIRECTOR_TOOLS_INFO: dict[str, dict[str, Any]] = {
    "declutter": {
        "req_type": "declutter",
        "anlas": 5,
        "extra_fields": [],
    },
    "bg-remover": {
        "req_type": "bg-removal",
        "anlas": 65,  # 保持原价 65 Anlas
        "extra_fields": [],
    },
    "lineart": {
        "req_type": "lineart",
        "anlas": 5,
        "extra_fields": [],
    },
    "sketch": {
        "req_type": "sketch",
        "anlas": 5,
        "extra_fields": [],
    },
    "colorize": {
        "req_type": "colorize",
        "anlas": 5,
        "extra_fields": ["prompt", "defry"],
    },
    "emotion": {
        "req_type": "emotion",
        "anlas": 5,
        "extra_fields": ["prompt", "defry"],
    },
}


def _director_augment_payload(
    body: dict[str, Any],
    tool: str,
) -> dict[str, Any]:
    """构建 /ai/augment-image 请求 payload。

    所有 Director Tools 统一走 /ai/augment-image 端点，
    通过 req_type 字段区分功能。
    """
    info = _DIRECTOR_TOOLS_INFO.get(tool)
    if info is None:
        raise HTTPException(status_code=400, detail=f"Unknown director tool: {tool}")

    image = body.get("image", "")
    if not image:
        raise HTTPException(status_code=400, detail="image (base64) is required")

    width = _safe_int(body.get("width", 1024), 1024)
    height = _safe_int(body.get("height", 1024), 1024)

    payload: dict[str, Any] = {
        "req_type": info["req_type"],
        "width": width,
        "height": height,
        "image": image,
    }

    # colorize 和 emotion 有额外字段
    if "prompt" in info["extra_fields"]:
        payload["prompt"] = body.get("prompt", "")
    if "defry" in info["extra_fields"]:
        defry = _safe_int(body.get("defry", 0), 0)
        payload["defry"] = max(0, min(5, defry))

    return payload


async def _director_simple(request: Request, tool: str) -> Response:
    """导演工具通用实现：统一走 /ai/augment-image 端点。

    所有 Director Tools（declutter/bg-remover/lineart/sketch/colorize/emotion）
    都通过 POST /ai/augment-image 发送，用 req_type 字段区分功能。
    图片数据通过 multipart/form-data 传输。

    计费：declutter/lineart/sketch/colorize/emotion = 5 Anlas，bg-remover = 65 Anlas。
    """
    body = await request.json()
    info = _DIRECTOR_TOOLS_INFO[tool]
    payload = _director_augment_payload(body, tool)
    target_url = settings.get_upstream_url("/ai/augment-image")

    # 不走排队（轻负载）
    content = await _send_nai_binary_request(request, payload, target_url)
    png_data = _extract_png_from_zip(content)

    # 计算 Anlas 消耗并映射为 prompt_tokens
    anlas_cost = info["anlas"]
    prompt_tokens = _anlas_to_tokens(anlas_cost)

    return Response(
        content=png_data,
        status_code=200,
        media_type="image/png",
        headers={
            "Access-Control-Allow-Origin": "*",
            "X-Anlas-Cost": str(anlas_cost),
            "X-Prompt-Tokens": str(prompt_tokens),
        },
    )


async def handle_director_declutter(request: Request) -> Response:
    """导演工具 - 去杂物：移除画面中的悬浮文字、占位气泡等视觉噪点。

    req_type=declutter，消耗 2 Anlas。
    """
    return await _director_simple(request, "declutter")


async def handle_director_bg_remover(request: Request) -> Response:
    """导演工具 - 背景移除：抠出主体并补全被遮挡部分。

    req_type=bg-removal，消耗 65 Anlas（保持原价）。
    """
    return await _director_simple(request, "bg-remover")


async def handle_director_lineart(request: Request) -> Response:
    """导演工具 - 线稿提取：将图像还原到线稿阶段。

    req_type=lineart，消耗 2 Anlas。
    """
    return await _director_simple(request, "lineart")


async def handle_director_sketch(request: Request) -> Response:
    """导演工具 - 草图化：生成草图版本。

    req_type=sketch，消耗 2 Anlas。
    """
    return await _director_simple(request, "sketch")


async def handle_director_colorize(request: Request) -> Response:
    """导演工具 - 线稿上色：AI 解读线稿并上色，可附带 prompt 引导。

    req_type=colorize，消耗 2 Anlas。支持 prompt（可选）和 defry（0-5）参数。
    """
    return await _director_simple(request, "colorize")


async def handle_director_emotion(request: Request) -> Response:
    """导演工具 - 情感迁移：改变角色表情，建议用于单角色正面中性脸。

    req_type=emotion，消耗 2 Anlas。支持 prompt（情感描述）和 defry（0-5）参数。
    prompt 格式示例："neutral;;"、"happy;;"、"sad;;" 等。
    """
    return await _director_simple(request, "emotion")


async def _clone_json_request(request: Request, body: dict[str, Any]) -> Request:
    """创建保留原请求头、可供专用处理器重新读取 JSON 的请求副本。"""
    payload = json.dumps(body).encode("utf-8")
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": payload, "more_body": False}

    return Request(dict(request.scope), receive)


async def _dispatch_image_operation(
    request: Request,
    body: dict[str, Any],
    operation: str,
) -> Response:
    """通过标准 images/generations 路径分派非生成类图像操作。

    NewAPI 只需看到 OpenAI 标准路径；具体图像能力由
    ``X-NovelAI-Operation`` 选择。二进制 PNG 结果统一转为 b64_json，保留原始
    PNG 字节和其中的 NovelAI 元数据。
    """
    body = {**body, "response_format": "b64_json"}
    request_copy = await _clone_json_request(request, body)
    image_handlers = {
        "img2img": handle_img2img,
        "inpainting": handle_nai_inpainting,
        "edits": handle_openai_image_edits,
        "vibe-transfer": handle_vibe_transfer,
        "character-reference": handle_character_reference,
    }
    handler = image_handlers.get(operation)
    if handler is not None:
        return await handler(request_copy)

    png_handlers = {
        "upscale": (handle_upscale, 0),
        "annotate": (handle_annotate, 0),
        "director-declutter": (handle_director_declutter, 5),
        "director-bg-remover": (handle_director_bg_remover, 65),
        "director-lineart": (handle_director_lineart, 5),
        "director-sketch": (handle_director_sketch, 5),
        "director-colorize": (handle_director_colorize, 5),
        "director-emotion": (handle_director_emotion, 5),
    }
    png_handler_info = png_handlers.get(operation)
    if png_handler_info is None:
        raise HTTPException(status_code=400, detail=f"Unsupported image operation: {operation}")

    handler, anlas_cost = png_handler_info
    binary_response = await handler(request_copy)
    png_data = binary_response.body
    return _build_png_image_response(
        png_data=png_data,
        prompt=str(body.get("prompt", "")),
        anlas_cost=anlas_cost,
    )


# ── /v1/chat/completions (真文本 Chat) ────────────────────────

def _build_chat_input(messages: list[dict]) -> str:
    """
    将 OpenAI messages 拼接成 NAI 文本生成的 input 字符串。

    格式:
    ----
    [System: ...]
    User: ...
    Assistant: ...
    User: ...
    ----
    最终只取 assistant 续写位置后的内容。
    """
    parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            # 处理 multimodal content (只取 text 部分)
            text_parts = [p.get("text", "") for p in content if p.get("type") == "text"]
            content = "\n".join(text_parts)
        if role == "system":
            parts.append(f"[System: {content}]\n")
        elif role == "user":
            parts.append(f"User: {content}\n")
        elif role == "assistant":
            parts.append(f"Assistant: {content}\n")

    # 在末尾加 "Assistant:" 提示模型续写
    parts.append("Assistant:")
    return "".join(parts)


def _build_nai_text_payload(
    input_text: str,
    model: str = "xialong",
    temperature: float = 1.0,
    max_length: int = 200,
    top_p: float = 0.9,
    top_k: int = 3,
) -> dict[str, Any]:
    """构建 NAI 文本生成 payload。"""
    return {
        "input": input_text,
        "model": model,
        "parameters": {
            "use_string": True,
            "temperature": temperature,
            "max_length": max_length,
            "min_length": 1,
            "top_k": top_k,
            "top_p": top_p,
            "tail_free_sampling": 0.975,
            "repetition_penalty": 1.05,
            "repetition_penalty_range": 2048,
            "repetition_penalty_frequency": 0,
            "repetition_penalty_presence": 0,
            "order": [6, 0, 1, 2, 3],
            "force_emotion": False,
        },
    }


async def handle_openai_chat_completions(request: Request) -> Response:
    """处理 OpenAI 兼容的 Chat Completions 请求（真文本生成）。"""
    body = await request.json()

    # 检查 chat 是否启用
    registry = get_registry()
    if registry and not registry.is_enabled("chat"):
        return Response(
            content=json.dumps({
                "error": {
                    "message": "Chat is disabled in configuration",
                    "type": "config_error",
                    "code": "chat_disabled",
                }
            }),
            status_code=403,
            media_type="application/json",
        )

    messages = body.get("messages", [])
    model_id = body.get("model", "nai-chat")
    stream = body.get("stream", False)
    temperature = body.get("temperature", 1.0)
    max_tokens = body.get("max_tokens", 200)
    top_p = body.get("top_p", 0.9)

    # Chat 只能使用 models.toml 中显式配置的模型，避免未知 model 静默回退。
    entry = registry.lookup(model_id) if registry else None
    if entry is None or entry.type != "chat":
        raise HTTPException(
            status_code=400,
            detail=f"Unknown or disabled Chat model: {model_id}. Check GET /v1/models.",
        )
    nai_model = entry.name

    # 构建 input
    input_text = _build_chat_input(messages)

    # 构建 NAI payload
    nai_payload = _build_nai_text_payload(
        input_text=input_text,
        model=nai_model,
        temperature=temperature,
        max_length=max_tokens,
        top_p=top_p,
    )

    timestamp = int(time.time())
    chat_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    if stream:
        # 流式响应
        nai_resp = await _send_nai_text_request(request, nai_payload, stream=True)
        return _build_chat_stream_response(nai_resp, model_id, timestamp, chat_id)
    else:
        # 非流式响应
        resp_bytes = await _send_nai_text_request(request, nai_payload, stream=False)
        try:
            resp_data = json.loads(resp_bytes)
            output = resp_data.get("output", "")
        except (json.JSONDecodeError, Exception):
            output = resp_bytes.decode("utf-8", errors="replace")

        return Response(
            content=json.dumps({
                "id": chat_id,
                "object": "chat.completion",
                "created": timestamp,
                "model": model_id,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": output},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }),
            status_code=200,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )


def _build_chat_stream_response(nai_resp, model: str, timestamp: int, chat_id: str) -> StreamingResponse:
    """将 NAI SSE 流转换为 OpenAI chat chunk SSE 流。"""

    async def generate():
        try:
            async for line in nai_resp.aiter_lines():
                if not line:
                    continue
                # NAI SSE 格式: data: {...}
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                elif line.startswith("{"):
                    data_str = line
                else:
                    continue

                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                token = data.get("token", "")
                is_final = data.get("final", False)

                if token:
                    chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": timestamp,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {"content": token},
                            "finish_reason": None,
                        }],
                    }
                    yield f"data: {json.dumps(chunk)}\n\n"

                if is_final:
                    # 发送 finish 标记
                    final_chunk = {
                        "id": chat_id,
                        "object": "chat.completion.chunk",
                        "created": timestamp,
                        "model": model,
                        "choices": [{
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                        }],
                    }
                    yield f"data: {json.dumps(final_chunk)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
        except Exception as e:
            logger.error(f"❌ Chat stream error: {e}")
        finally:
            await nai_resp.aclose()

        # 如果流正常结束但没收到 final=true
        yield "data: [DONE]\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── /v1/audio/speech (TTS) ────────────────────────────────────

def _parse_tts_name(name: str) -> dict[str, Any]:
    """解析 TTS 模型的 name 字段为参数字典。"""
    params = {}
    for part in name.split(","):
        key, _, value = part.strip().partition("=")
        if key == "voice":
            params["voice"] = int(value)
        elif key == "version":
            params["version"] = value
        elif key == "opus":
            params["opus"] = value.lower() == "true"
        elif key == "seed":
            params["seed"] = value
    return params


def _resolve_tts_params(body: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """合并并校验下游 TTS 参数，返回 NovelAI generate-voice payload 参数。"""
    unsupported_playback_params = {"speed", "volume"}.intersection(body)
    if unsupported_playback_params:
        param_names = ", ".join(sorted(unsupported_playback_params))
        raise HTTPException(
            status_code=400,
            detail=(
                f"{param_names} only control NovelAI web playback and do not affect "
                "downloaded audio; they are not supported by this endpoint"
            ),
        )

    params = dict(defaults)

    if "version" in body:
        version = body["version"]
        if not isinstance(version, str) or version not in VALID_TTS_VERSIONS:
            raise HTTPException(status_code=400, detail="version must be 'v1' or 'v2'")
        params["version"] = version

    if "opus" in body:
        opus = body["opus"]
        if not isinstance(opus, bool):
            raise HTTPException(status_code=400, detail="opus must be a boolean")
        params["opus"] = opus

    if "response_format" in body:
        response_format = body["response_format"]
        if response_format not in TTS_RESPONSE_FORMAT_TO_OPUS:
            raise HTTPException(
                status_code=400,
                detail="response_format must be 'mp3' or 'opus'",
            )
        response_opus = TTS_RESPONSE_FORMAT_TO_OPUS[response_format]
        if "opus" in body and body["opus"] != response_opus:
            raise HTTPException(
                status_code=400,
                detail="opus conflicts with response_format",
            )
        params["opus"] = response_opus

    custom_seed: str | None = None
    if "seed" in body:
        seed = body["seed"]
        if not isinstance(seed, str) or not seed.strip():
            raise HTTPException(status_code=400, detail="seed must be a non-empty string")
        custom_seed = seed

    # OpenAI voice is a string; keep it as a backwards-compatible custom-seed alias.
    if "voice" in body:
        voice = body["voice"]
        if not isinstance(voice, str) or not voice.strip():
            raise HTTPException(status_code=400, detail="voice must be a non-empty string")
        if custom_seed is not None and custom_seed != voice:
            raise HTTPException(status_code=400, detail="voice conflicts with seed")
        custom_seed = voice

    voice_id: int | None = None
    if "voice_id" in body:
        candidate = body["voice_id"]
        if isinstance(candidate, bool) or not isinstance(candidate, int):
            raise HTTPException(status_code=400, detail="voice_id must be an integer")
        if candidate not in VALID_TTS_VOICE_IDS:
            raise HTTPException(
                status_code=400,
                detail="voice_id must be one of -1, 0, or 1",
            )
        voice_id = candidate

    if custom_seed is not None:
        if voice_id is not None and voice_id != -1:
            raise HTTPException(
                status_code=400,
                detail="custom seed requires voice_id=-1 or omitting voice_id",
            )
        if params["version"] != "v2" and custom_seed.startswith("seedmix:"):
            raise HTTPException(status_code=400, detail="seedmix is only supported by version='v2'")
        params["seed"] = custom_seed
        params["voice"] = -1
    elif voice_id is not None:
        params["voice"] = voice_id

    return params


async def handle_tts(request: Request) -> Response:
    """处理 OpenAI 兼容的 TTS 请求。"""
    body = await request.json()

    # 检查 TTS 是否启用
    registry = get_registry()
    if registry and not registry.is_enabled("tts"):
        return Response(
            content=json.dumps({
                "error": {
                    "message": "TTS is disabled in configuration",
                    "type": "config_error",
                    "code": "tts_disabled",
                }
            }),
            status_code=403,
            media_type="application/json",
        )

    model_id = body.get("model", "tts0-v2-mp3")
    input_text = body.get("input", "")
    if not isinstance(input_text, str) or not input_text:
        raise HTTPException(status_code=400, detail="input text is required")

    # 从 registry 查找 TTS 模型
    tts_params = {
        "voice": 0,
        "version": "v2",
        "opus": False,
        "seed": "Aini",
    }

    if registry:
        entry = registry.lookup(model_id)
        if entry and entry.type == "tts":
            tts_params = _parse_tts_name(entry.name)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown or disabled TTS model: {model_id}. Check GET /v1/models.",
            )

    tts_params = _resolve_tts_params(body, tts_params)

    payload = {
        "text": input_text,
        "voice": tts_params.get("voice", 0),
        "version": tts_params.get("version", "v2"),
        "opus": tts_params.get("opus", False),
        "seed": tts_params.get("seed", "Aini"),
    }

    target_url = settings.get_upstream_url("/ai/generate-voice")

    # 走排队门控
    async with gate:
        content = await _send_nai_binary_request(request, payload, target_url)

    # 返回音频
    # NovelAI 的 opus=true 当前封装为 WebM/Opus，而非 Ogg/Opus。
    content_type = "audio/webm" if tts_params.get("opus", False) else "audio/mpeg"

    return Response(
        content=content,
        status_code=200,
        media_type=content_type,
        headers={"Access-Control-Allow-Origin": "*"},
    )
