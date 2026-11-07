"""
生成统计模块。

记录每次图片生成的画幅、大小，并按日汇总到 JSON 文件。
分类标准：像素数 > 1,048,576（即超过 1024x1024）为大图。
"""

import io
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
import struct
import time
import zipfile
from datetime import datetime
from threading import Lock

# ── 初始化 ─────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

stats_logger = logging.getLogger("stats")
stats_logger.setLevel(logging.INFO)
if not stats_logger.handlers:
    fh = RotatingFileHandler(
        "logs/stats.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-10s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    stats_logger.addHandler(fh)

STATS_JSON = "logs/stats_summary.json"
_stats_lock = Lock()

# 大图阈值：超过 1M 像素
_LARGE_THRESHOLD = 1024 * 1024

LATENCY_JSON = "logs/model_latency.json"
LATENCY_SAMPLES_JSON = "logs/model_latency_samples.json"
_LATENCY_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_LATENCY_SAMPLES = 100_000
_latency_lock = Lock()


def record_model_latency(
    model: str,
    elapsed_seconds: float,
    queued_elapsed_seconds: float | None = None,
) -> None:
    """Record active upstream time and full queue-to-response time."""
    model = model.removesuffix("-limit")
    if not any(family in model for family in ("diffusion-4-5", "diffusion-5", "v4.5", "v5")):
        return

    timestamp = time.time()
    minute = int(timestamp // 60)
    active_ms = max(0.0, float(elapsed_seconds)) * 1000
    queued_ms = max(
        active_ms,
        max(0.0, float(queued_elapsed_seconds if queued_elapsed_seconds is not None else elapsed_seconds)) * 1000,
    )
    with _latency_lock:
        data = _load_latency()
        data = {
            key: value
            for key, value in data.items()
            if _is_recent_minute(key, minute)
        }
        minute_data = data.setdefault(str(minute), {})
        if not isinstance(minute_data, dict):
            minute_data = data[str(minute)] = {}
        bucket = minute_data.setdefault(model, {"count": 0, "total_ms": 0.0})
        if not isinstance(bucket, dict):
            bucket = minute_data[model] = {"count": 0, "total_ms": 0.0}
        bucket["count"] += 1
        bucket["total_ms"] += active_ms
        _write_json_atomic(LATENCY_JSON, data)

        samples = _load_latency_samples()
        threshold = timestamp - _LATENCY_RETENTION_SECONDS
        samples = [
            sample for sample in samples
            if _valid_latency_sample(sample) and float(sample["timestamp"]) >= threshold
        ]
        samples.append({
            "timestamp": timestamp,
            "model": model,
            "active_ms": active_ms,
            "queued_ms": queued_ms,
        })
        if len(samples) > _MAX_LATENCY_SAMPLES:
            samples = samples[-_MAX_LATENCY_SAMPLES:]
        _write_json_atomic(LATENCY_SAMPLES_JSON, samples)


def _is_recent_minute(key: str, current_minute: int) -> bool:
    try:
        return int(key) >= current_minute - 7 * 24 * 60
    except (TypeError, ValueError):
        return False


def _valid_latency_sample(sample: object) -> bool:
    return (
        isinstance(sample, dict)
        and isinstance(sample.get("model"), str)
        and all(isinstance(sample.get(key), (int, float)) for key in ("timestamp", "active_ms", "queued_ms"))
        and all(math.isfinite(float(sample[key])) for key in ("timestamp", "active_ms", "queued_ms"))
    )


def _write_json_atomic(path: str, data: object) -> None:
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(data, stream, separators=(",", ":"))
    os.replace(temporary, path)


def _load_latency() -> dict:
    try:
        with open(LATENCY_JSON, encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_latency_samples() -> list[dict]:
    try:
        with open(LATENCY_SAMPLES_JSON, encoding="utf-8") as stream:
            data = json.load(stream)
        return [sample for sample in data if _valid_latency_sample(sample)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def queue_inclusive_elapsed(request, fallback_seconds: float) -> float:
    state = getattr(request, "state", None)
    started = getattr(state, "gateway_request_started_at", None)
    if started is None:
        return max(0.0, fallback_seconds)
    try:
        return max(max(0.0, fallback_seconds), time.monotonic() - float(started))
    except (TypeError, ValueError):
        return max(0.0, fallback_seconds)


def get_model_latency(
    window_minutes: int = 24 * 60,
    points: int = 24,
    mode: str = "active",
    bucket_seconds: int | None = None,
) -> dict:
    """Return per-window OHLC candles and averages for one latency mode."""
    window_minutes = max(1, min(window_minutes, 7 * 24 * 60))
    points = max(2, points)
    if mode not in {"active", "queued"}:
        raise ValueError("mode must be 'active' or 'queued'")

    now = time.time()
    cutoff = now - window_minutes * 60
    start = cutoff
    bucket_width = (now - start) / points
    if bucket_seconds is not None:
        bucket_width = max(1, int(bucket_seconds))
        start = math.floor(cutoff / bucket_width) * bucket_width
        points = max(1, math.ceil((now - start) / bucket_width))
    timestamps = [start + index * bucket_width for index in range(points)]
    with _latency_lock:
        samples = _load_latency_samples()
        legacy = _load_latency() if mode == "active" else {}

    metric_key = "active_ms" if mode == "active" else "queued_ms"
    by_model: dict[str, list[tuple[float, float, bool]]] = {}
    sampled_minutes: set[tuple[int, str]] = set()
    for sample in samples:
        sample_time = float(sample["timestamp"])
        if cutoff <= sample_time <= now:
            model = sample["model"].removesuffix("-limit")
            by_model.setdefault(model, []).append((sample_time, max(0.0, float(sample[metric_key])), False))
            sampled_minutes.add((int(sample_time // 60), model))

    # Preserve the old active-time history as flat one-sample candles until the
    # gateway has collected request-level OHLC samples for those minutes.
    if mode == "active":
        for minute_key, minute_data in legacy.items():
            try:
                minute = int(minute_key)
            except (TypeError, ValueError):
                continue
            sample_time = minute * 60 + 30
            if not cutoff <= sample_time <= now or not isinstance(minute_data, dict):
                continue
            for raw_model, entry in minute_data.items():
                if not isinstance(entry, dict):
                    continue
                model = str(raw_model).removesuffix("-limit")
                try:
                    count = max(0, int(entry.get("count", 0) or 0))
                except (TypeError, ValueError, OverflowError):
                    continue
                if count == 0 or (minute, model) in sampled_minutes:
                    continue
                try:
                    average = max(0.0, float(entry.get("total_ms", 0.0)) / count)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                by_model.setdefault(model, []).append((sample_time, average, True))

    model_buckets: dict[str, list[list[tuple[float, float, bool]]]] = {}
    for model, values in by_model.items():
        buckets = [[] for _ in range(points)]
        for sample_time, value, legacy_value in sorted(values):
            index = min(points - 1, max(0, int((sample_time - start) / bucket_width)))
            buckets[index].append((sample_time, value, legacy_value))
        model_buckets[model] = buckets

    series = []
    for model in sorted(model_buckets):
        candles = []
        averages = []
        counts = []
        for values in model_buckets[model]:
            if not values:
                candles.append(None)
                averages.append(None)
                counts.append(0)
                continue
            latencies = [value for _, value, _ in values]
            candles.append({
                "open_ms": latencies[0],
                "high_ms": max(latencies),
                "low_ms": min(latencies),
                "close_ms": latencies[-1],
                "count": len(latencies),
                "legacy_count": sum(1 for _, _, legacy_value in values if legacy_value),
                "first_timestamp": values[0][0],
                "last_timestamp": values[-1][0],
            })
            averages.append(sum(latencies) / len(latencies))
            counts.append(len(latencies))
        series.append({
            "model": model,
            "ohlc": candles,
            "average_ms": averages,
            "counts": counts,
        })
    return {
        "timestamps": timestamps,
        "window_start": cutoff,
        "window_end": now,
        "bucket_seconds": bucket_width,
        "display_start": start,
        "display_end": start + points * bucket_width,
        "series": series,
        "window_minutes": window_minutes,
        "mode": mode,
    }


def get_latency_chart_data(window_minutes: int = 1440, mode: str = "active") -> dict:
    """Use natural clock boundaries and roughly 48 candles in both clients."""
    window_minutes = max(1, min(int(window_minutes), 10080))
    target = window_minutes * 60 / 48
    intervals = (1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400)
    bucket_seconds = next((step for step in intervals if step >= target), intervals[-1])
    return get_model_latency(window_minutes, mode=mode, bucket_seconds=bucket_seconds)


def render_model_latency_png(window_minutes: int = 1440, mode: str = "active") -> bytes:
    from .latency_chart import render_latency_png
    return render_latency_png(get_latency_chart_data(window_minutes, mode))


# ── PNG 解析 ──────────────────────────────────────────────────

def _get_png_size(data: bytes) -> tuple[int, int]:
    """从 PNG 文件头的 IHDR chunk 中解析宽高。"""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return 0, 0
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def _find_png_in_bytes(data: bytes) -> tuple[int, int]:
    """在任意二进制内容中扫描 PNG 签名并解析宽高（兜底方案）。"""
    signature = b"\x89PNG\r\n\x1a\n"
    offset = data.find(signature)
    if offset < 0:
        return 0, 0
    return _get_png_size(data[offset:])


def _detect_image_size(content: bytes) -> tuple[int, int]:
    """
    从响应内容中检测图片宽高。

    依次尝试：裸 PNG → ZIP 内 PNG → 二进制流扫描。
    """
    if not content:
        return 0, 0

    # 1. 裸 PNG
    w, h = _get_png_size(content)
    if w > 0:
        return w, h

    # 2. ZIP 内的 PNG（NovelAI 常返回 application/zip）
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".png"):
                    # 只需 PNG 头 24 字节，不把整张图片再解压复制到内存。
                    with zf.open(name) as image_file:
                        w, h = _get_png_size(image_file.read(24))
                    if w > 0:
                        return w, h
    except (zipfile.BadZipFile, Exception):
        pass

    # 3. 兜底：扫描整个响应
    return _find_png_in_bytes(content)


# ── 统计记录 ──────────────────────────────────────────────────

def record_generation(content: bytes, path: str, width: int = 0, height: int = 0) -> None:
    """
    记录一次图片生成。

    Args:
        content: 上游响应体（ZIP 或 PNG）
        path: 请求路径（用于过滤非生成接口）
        width: 请求中指定的宽度（可选，0 表示需要从响应中检测）
        height: 请求中指定的高度（可选）
    """
    if "/ai/generate-image" not in path:
        return

    # 确定画幅：优先用请求参数，否则从响应内容检测
    width, height = int(width or 0), int(height or 0)
    if width <= 0 or height <= 0:
        width, height = _detect_image_size(content)

    # 分类
    is_large = (width * height) > _LARGE_THRESHOLD
    size_type = "large" if is_large else "small"
    size_label = "大图" if is_large else "小图"
    size_bytes = len(content)
    today = datetime.now().strftime("%Y-%m-%d")

    with _stats_lock:
        # 更新持久化 JSON
        stats_data = _load_stats()
        if today not in stats_data:
            stats_data[today] = {"small": 0, "large": 0}
        stats_data[today][size_type] += 1
        _save_stats(stats_data)

        # 写入日志
        current = stats_data[today]
        dim_str = f"{width}x{height}" if width > 0 else "未知"
        log_msg = (
            f"生成确认 | 类型: {size_label} | 画幅: {dim_str} | "
            f"大小: {size_bytes / 1024 / 1024:.2f}MB | "
            f"今日累计: 小图={current['small']}, 大图={current['large']}"
        )
        stats_logger.info(f"📊 {log_msg}")


def _load_stats() -> dict:
    """加载统计 JSON 文件。"""
    if not os.path.exists(STATS_JSON):
        return {}
    try:
        with open(STATS_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_stats(data: dict) -> None:
    """保存统计 JSON 文件。"""
    with open(STATS_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
