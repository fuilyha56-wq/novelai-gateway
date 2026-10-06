"""
生成统计模块。

记录每次图片生成的画幅、大小，并按日汇总到 JSON 文件。
分类标准：像素数 > 1,048,576（即超过 1024x1024）为大图。
"""

import io
import json
import logging
from logging.handlers import RotatingFileHandler
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
_latency_lock = Lock()


def record_model_latency(model: str, elapsed_seconds: float) -> None:
    model = model.removesuffix("-limit")
    if not any(family in model for family in ("diffusion-4-5", "diffusion-5", "v4.5", "v5")):
        return
    minute = int(time.time() // 60)
    with _latency_lock:
        data = _load_latency()
        data = {key: value for key, value in data.items() if int(key) >= minute - 7 * 24 * 60}
        bucket = data.setdefault(str(minute), {}).setdefault(model, {"count": 0, "total_ms": 0.0})
        bucket["count"] += 1
        bucket["total_ms"] += max(0.0, elapsed_seconds) * 1000
        temporary = LATENCY_JSON + ".tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
        os.replace(temporary, LATENCY_JSON)


def _load_latency() -> dict:
    try:
        with open(LATENCY_JSON, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {}


def get_model_latency(window_minutes: int = 24 * 60, points: int = 24) -> dict:
    window_minutes = max(1, min(window_minutes, 7 * 24 * 60))
    points = max(2, points)
    now_minute = int(time.time() // 60)
    start_minute = now_minute - window_minutes + 1
    bucket_width = window_minutes / points
    buckets = [
        (start_minute + index * bucket_width, start_minute + (index + 1) * bucket_width)
        for index in range(points)
    ]
    with _latency_lock:
        data = _load_latency()
    models = sorted({model for key in data if start_minute <= int(key) <= now_minute for model in data[key]})
    series = []
    for model in models:
        model_buckets = []
        for bucket_start, bucket_end in buckets:
            first_minute = int(bucket_start)
            last_minute = max(first_minute, int(bucket_end - 1e-9))
            if first_minute == last_minute:
                sample_minute = int(round((bucket_start + bucket_end) / 2))
                minute_keys = [sample_minute]
            else:
                minute_keys = range(first_minute, last_minute + 1)
            entries = [data.get(str(key), {}).get(model, {}) for key in minute_keys]
            model_buckets.append({
                "count": sum(entry.get("count", 0) for entry in entries),
                "total_ms": sum(entry.get("total_ms", 0.0) for entry in entries),
            })
        series.append({
            "model": model,
            "average_ms": [bucket["total_ms"] / bucket["count"] if bucket["count"] else None for bucket in model_buckets],
            "counts": [bucket["count"] for bucket in model_buckets],
        })
    return {"timestamps": [bucket_start * 60 for bucket_start, _ in buckets], "series": series, "window_minutes": window_minutes}


def render_model_latency_png(window_minutes: int = 24 * 60) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    data = get_model_latency(window_minutes=window_minutes, points=24)
    series = data["series"]
    width = 1600
    height = 940 + max(1, (len(series) + 1) // 2) * 60
    scale = 3
    image = Image.new("RGB", (width * scale, height * scale), "white")
    draw = ImageDraw.Draw(image)
    font_path = os.path.join(os.path.dirname(__file__), "templates", "NotoSansSC.ttf")
    font = ImageFont.truetype(font_path, 26 * scale)
    title_font = ImageFont.truetype(font_path, 42 * scale)
    font.set_variation_by_axes([400])
    title_font.set_variation_by_axes([600])

    def text(position, content, fill="#738096", selected_font=font):
        draw.text(tuple(value * scale for value in position), content, fill=fill, font=selected_font)

    def line(coordinates, fill, thickness=1):
        draw.line(tuple(value * scale for value in coordinates), fill=fill, width=thickness * scale)

    text((56, 35), "NovelAI 模型调用耗时", "#172033", title_font)
    window_label = f"最近 {window_minutes} 分钟" if window_minutes < 60 else f"最近 {window_minutes / 60:g} 小时"
    text((56, 105), f"{window_label} · 平均上游耗时 · 北京时间（UTC+8）· 共24个时间点")
    text((56, 158), "平均耗时（秒）")
    left, top, right, bottom = 150, 225, 1530, 780
    values = [value / 1000 for item in series for value in item["average_ms"] if value is not None]
    maximum = max(1, max(values, default=1)) * 1.15
    for tick in range(5):
        position = bottom - (bottom - top) * tick / 4
        for dash in range(left, right, 14):
            line((dash, position, dash + 6, position), "#dce2ea")
        text((45, position - 19), f"{maximum * tick / 4:.1f}")
    line((left, bottom, right, bottom), "#bdc7d6")
    from datetime import timedelta, timezone

    for index, timestamp in enumerate(data["timestamps"]):
        if index % 3 == 0 or index == 23:
            label = datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%H:%M:%S")
            text((left + index * (right - left) / 23 - 46, bottom + 20), label)
    colors = ["#3186ff", "#14a36e", "#e89b22", "#e65a68", "#18aabc", "#b45cc6", "#7a9824", "#936944"]
    point_font = ImageFont.truetype(font_path, 20 * scale)
    point_font.set_variation_by_axes([500])
    point_labels = []
    for model_index, item in enumerate(series):
        color = colors[model_index % len(colors)]
        previous = None
        for index, value in enumerate(item["average_ms"]):
            if value is None:
                previous = None
                continue
            point = (left + index * (right - left) / 23, bottom - value / 1000 / maximum * (bottom - top))
            if previous is not None:
                line((*previous, *point), color, 4)
            draw.ellipse(tuple(value * scale for value in (point[0] - 5, point[1] - 5, point[0] + 5, point[1] + 5)), fill=color)
            point_labels.append((point, f"{value / 1000:.2f} 秒", color))
            previous = point
        legend_left = 56 + (model_index % 2) * 770
        legend_top = 890 + (model_index // 2) * 60
        line((legend_left, legend_top + 20, legend_left + 38, legend_top + 20), color, 4)
        text((legend_left + 52, legend_top), item["model"], "#172033")
    occupied = []
    for point, label, color in point_labels:
        label_width = draw.textlength(label, font=point_font) / scale
        label_left = max(left, min(right - label_width, point[0] - label_width / 2))
        for offset in (-38, 14, -68, 44, -98, 74):
            label_top = max(top - 32, min(bottom - 28, point[1] + offset))
            rectangle = (label_left - 3, label_top, label_left + label_width + 3, label_top + 30)
            if not any(rectangle[0] < area[2] and rectangle[2] > area[0] and rectangle[1] < area[3] and rectangle[3] > area[1] for area in occupied):
                break
        occupied.append(rectangle)
        draw.text((label_left * scale, label_top * scale), label, fill=color, font=point_font,
                  stroke_width=2 * scale, stroke_fill="white")
    if not values:
        text((480, 470), "暂无成功调用的耗时数据", selected_font=title_font)
    text((56, height - 54), "空白时段表示暂无调用记录；limit 模型已合并至对应普通模型。")
    image = image.resize((width, height), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


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
