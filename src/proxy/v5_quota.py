"""
图像生成限额模块。

NovelAI Opus V5 免费额度（官方 UI 实测 2026-08-21）：
- 每周总量 1730 张（UI 显示 "99% remaining (~1713 images)"）
- 每日自动补充 ~190 张（"Currently refills at 11% per day (~190 images)"）

本站限制策略（V5 双限额，任一触达即拒绝）：
1. 每日上限 190 张 —— 对齐官方每日补充速率，避免净消耗存量额度；
2. 滚动 7 天窗口上限 1730 张 —— 对齐官方每周总量硬顶。

限制对象：所有 V5 系模型（`nai-diffusion-5-*`，含 -limit 免费额度变体），
按请求成功的生成张数（n_samples）累计，img2img / infill / vibe /
character-reference / precise-reference 各端点同样计入。

统计维度：按模型分别累计（V4.5 / V5 各模型独立计数，如 full / curated /
inpaint），持久化到 logs/v5_daily_usage.json，结构为
``{model: {date: count}}``；V5 限额按所有 V5 模型合计判断（账号级硬顶）。

- 请求发送前调用 ``check_v5_quota`` 预检，超限直接拒绝（不再消耗上游额度）；
- 生成成功后调用 ``log_generation`` / ``record_v5_generation`` 计数。

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


def _fmt_params(params: dict[str, Any] | None) -> str:
    """渲染请求参数摘要：``1024×1024·28步·k_euler``（缺失字段自动跳过）。"""
    if not params:
        return ""
    parts: list[str] = []
    w, h = params.get("width"), params.get("height")
    if w and h:
        parts.append(f"{w}×{h}")
    steps = params.get("steps")
    if steps:
        parts.append(f"{steps}步")
    sampler = params.get("sampler")
    if sampler:
        parts.append(str(sampler))
    return " · ".join(parts)


def is_v5_model(nai_model: str | None) -> bool:
    """判断内部模型名是否为 V5 系模型。"""
    return isinstance(nai_model, str) and "diffusion-5" in nai_model


def _load_usage() -> dict[str, dict[str, int]]:
    """加载按模型统计的用量 JSON。

    新格式：``{model: {date: count}}``；旧格式 ``{date: count}`` 自动迁移为
    ``{"*": {date: count}}``（* 表示未知/旧数据模型）。
    """
    if not os.path.exists(V5_USAGE_JSON):
        return {}
    try:
        with open(V5_USAGE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        # 旧格式迁移：顶层 key 全是日期（YYYY-MM-DD）
        if data and all(len(k) == 10 and k[4] == "-" and k[7] == "-" for k in data):
            return {"*": data}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _save_usage(data: dict[str, dict[str, int]]) -> None:
    """保存按模型统计的用量 JSON。"""
    os.makedirs(os.path.dirname(V5_USAGE_JSON) or ".", exist_ok=True)
    with open(V5_USAGE_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _today() -> str:
    """返回 UTC+8 自然日字符串（YYYY-MM-DD）。"""
    return datetime.now(_CST).strftime("%Y-%m-%d")


def _model_today(usage: dict[str, dict[str, int]], model: str, today: str) -> int:
    """指定模型当日生成张数。"""
    return usage.get(model, {}).get(today, 0)


def _model_week_total(usage: dict[str, dict[str, int]], model: str, today: str) -> int:
    """指定模型滚动 7 天窗口（含今天）生成总量。"""
    day = datetime.strptime(today, "%Y-%m-%d").date()
    model_usage = usage.get(model, {})
    return sum(model_usage.get((day - timedelta(days=i)).isoformat(), 0) for i in range(_WEEK_WINDOW_DAYS))


def _v5_today(usage: dict[str, dict[str, int]], today: str) -> int:
    """V5 系模型（含旧数据 *）当日合计，用于限额判断。"""
    return sum(
        m.get(today, 0) for k, m in usage.items() if k == "*" or is_v5_model(k)
    )


def _v5_week_total(usage: dict[str, dict[str, int]], today: str) -> int:
    """V5 系模型（含旧数据 *）滚动 7 天合计，用于限额判断。"""
    day = datetime.strptime(today, "%Y-%m-%d").date()
    return sum(
        m.get((day - timedelta(days=i)).isoformat(), 0)
        for k, m in usage.items()
        if k == "*" or is_v5_model(k)
        for i in range(_WEEK_WINDOW_DAYS)
    )


def check_v5_quota(nai_model: str | None, n_samples: int = 1) -> None:
    """请求发送前预检：V5 模型当日/滚动周用量达到上限时抛 ValueError。

    仅对 V5 系模型生效；非 V5 模型直接放行。
    限额按所有 V5 模型合计判断（账号级硬顶）。

    Raises:
        ValueError: 当日或滚动 7 天 V5 生成张数已达上限（剩余 < n_samples）
    """
    if not is_v5_model(nai_model) or n_samples <= 0:
        return
    today = _today()
    with _lock:
        usage = _load_usage()
        used_today = _v5_today(usage, today)
        used_week = _v5_week_total(usage, today)
    remaining_today = V5_DAILY_LIMIT - used_today
    remaining_week = V5_WEEKLY_LIMIT - used_week
    if remaining_today < n_samples:
        _logger.warning(
            f"⚠️ V5 限额拒绝 | {nai_model} 请求 {n_samples} 张 | "
            f"今日 {used_today}/{V5_DAILY_LIMIT} ({_pct(used_today, V5_DAILY_LIMIT)}) | "
            f"本周 {used_week}/{V5_WEEKLY_LIMIT} ({_pct(used_week, V5_WEEKLY_LIMIT)})"
        )
        raise ValueError(
            f"V5 今日免费额度已用完：今日已生成 {used_today}/{V5_DAILY_LIMIT} 张，"
            f"本次请求需要 {n_samples} 张（剩余 {max(remaining_today, 0)} 张），"
            f"请明天再试或改用 V4.5 模型"
        )
    if remaining_week < n_samples:
        _logger.warning(
            f"⚠️ V5 限额拒绝 | {nai_model} 请求 {n_samples} 张 | "
            f"今日 {used_today}/{V5_DAILY_LIMIT} ({_pct(used_today, V5_DAILY_LIMIT)}) | "
            f"本周 {used_week}/{V5_WEEKLY_LIMIT} ({_pct(used_week, V5_WEEKLY_LIMIT)})"
        )
        raise ValueError(
            f"V5 本周免费额度已用完：近 7 天已生成 {used_week}/{V5_WEEKLY_LIMIT} 张，"
            f"本次请求需要 {n_samples} 张（剩余 {max(remaining_week, 0)} 张），"
            f"请下周再试或改用 V4.5 模型"
        )


def _record_model_usage(nai_model: str, n_samples: int) -> None:
    """按模型累计当日生成张数（所有模型通用，不参与限额判断）。"""
    today = _today()
    with _lock:
        usage = _load_usage()
        model_usage = usage.setdefault(nai_model, {})
        model_usage[today] = model_usage.get(today, 0) + n_samples
        _save_usage(usage)


def record_v5_generation(
    nai_model: str | None,
    n_samples: int = 1,
    params: dict[str, Any] | None = None,
) -> None:
    """V5 生成成功后计数：按模型累计 + 账号级 V5 合计进度日志。

    仅对 V5 系模型生效；非 V5 模型直接放行。

    Args:
        nai_model: NAI 内部模型名（如 ``nai-diffusion-5-full``）。
        n_samples: 本次成功生成的张数。
        params: 请求参数（width/height/steps/sampler 等），仅用于日志展示。
    """
    if not is_v5_model(nai_model) or n_samples <= 0:
        return
    _record_model_usage(nai_model, n_samples)
    today = _today()
    with _lock:
        usage = _load_usage()
        model_today = _model_today(usage, nai_model, today)
        model_week = _model_week_total(usage, nai_model, today)
        v5_today = _v5_today(usage, today)
        v5_week = _v5_week_total(usage, today)
    param_str = _fmt_params(params)
    extra = f" | {param_str}" if param_str else ""
    _logger.info(
        f"🎨 V5 +{n_samples} 张 | {nai_model}{extra} | "
        f"模型 今日 {model_today} · 本周 {model_week} | "
        f"V5合计 今日 {v5_today}/{V5_DAILY_LIMIT} {_bar(v5_today, V5_DAILY_LIMIT)} "
        f"({_pct(v5_today, V5_DAILY_LIMIT)}) | "
        f"本周 {v5_week}/{V5_WEEKLY_LIMIT} {_bar(v5_week, V5_WEEKLY_LIMIT)} "
        f"({_pct(v5_week, V5_WEEKLY_LIMIT)})"
    )


def log_generation(
    nai_model: str | None,
    n_samples: int = 1,
    params: dict[str, Any] | None = None,
) -> None:
    """生成成功日志：按模型分别统计，V5 额外参与限额计数。

    - V5 系模型：按模型计数 + 账号级 V5 进度条日志（调用 ``record_v5_generation``）；
    - 非 V5（V4 / V4.5 / 其他）：按模型分别计数 + 单行日志，不参与 V5 限额。

    Args:
        nai_model: NAI 内部模型名（如 ``nai-diffusion-5-full`` / ``nai-diffusion-4-5-full``）。
        n_samples: 本次成功生成的张数。
        params: 请求参数（width/height/steps/sampler 等），仅用于日志展示。
    """
    if not nai_model or n_samples <= 0:
        return
    if is_v5_model(nai_model):
        record_v5_generation(nai_model, n_samples, params)
        return
    _record_model_usage(nai_model, n_samples)
    today = _today()
    with _lock:
        usage = _load_usage()
        model_today = _model_today(usage, nai_model, today)
        model_week = _model_week_total(usage, nai_model, today)
    param_str = _fmt_params(params)
    extra = f" | {param_str}" if param_str else ""
    _logger.info(
        f"🎨 生成 +{n_samples} 张 | {nai_model}{extra} | "
        f"模型 今日 {model_today} · 本周 {model_week}"
    )


def get_usage() -> dict[str, Any]:
    """查询当前限额状态（供调试/文档用）。

    返回含按模型明细（per_model）与 V5 账号级合计（used_today/used_this_week）。
    """
    usage = _load_usage()
    today = _today()
    v5_today = _v5_today(usage, today)
    v5_week = _v5_week_total(usage, today)
    per_model: dict[str, dict[str, int]] = {}
    for model in usage:
        if model == "*":
            continue
        per_model[model] = {
            "used_today": _model_today(usage, model, today),
            "used_this_week": _model_week_total(usage, model, today),
        }
    return {
        "daily_limit": V5_DAILY_LIMIT,
        "weekly_limit": V5_WEEKLY_LIMIT,
        "today": today,
        "used_today": v5_today,
        "remaining_today": max(V5_DAILY_LIMIT - v5_today, 0),
        "used_this_week": v5_week,
        "remaining_week": max(V5_WEEKLY_LIMIT - v5_week, 0),
        "per_model": per_model,
        "history": usage,
    }
