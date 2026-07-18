"""
NovelAI 透明反向代理网关 — 路由层。

职责：定义路由、分发请求、调用排队门控。
"""

import logging
import mimetypes
import platform
import secrets
import subprocess
from urllib.parse import unquote

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager

from .config import settings
from .forwarder import forward, build_response, close_client, _CORS_HEADERS
from .stats import record_generation
from .openai import (
    handle_annotate,
    handle_character_reference,
    handle_director_bg_remover,
    handle_director_colorize,
    handle_director_declutter,
    handle_director_emotion,
    handle_director_lineart,
    handle_director_sketch,
    handle_img2img,
    handle_nai_inpainting,
    handle_openai_chat_completions,
    handle_openai_generations,
    handle_openai_image_edits,
    handle_openai_models,
    handle_precise_reference,
    handle_suggest_tags,
    handle_tts,
    handle_upscale,
    handle_vibe_transfer,
    set_registry,
)
from .model_registry import ModelRegistry
from .model_fetcher import handle_refresh_upstream_models

logger = logging.getLogger("gateway")

# 不应透传给客户端的响应头（重负载请求专用，比 forwarder 多去掉 content-disposition）
_DROP_HEADERS = frozenset({
    "content-encoding", "transfer-encoding", "connection",
    "content-security-policy", "content-security-policy-report-only",
    "strict-transport-security", "x-frame-options",
    "content-disposition",
})


# ── 生命周期 ────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings.image_dir.mkdir(parents=True, exist_ok=True)

    if settings.has_shared_credentials() and not settings.gateway_password:
        if settings.allow_unauthenticated_access:
            logger.warning("⚠️ 已显式允许无鉴权使用共享 NovelAI 凭据；请勿暴露到公网")
        else:
            logger.warning("⚠️ 已配置共享 NovelAI 凭据但未设置 GATEWAY_PASSWORD；受保护 API 将返回 503")

    # 加载模型注册表
    try:
        registry = ModelRegistry(settings.models_config)
        set_registry(registry)
    except FileNotFoundError as e:
        logger.warning(f"⚠️ {e}")
        logger.warning("⚠️ 模型注册表未加载，将使用 fallback 模式")

    # 自动启动 Cloudflare Tunnel（可选）
    if settings.cloudflare_tunnel_token:
        _start_cloudflare_tunnel()

    logger.info(f"🚀 网关已启动  http://{settings.host}:{settings.port}")
    yield
    await close_client()
    logger.info("🛑 网关已关闭")


def _start_cloudflare_tunnel():
    """以管理员权限启动 cloudflared 隧道进程。"""
    logger.info("☁️ 正在尝试以管理员权限启动 Cloudflare Tunnel...")
    try:
        exe = "cloudflared.exe" if platform.system() == "Windows" else "cloudflared"
        token = settings.cloudflare_tunnel_token

        if platform.system() == "Windows":
            ps_cmd = f'powershell -Command "Start-Process {exe} -ArgumentList \'tunnel run --token {token}\' -Verb RunAs -WindowStyle Minimized"'
            subprocess.Popen(ps_cmd, shell=True)
            logger.info("✅ 已发出管理员启动指令（请检查是否有弹出权限确认窗口）")
        else:
            cmd = f"{exe} tunnel run --token {token}"
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            logger.info("✅ Cloudflare Tunnel 已在后台启动")
    except Exception as e:
        logger.error(f"❌ 启动 Cloudflare Tunnel 失败: {e}")


app = FastAPI(title="NovelAI Gateway", lifespan=lifespan)


def _gateway_auth_error(request: Request) -> Response | None:
    """校验下游访问权限；返回 None 表示允许访问。"""
    password = settings.gateway_password
    if password:
        authorization = request.headers.get("authorization", "")
        bearer = authorization[7:] if authorization.lower().startswith("bearer ") else ""
        cookie = unquote(request.cookies.get("gw_pass", ""))
        if not (
            (bearer and secrets.compare_digest(bearer, password))
            or (cookie and secrets.compare_digest(cookie, password))
        ):
            return Response(
                content='{"detail":"Unauthorized"}',
                status_code=401,
                media_type="application/json",
                headers={**_CORS_HEADERS, "WWW-Authenticate": "Bearer"},
            )
        return None

    if settings.has_shared_credentials() and not settings.allow_unauthenticated_access:
        return Response(
            content=(
                '{"detail":"Gateway authentication is required when shared NovelAI '
                'credentials are configured. Set GATEWAY_PASSWORD, or explicitly set '
                'ALLOW_UNAUTHENTICATED_ACCESS=true only for a trusted private network."}'
            ),
            status_code=503,
            media_type="application/json",
            headers=_CORS_HEADERS,
        )
    return None


