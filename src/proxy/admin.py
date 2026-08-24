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

from .account_pool import account_pool, mask_secret, parse_accounts, serialize_accounts
from .config import _parse_weighted_api_keys, settings
from .model_registry import ModelRegistry
from .model_fetcher import handle_refresh_upstream_models
from .v5_quota import get_usage

router = APIRouter(prefix="/admin/api")
_logger = logging.getLogger("gateway")
_ENV_PATH = Path(".env")
_LOG_BUFFER: list[str] = []
_LOG_PATH = Path("logs/gateway.log")


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
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
_file_handler = logging.FileHandler(_LOG_PATH, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"))
logging.getLogger().addHandler(_file_handler)


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
    return {"env": safe, "models": _models(), "usage": get_usage(), "accounts": account_pool.public()}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """返回控制台概览数据（与 /overview 兼容的别名）。"""
    return await overview(request)


async def _choose_upstream_account(account_id: str | None) -> tuple[str, str]:
    """选择用于探测 NovelAI 上游的账号。

    返回 (account_id, secret)。优先使用账号池；未配置账号池时回退到共享凭据。
    """
    if account_id:
        secret = account_pool.get_secret(account_id)
        if not secret:
            raise HTTPException(status_code=404, detail="账号不存在")
        return account_id, secret

    if account_pool._accounts:
        return account_pool.choose()

    secret = settings.get_shared_auth_token()
    if secret:
        return "shared", secret

    raise HTTPException(status_code=503, detail="未配置 NovelAI 凭据")


async def _fetch_upstream(path: str, account_id: str | None) -> dict[str, Any]:
    """代理请求 NovelAI 上游用户接口并附加账号元数据。"""
    selected_id, secret = await _choose_upstream_account(account_id)
    target = f"{settings.novelai_image_url}{path}"
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                target,
                headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
            )
    except Exception as exc:
        account_pool.failure(selected_id, str(exc)[:300])
        raise HTTPException(status_code=502, detail=f"上游连接失败: {exc}") from exc

    if response.status_code >= 400:
        account_pool.failure(selected_id, f"HTTP {response.status_code}")
        raise HTTPException(
            status_code=response.status_code,
            detail=response.text[:500] or "上游请求失败",
        )

    account_pool.success(selected_id)
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": response.text}

    return {
        "account_id": selected_id,
        "masked_key": mask_secret(secret),
        "upstream": target,
        "data": payload,
    }


@router.get("/upstream/user/data")
async def upstream_user_data(request: Request, account_id: str | None = None) -> dict[str, Any]:
    """代理获取 NovelAI /user/data（包含任务优先级与额度百分比）。"""
    await _require_auth(request)
    return await _fetch_upstream("/user/data", account_id)


@router.get("/upstream/subscription")
async def upstream_subscription(request: Request, account_id: str | None = None) -> dict[str, Any]:
    """代理获取 NovelAI /user/subscription（包含订阅等级、Anlas 数量）。"""
    await _require_auth(request)
    return await _fetch_upstream("/user/subscription", account_id)


def _persist_accounts(accounts: list[dict[str, Any]]) -> None:
    """持久化账号并让当前进程立即使用新账号池。"""
    encoded = serialize_accounts(accounts)
    _write_env({"SHARED_API_KEYS": encoded})
    settings.shared_api_keys = encoded
    account_pool.configure(encoded)


@router.get("/accounts")
async def list_accounts(request: Request) -> dict[str, Any]:
    """返回脱敏账号状态。"""
    await _require_auth(request)
    return {"accounts": account_pool.public()}


@router.post("/accounts")
async def create_account(request: Request) -> dict[str, Any]:
    """创建账号。"""
    await _require_auth(request)
    body = await request.json()
    secret = body.get("key", body.get("token", ""))
    if not isinstance(secret, str) or not secret.strip():
        raise HTTPException(status_code=400, detail="账号密钥不能为空")
    accounts = parse_accounts(settings.shared_api_keys)
    account_id = str(body.get("id", f"account-{len(accounts) + 1}"))
    if any(item.get("id") == account_id for item in accounts):
        raise HTTPException(status_code=409, detail="账号 ID 已存在")
    accounts.append({"id": account_id, "name": body.get("name", account_id), "key": secret, "weight": body.get("weight", 1), "enabled": body.get("enabled", True)})
    _persist_accounts(accounts)
    return {"account": next(item for item in account_pool.public() if item["id"] == account_id)}


@router.put("/accounts/{account_id}")
async def update_account(account_id: str, request: Request) -> dict[str, Any]:
    """编辑账号元数据或密钥。"""
    await _require_auth(request)
    body = await request.json()
    accounts = parse_accounts(settings.shared_api_keys)
    target = next((item for item in accounts if item.get("id") == account_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    for key in ("name", "weight", "enabled"):
        if key in body:
            target[key] = body[key]
    if isinstance(body.get("key"), str) and body["key"].strip() and not body["key"].startswith("••••"):
        target["key"] = body["key"]
    _persist_accounts(accounts)
    return {"account": next(item for item in account_pool.public() if item["id"] == account_id)}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: str, request: Request) -> dict[str, str]:
    """删除账号。"""
    await _require_auth(request)
    accounts = [item for item in parse_accounts(settings.shared_api_keys) if item.get("id") != account_id]
    if len(accounts) == len(parse_accounts(settings.shared_api_keys)):
        raise HTTPException(status_code=404, detail="账号不存在")
    _persist_accounts(accounts)
    return {"message": "账号已删除"}


@router.post("/accounts/{account_id}/reset")
async def reset_account(account_id: str, request: Request) -> dict[str, str]:
    """清除账号失败和冷却状态。"""
    await _require_auth(request)
    if not account_pool.reset(account_id):
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"message": "账号状态已重置"}


@router.post("/accounts/{account_id}/test")
async def test_account(account_id: str, request: Request) -> dict[str, Any]:
    """调用轻量上游接口测试账号有效性。"""
    await _require_auth(request)
    secret = account_pool.get_secret(account_id)
    if not secret:
        raise HTTPException(status_code=404, detail="账号不存在")
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{settings.novelai_image_url}/user/data",
                headers={"Authorization": f"Bearer {secret}"},
                timeout=15,
            )
        if response.status_code >= 400:
            account_pool.failure(account_id, f"HTTP {response.status_code}: {response.text[:180]}")
            return {"ok": False, "status_code": response.status_code, "message": "上游拒绝该账号"}
        account_pool.success(account_id)
        return {"ok": True, "status_code": response.status_code, "message": "账号可用"}
    except Exception as exc:
        account_pool.failure(account_id, str(exc))
        return {"ok": False, "message": "账号测试失败"}


@router.get("/env")
async def get_env(request: Request) -> dict[str, Any]:
    """返回允许管理的环境变量。"""
    await _require_auth(request)
    env = _env_values()
    safe = {key: (_safe_env(value) if any(word in key for word in ("KEY", "TOKEN", "PASSWORD")) else value) for key, value in env.items()}
    return {"env": safe}


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
    if "SHARED_API_KEYS" in values:
        settings.shared_api_keys = values["SHARED_API_KEYS"]
        account_pool.configure(settings.shared_api_keys)
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
    limit = max(1, min(lines, 500))
    if _LOG_PATH.exists():
        return {"lines": _LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]}
    return {"lines": _LOG_BUFFER[-limit:]}


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