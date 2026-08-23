"""网关管理控制台 API 与内置静态页面。"""

import asyncio
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from .config import _parse_weighted_api_keys, settings
from .model_registry import ModelRegistry
from .model_fetcher import handle_refresh_upstream_models
from .v5_quota import get_usage

router = APIRouter(prefix="/admin/api")
_logger = logging.getLogger("gateway")
_ENV_PATH = Path(".env")
_LOG_BUFFER: list[str] = []


class _AdminLogHandler(logging.Handler):
    """将网关日志保留在内存中供控制台读取。"""

    def emit(self, record: logging.LogRecord) -> None:
        """保存格式化后的单行日志。"""
        try:
            _LOG_BUFFER.append(self.format(record))
            del _LOG_BUFFER[:-500]
        except Exception:
            pass


_log_handler = _AdminLogHandler()
_log_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%H:%M:%S"))
_logger.addHandler(_log_handler)


def _authorized(request: Request) -> bool:
    """校验控制台使用的网关密码。"""
    if not settings.gateway_password:
        return False
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    return secrets.compare_digest(token, settings.gateway_password)


async def _require_auth(request: Request) -> None:
    """拒绝未认证管理请求。"""
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="管理控制台需要 GATEWAY_PASSWORD")


def _env_values() -> dict[str, str]:
    """读取 .env，隐藏凭据值。"""
    if not _ENV_PATH.exists():
        return {}
    result: dict[str, str] = {}
    for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            key, value = match.groups()
            result[key] = value
    return result


def _write_env(values: dict[str, str]) -> None:
    """更新 .env 中的键值，不改动未提交的其他行。"""
    existing = _ENV_PATH.read_text(encoding="utf-8").splitlines() if _ENV_PATH.exists() else []
    seen: set[str] = set()
    output: list[str] = []
    for line in existing:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match and match.group(1) in values:
            key = match.group(1)
            output.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in seen:
            output.append(f"{key}={value}")
    _ENV_PATH.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def _safe_env(value: str) -> str:
    """对敏感配置做脱敏。"""
    return "••••••••" if value else ""


def _models() -> list[dict[str, Any]]:
    """返回当前模型注册表条目。"""
    registry = ModelRegistry(settings.models_config)
    return [
        {
            "id": entry.model_identifier,
            "name": entry.name,
            "type": entry.type,
            "enabled": registry.is_enabled(entry.type),
        }
        for entry in registry._entries.values()
    ]


@router.get("/session")
async def session(request: Request) -> dict[str, Any]:
    """验证管理密码并返回会话信息。"""
    await _require_auth(request)
    return {"authenticated": True, "port": settings.port}


@router.get("/overview")
async def overview(request: Request) -> dict[str, Any]:
    """返回控制台概览数据。"""
    await _require_auth(request)
    env = _env_values()
    safe = {key: (_safe_env(value) if any(word in key for word in ("KEY", "TOKEN", "PASSWORD")) else value) for key, value in env.items()}
    return {"env": safe, "models": _models(), "usage": get_usage(), "accounts": len(_parse_weighted_api_keys(settings.shared_api_keys))}


@router.put("/env")
async def update_env(request: Request) -> dict[str, str]:
    """保存允许管理的环境变量，凭据必须显式传入才覆盖。"""
    await _require_auth(request)
    body = await request.json()
    allowed = {
        "IMAGE_BASE_URL", "HOST", "PORT", "MAX_CONCURRENT", "COOLDOWN_MIN", "COOLDOWN_MAX",
        "V5_QUOTA_ENABLED", "V5_DAILY_LIMIT", "V5_WEEKLY_LIMIT", "SHARED_API_KEYS",
    }
    values = {key: str(value) for key, value in body.items() if key in allowed}
    _write_env(values)
    return {"message": "配置已保存，重启网关后生效", "updated": ",".join(values)}


@router.post("/models/refresh")
async def refresh_models(request: Request) -> Any:
    """抓取 NAI 上游模型。"""
    await _require_auth(request)
    return await handle_refresh_upstream_models(request)


@router.post("/restart")
async def restart(request: Request) -> dict[str, str]:
    """请求容器通过 restart policy 自动重启。"""
    await _require_auth(request)
    asyncio.get_running_loop().call_later(0.5, os._exit, 0)
    return {"message": "网关正在重启"}


@router.get("/logs")
async def logs(request: Request, lines: int = 100) -> dict[str, list[str]]:
    """读取最近网关日志。"""
    await _require_auth(request)
    return {"lines": _LOG_BUFFER[-max(1, min(lines, 500)): ]}


@router.get("/models/config")
async def model_config(request: Request) -> dict[str, str]:
    """读取模型 TOML 原文，供别名和可调用模型编辑。"""
    await _require_auth(request)
    return {"content": settings.models_config.read_text(encoding="utf-8")}


@router.put("/models/config")
async def update_model_config(request: Request) -> dict[str, str]:
    """校验并保存模型 TOML。"""
    await _require_auth(request)
    body = await request.json()
    content = body.get("content")
    if not isinstance(content, str) or len(content) > 256_000:
        raise HTTPException(status_code=400, detail="模型配置内容无效或过大")
    import tomllib
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"TOML 解析失败: {exc}") from exc
    settings.models_config.write_text(content.rstrip() + "\n", encoding="utf-8")
    return {"message": "模型配置已保存，重启后生效"}


@router.get("/ui")
async def admin_ui() -> FileResponse:
    """返回内置控制台页面。"""
    return FileResponse(Path(__file__).parent / "templates" / "admin.html")