"""
V5 图像生成限额模块。

NovelAI Opus V5 免费额度（官方 UI 实测 2026-08-21）：
- 每周总量 1730 张（UI 显示 "99% remaining (~1713 images)"）
- 每日自动补充 ~190 张（"Currently refills at 11% per day (~190 images)"）

本站限制策略（双限额，任一触达即拒绝）：
1. 每日上限 190 张 —— 对齐官方每日补充速率，避免净消耗存量额度；
2. 滚动 7 天窗口上限 1730 张 —— 对齐官方每周总量硬顶。

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

# 官方免费额度：每周 1730 张、每日补充 ~190 张（11%）
V5_WEEKLY_LIMIT = 1730
V5_DAILY_LIMIT = 190

# 滚动周窗口天数（对齐官方"随时间自动补充"）
_WEEK_WINDOW_DAYS = 7

# 计数文件（与 stats 模块同目录）
V5_USAGE_JSON = "logs/v5_daily_usage.json"

# UTC+8 时区
_CST = timezone(timedelta(hours=8))

_lock = threading.Lock()
_logger = logging.getLogger("v5_quota")
# 独立设置级别：main.py 只把 gateway 设为 INFO，不设的话本模块 INFO 日志不会显示
_logger.setLevel(logging.INFO)


# ── 日志美化 ──────────────────────────────────────────────────

def _bar(used: int, limit: int, width: int = 10) -> str:
    """渲染进度条：``██████░░░░``（默认 10 格）。"""
    ratio = min(used / limit, 1.0) if limit else 0.0
    filled = int(round(ratio * width))
    return "█" * filled + "░" * (width - filled)


def _pct(used: int, limit: int) -> str:
    """渲染百分比字符串（0-100%，无小数）。"""
    return f"{used / limit * 100:.0f}%" if limit else "0%"


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


def _week_total(usage: dict[str, int], today: str) -> int:
    """滚动 7 天窗口（含今天）的 V5 生成总量。"""
    day = datetime.strptime(today, "%Y-%m-%d").date()
    total = 0
    for i in range(_WEEK_WINDOW_DAYS):
        key = (day - timedelta(days=i)).isoformat()
        total += usage.get(key, 0)
    return total


def check_v5_quota(nai_model: str | None, n_samples: int = 1) -> None:
    """请求发送前预检：V5 模型当日/滚动周用量达到上限时抛 ValueError。

    仅对 V5 系模型生效；非 V5 模型直接放行。

    Raises:
        ValueError: 当日或滚动 7 天 V5 生成张数已达上限（剩余 < n_samples）
    """
    if not is_v5_model(nai_model) or n_samples <= 0:
        return
    today = _today()
    with _lock:
        usage = _load_usage()
        used_today = usage.get(today, 0)
        used_week = _week_total(usage, today)
    remaining_today = V5_DAILY_LIMIT - used_today
    remaining_week = V5_WEEKLY_LIMIT - used_week
    if remaining_today < n_samples:
        raise ValueError(
            f"V5 今日免费额度已用完：今日已生成 {used_today}/{V5_DAILY_LIMIT} 张，"
            f"本次请求需要 {n_samples} 张（剩余 {max(remaining_today, 0)} 张），"
            f"请明天再试或改用 V4.5 模型"
        )
    if remaining_week < n_samples:
        raise ValueError(
            f"V5 本周免费额度已用完：近 7 天已生成 {used_week}/{V5_WEEKLY_LIMIT} 张，"
            f"本次请求需要 {n_samples} 张（剩余 {max(remaining_week, 0)} 张），"
            f"请下周再试或改用 V4.5 模型"
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
        used_today = usage[today]
        used_week = _week_total(usage, today)
    _logger.info(
        f"🎨 V5 生成 +{n_samples} 张 | {nai_model} | "
        f"今日 {used_today}/{V5_DAILY_LIMIT} {_bar(used_today, V5_DAILY_LIMIT)} "
        f"({_pct(used_today, V5_DAILY_LIMIT)}) | "
        f"本周 {used_week}/{V5_WEEKLY_LIMIT} {_bar(used_week, V5_WEEKLY_LIMIT)} "
        f"({_pct(used_week, V5_WEEKLY_LIMIT)})"
    )


def get_usage() -> dict[str, Any]:
    """查询当前限额状态（供调试/文档用）。"""
    usage = _load_usage()
    today = _today()
    used_today = usage.get(today, 0)
    used_week = _week_total(usage, today)
    return {
        "daily_limit": V5_DAILY_LIMIT,
        "weekly_limit": V5_WEEKLY_LIMIT,
        "today": today,
        "used_today": used_today,
        "remaining_today": max(V5_DAILY_LIMIT - used_today, 0),
        "used_this_week": used_week,
        "remaining_week": max(V5_WEEKLY_LIMIT - used_week, 0),
        "history": usage,
    }
