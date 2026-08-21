"""
V5 图像生成限额模块。

NovelAI Diffusion V5 每周限额 1730 张，按自然日（UTC+8）均摊：
    1730 / 7 ≈ 247 张/天（余 1 张机动，累计一周最多 1729 张）。

限制对象：所有 V5 系模型（`nai-diffusion-5-*`，含 -limit 免费额度变体），
按请求成功的生成张数（n_samples）累计，img2img / infill / vibe /
character-reference / precise-reference 各端点同样计入。

- 请求发送前调用 ``check_v5_quota`` 预检，超限直接拒绝（不再消耗上游额度）；
- 生成成功后调用 ``record_v5_generation`` 计数，持久化到 logs/v5_daily_usage.json。

并发控制：模块级锁保证计数原子性；预检与计数分离，极端并发下可能略超，
可接受（V5 上架初期请求量有限，后续可按需升级为预占式）。
"""

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

# ── 常量 ─────────────────────────────────────────────────────

# 每周限额 1730 张 → 每日 247 张（1730 = 247 * 7 + 1）
V5_WEEKLY_LIMIT = 1730
V5_DAILY_LIMIT = V5_WEEKLY_LIMIT // 7

# 计数文件（与 stats 模块同目录）
V5_USAGE_JSON = "logs/v5_daily_usage.json"

# UTC+8 时区
_CST = timezone(timedelta(hours=8))

_lock = threading.Lock()
_logger = logging.getLogger("v5_quota")


def is_v5_model(nai_model: str | None) -> bool:
    """判断内部模型名是否为 V5 系模型。"""
    return isinstance(nai_model, str) and "diffusion-5" in nai_model


def _load_usage() -> dict[str, int]:
    """加载每日用量 JSON。"""
    if not os.path.exists(V5_USAGE_JSON):
        return {}
    try:
        with open(V5_USAGE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_usage(data: dict[str, int]) -> None:
    """保存每日用量 JSON。"""
    os.makedirs(os.path.dirname(V5_USAGE_JSON) or ".", exist_ok=True)
    with open(V5_USAGE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _today() -> str:
    """返回 UTC+8 自然日字符串（YYYY-MM-DD）。"""
    return datetime.now(_CST).strftime("%Y-%m-%d")


def check_v5_quota(nai_model: str | None, n_samples: int = 1) -> None:
    """请求发送前预检：V5 模型当日用量达到上限时抛 ValueError。

    仅对 V5 系模型生效；非 V5 模型直接放行。

    Raises:
        ValueError: 当日 V5 生成张数已达上限（剩余 < n_samples）
    """
    if not is_v5_model(nai_model) or n_samples <= 0:
        return
    today = _today()
    with _lock:
        used = _load_usage().get(today, 0)
    remaining = V5_DAILY_LIMIT - used
    if remaining < n_samples:
        raise ValueError(
            f"V5 模型今日生成额度已用完：今日已生成 {used}/{V5_DAILY_LIMIT} 张，"
            f"本次请求需要 {n_samples} 张（剩余 {max(remaining, 0)} 张），"
            f"请明天再试或改用 V4.5 模型"
        )


def record_v5_generation(nai_model: str | None, n_samples: int = 1) -> None:
    """生成成功后计数：当日 V5 生成张数累加 n_samples。

    仅对 V5 系模型生效；非 V5 模型直接放行。
    """
    if not is_v5_model(nai_model) or n_samples <= 0:
        return
    today = _today()
    with _lock:
        usage = _load_usage()
        usage[today] = usage.get(today, 0) + n_samples
        _save_usage(usage)
        total = usage[today]
    _logger.info(f"V5 生成计数 +{n_samples}，今日 {total}/{V5_DAILY_LIMIT} 张（{nai_model}）")


def get_usage() -> dict[str, Any]:
    """查询当前限额状态（供调试/文档用）。"""
    usage = _load_usage()
    today = _today()
    return {
        "daily_limit": V5_DAILY_LIMIT,
        "weekly_limit": V5_WEEKLY_LIMIT,
        "today": today,
        "used_today": usage.get(today, 0),
        "remaining_today": max(V5_DAILY_LIMIT - usage.get(today, 0), 0),
        "history": usage,
    }
