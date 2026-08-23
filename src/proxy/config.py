"""
网关配置。

通过 .env 文件或环境变量覆盖默认值。
使用 pydantic-settings 自动加载。
"""

import json
from pathlib import Path
from threading import Lock
from typing import Any, Set

from pydantic_settings import BaseSettings, SettingsConfigDict

from .account_pool import account_pool


def _normalize_credential(value: str) -> str:
    """清理凭据文本的首尾空白和外层引号。"""
    return value.strip().strip("'\"")


def _parse_api_keys(value: str) -> list[str]:
    """解析 JSON 数组或逗号、分号、换行分隔的持久 API Key 列表。"""
    normalized = value.strip()
    if not normalized:
        return []

    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        return [
            _normalize_credential(item)
            for item in parsed
            if isinstance(item, str) and _normalize_credential(item)
        ]

    return [
        _normalize_credential(item)
        for item in normalized.replace(";", ",").replace("\n", ",").split(",")
        if _normalize_credential(item)
    ]


def _parse_weighted_api_keys(value: str) -> list[tuple[str, int]]:
    """解析带权重的 API Key 配置，兼容字符串数组和对象数组。"""
    normalized = value.strip()
    if not normalized:
        return []
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        parsed = None
    if not isinstance(parsed, list):
        return [(key, 1) for key in _parse_api_keys(value)]
    result: list[tuple[str, int]] = []
    for item in parsed:
        if isinstance(item, str):
            key = _normalize_credential(item)
            if key:
                result.append((key, 1))
        elif isinstance(item, dict):
            key = item.get("key", item.get("token", ""))
            if isinstance(key, str) and (key := _normalize_credential(key)):
                try:
                    weight = max(1, int(item.get("weight", 1)))
                except (TypeError, ValueError):
                    weight = 1
                if item.get("enabled", True):
                    result.append((key, weight))
    return result


class Settings(BaseSettings):
    """网关全局配置。"""

    # 服务监听
    host: str = "0.0.0.0"
    port: int = 31555

    # 模型配置文件路径
    models_config: Path = Path("config/models.toml")

    # 本地图床
    image_dir: Path = Path("images")
    image_base_url: str = ""  # 留空则根据请求 Host 自动生成

    # Cloudflare Tunnel（可选）
    cloudflare_tunnel_token: str = ""

    # 共享的 NovelAI Session Token（JSON 格式字符串）
    shared_token: str = ""

    # 共享的 NovelAI 持久 API Key（与 SHARED_TOKEN 二选一，API Key 优先）
    shared_api_key: str = ""

    # 多个持久 API Key。配置后优先于 SHARED_API_KEY，按请求轮询。
    shared_api_keys: str = ""

    # 网页端访问密码保护（留空则不开启密码拦截）
    gateway_password: str = ""

    # 显式允许在配置共享 NovelAI 凭据时关闭下游鉴权。
    # 默认关闭，避免公网部署意外暴露共享账户。
    allow_unauthenticated_access: bool = False

    # NovelAI 上游地址
    novelai_base_url: str = "https://novelai.net"
    novelai_api_url: str = "https://api.novelai.net"
    novelai_image_url: str = "https://image.novelai.net"
    novelai_text_url: str = "https://text.novelai.net"

    # 需要排队的重负载 API 路径前缀
    heavy_prefixes: Set[str] = {
        "/ai/generate-image",
        "/ai/upscale",
        "/ai/generate-voice",
    }

    # 并发与冷却
    max_concurrent: int = 1
    queue_timeout: int = 300
    cooldown_min: float = 0.5
    cooldown_max: float = 1.0
    upstream_timeout: float = 120.0

    # V5 图片生成限额
    v5_quota_enabled: bool = True
    v5_daily_limit: int = 190
    v5_weekly_limit: int = 1730

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def model_post_init(self, __context: Any) -> None:
        """初始化不参与配置序列化的 Key 轮询状态。"""
        self._api_key_lock = Lock()
        self._api_key_index = 0
        account_pool.configure(self.shared_api_keys)

    def get_shared_auth_token(self) -> str:
        """返回下一把共享凭据，多 API Key 时按请求进行线程安全轮询。"""
        if self.shared_api_keys.strip():
            _account_id, token = account_pool.choose()
            return token

        if self.shared_api_key:
            return _normalize_credential(self.shared_api_key)

        if self.shared_token:
            token_str = _normalize_credential(self.shared_token)
            try:
                parsed = json.loads(token_str)
            except json.JSONDecodeError:
                return token_str
            if isinstance(parsed, dict):
                token = parsed.get("auth_token", token_str)
                return token if isinstance(token, str) else token_str
            return token_str

        return ""

    def has_shared_credentials(self) -> bool:
        """是否配置了会被所有下游请求共用的 NovelAI 凭据。"""
        return bool(
            _parse_weighted_api_keys(self.shared_api_keys)
            or _normalize_credential(self.shared_api_key)
            or _normalize_credential(self.shared_token)
        )

    def is_heavy(self, path: str) -> bool:
        """判断路径是否为重负载请求。"""
        return any(path.startswith(p) for p in self.heavy_prefixes)

    def get_upstream_url(self, api_path: str) -> str:
        """根据 API 路径选择对应的上游服务器。

        image.novelai.net: 图片生成、标签建议、Vibe 编码、导演工具
        api.novelai.net: 放大、图片标注、用户数据、TTS
        text.novelai.net: 文本生成
        """
        if api_path == "/ai/generate-voice":
            return f"{self.novelai_api_url}{api_path}"
        if api_path.startswith((
            "/ai/generate-image",
            "/ai/augment-image",
            "/ai/encode-vibe",
        )):
            return f"{self.novelai_image_url}{api_path}"
        if api_path in {"/ai/generate", "/ai/generate-stream"}:
            return f"{self.novelai_text_url}{api_path}"
        return f"{self.novelai_api_url}{api_path}"


def get_request_auth_token(request: Any) -> str:
    """为一个下游请求选择并缓存共享凭据，避免内部子请求跨 Key。"""
    cached_token = getattr(request.state, "gateway_auth_token", None)
    if isinstance(cached_token, str) and cached_token:
        return cached_token

    if settings.shared_api_keys.strip():
        account_id = getattr(request.state, "gateway_account_id", None)
        if account_id:
            token = account_pool.get_secret(account_id)
        else:
            account_id, token = account_pool.choose()
            request.state.gateway_account_id = account_id
    else:
        token = settings.get_shared_auth_token()
    if not token:
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else auth

    if token:
        request.state.gateway_auth_token = token
    return token


settings = Settings()