@app.middleware("http")
async def protect_api_routes(request: Request, call_next):
    """保护会使用 NovelAI 凭据的 OpenAI 与透明 API 入口。"""
    path = request.url.path
    # CORS 预检本身不会使用上游凭据，也通常不会携带 Authorization。
    if request.method != "OPTIONS" and (
        path.startswith("/v1/") or path.startswith("/_api/")
    ):
        error = _gateway_auth_error(request)
        if error is not None:
            return error
    return await call_next(request)


# ── 全局异常处理（确保 CORS 头总是返回） ──────────────────────

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    import json
    return Response(
        content=json.dumps({"detail": exc.detail}),
        status_code=exc.status_code,
        media_type="application/json",
        headers=_CORS_HEADERS,
    )


# ── 静态资源 ────────────────────────────────────────────────

@app.get("/images/{filename}")
async def serve_image(filename: str):
    """本地图床：提供生成的图片访问。"""
    filepath = (settings.image_dir / filename).resolve()
    # 防止路径穿越
    if not filepath.is_relative_to(settings.image_dir.resolve()):
        raise HTTPException(status_code=403, detail="Access denied")
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="图片不存在")
    media_type = mimetypes.guess_type(filepath.name)[0] or "application/octet-stream"
    return FileResponse(filepath, media_type=media_type)


# ── OpenAI 兼容端点 ────────────────────────────────────────

@app.get("/v1/models")
async def openai_models():
    """返回支持的模型列表（从 models.toml 动态生成）。"""
    return await handle_openai_models()


@app.post("/v1/images/generations")
async def openai_generations(request: Request):
    """文生图（OpenAI 兼容）。"""
    return await handle_openai_generations(request)


@app.post("/v1/images/inpainting")
async def openai_inpainting(request: Request):
    """局部重绘（NAI SDK 格式）。"""
    return await handle_nai_inpainting(request)


@app.post("/v1/images/edits")
async def openai_image_edits(request: Request):
    """局部重绘（OpenAI 兼容格式）。"""
    return await handle_openai_image_edits(request)


@app.post("/v1/images/img2img")
async def openai_img2img(request: Request):
    """图生图接口 (action=img2img)。"""
    return await handle_img2img(request)


@app.post("/v1/images/vibe-transfer")
async def openai_vibe_transfer(request: Request):
    """Vibe Transfer 风格迁移接口。"""
    return await handle_vibe_transfer(request)

@app.post("/v1/images/character-reference")
async def openai_character_reference(request: Request):
    """角色精确参考 (Character Reference) 接口。

    每张参考图对应一个角色，配合 v4_prompt.char_captions 指定角色位置。
    V4+ 版本每张参考图编码收费 2 Anlas，超过 4 张后每张额外 2 Anlas。
    """
    return await handle_character_reference(request)


@app.post("/v1/images/precise-reference")
async def openai_precise_reference(request: Request):
    """Precise Reference (Director Reference) 接口。

    NovelAI V4.5 的 Precise Reference 功能，不走 encode-vibe，直接传原图。
    支持三种类型：character / style / character&style。
    不绑定角色提示词框，每张参考图 5 Anlas。
    """
    return await handle_precise_reference(request)


@app.post("/v1/images/upscale")
async def openai_upscale(request: Request):
    """图像放大接口 (2x/4x)。"""
    return await handle_upscale(request)


@app.post("/v1/images/annotate")
async def openai_annotate(request: Request):
    """注释图生成接口 (Canny/HED/OpenPose 等)。"""
    return await handle_annotate(request)


@app.post("/v1/images/suggest-tags")
async def openai_suggest_tags(request: Request):
    """标签建议接口。"""
    return await handle_suggest_tags(request)


# ── 导演工具 (Director Tools) ────────────────────────────────

@app.post("/v1/images/director/declutter")
async def director_declutter(request: Request):
    """导演工具 - 去杂物。"""
    return await handle_director_declutter(request)


@app.post("/v1/images/director/bg-remover")
async def director_bg_remover(request: Request):
    """导演工具 - 背景移除。"""
    return await handle_director_bg_remover(request)


