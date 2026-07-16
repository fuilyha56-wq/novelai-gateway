"""
生成统计模块。

记录每次图片生成的画幅、大小，并按日汇总到 JSON 文件。
分类标准：像素数 > 1,048,576（即超过 1024x1024）为大图。
"""

import io
import json
import logging
import os
import struct
import zipfile
from datetime import datetime
from threading import Lock

# ── 初始化 ─────────────────────────────────────────────────

os.makedirs("logs", exist_ok=True)

stats_logger = logging.getLogger("stats")
stats_logger.setLevel(logging.INFO)
if not stats_logger.handlers:
    fh = logging.FileHandler("logs/stats.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s - %(message)s"))
    stats_logger.addHandler(fh)

STATS_JSON = "logs/stats_summary.json"
_stats_lock = Lock()

# 大图阈值：超过 1M 像素
_LARGE_THRESHOLD = 1024 * 1024


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
                    w, h = _get_png_size(zf.read(name))
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
        stats_logger.info(log_msg)
        try:
            print(f"📊 {log_msg}")
        except UnicodeEncodeError:
            # Windows GBK 控制台可能无法输出 emoji，降级为纯文本
            print(f"[stats] {log_msg}")


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