@app.post("/v1/images/director/lineart")
async def director_lineart(request: Request):
    """导演工具 - 线稿提取。"""
    return await handle_director_lineart(request)


@app.post("/v1/images/director/sketch")
async def director_sketch(request: Request):
    """导演工具 - 草图化。"""
    return await handle_director_sketch(request)


@app.post("/v1/images/director/colorize")
async def director_colorize(request: Request):
    """导演工具 - 线稿上色。"""
    return await handle_director_colorize(request)


@app.post("/v1/images/director/emotion")
async def director_emotion(request: Request):
    """导演工具 - 情感迁移。"""
    return await handle_director_emotion(request)


@app.post("/v1/audio/speech")
async def openai_audio_speech(request: Request):
    """OpenAI 兼容 TTS 接口。"""
    return await handle_tts(request)


@app.post("/v1/chat/completions")
async def openai_chat(request: Request):
    """OpenAI 兼容 Chat Completions（真文本生成）。"""
    return await handle_openai_chat_completions(request)


# ── 管理端点 ────────────────────────────────────────────────

@app.post("/admin/refresh-upstream-models")
async def refresh_models(request: Request):
    """抓取 NAI 网页端 JS，解析模型 ID，写入 models_suggested.toml。需要网关密码认证。"""
    # 简单密码保护（复用 gateway_password）
    if settings.gateway_password:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {settings.gateway_password}":
            raise HTTPException(status_code=401, detail="Unauthorized")
    return await handle_refresh_upstream_models(request)


# ── CORS 预检 ──────────────────────────────────────────────

def _cors_preflight():
    return Response(status_code=204, headers=_CORS_HEADERS)


# ── NAI API 代理 ──────────────────────────────────────────────

@app.api_route(
    "/_api/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_api(request: Request, path: str):
    """NovelAI API 代理：/_api/ 开头的请求转发到 api/image.novelai.net。"""
    if request.method == "OPTIONS":
        return _cors_preflight()

    target_url = settings.get_upstream_url(f"/{path}")
    try:
        upstream = await forward(request, target_url)
        # 读取响应（需要去 content-disposition）
        await upstream.aread()
        headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _DROP_HEADERS
        }
        headers.update(_CORS_HEADERS)

        content_bytes = upstream.content

        # 图像生成相关请求记录统计
        if settings.is_heavy(f"/{path}"):
            record_generation(content_bytes, f"/{path}")

        return Response(
            content=content_bytes,
            status_code=upstream.status_code,
            headers=headers,
            media_type=upstream.headers.get("content-type", "application/octet-stream"),
        )
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        logger.error(f"❌ API 代理失败 (/{path}): {exc}")
        logger.debug("详细错误堆栈:", exc_info=True)
        raise HTTPException(status_code=502, detail=f"上游连接失败: {exc}")


@app.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def proxy_site(request: Request, path: str):
    """网站代理（兜底）：透传并注入劫持脚本。"""
    if request.method == "OPTIONS":
        return _cors_preflight()

    # 网页访问安全校验（只在开启了密码且为 GET HTML 请求时做拦截保护）
    if settings.gateway_password and request.method == "GET":
        gw_pass = request.cookies.get("gw_pass", "")
        if unquote(gw_pass) != settings.gateway_password:
            from pathlib import Path as _Path
            lock_template = _Path(__file__).parent / "templates" / "lock.html"
            if lock_template.exists():
                return HTMLResponse(content=lock_template.read_text(encoding="utf-8"), status_code=200)
            return Response(content="Gateway Locked. Please set correct gw_pass cookie.", status_code=403)

    # 非 GET 请求无法展示登录页；无密码但配置共享凭据时也必须默认拒绝。
    if request.method != "GET" or not settings.gateway_password:
        auth_error = _gateway_auth_error(request)
        if auth_error is not None:
            return auth_error

    target_url = f"{settings.novelai_base_url}/{path}"
    try:
        upstream = await forward(request, target_url)
        return await build_response(request, upstream, do_rewrite=True)
    except Exception as exc:
        if isinstance(exc, HTTPException):
            raise
        logger.error(f"❌ 站点代理失败 ({path}): {exc}")
        logger.debug("详细错误堆栈:", exc_info=True)
        raise HTTPException(status_code=502, detail=f"上游连接失败: {exc}")
