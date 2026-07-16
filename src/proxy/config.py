"""
网关配置。

通过 .env 文件或环境变量覆盖默认值。
使用 pydantic-settings 自动加载。
"""

from pathlib import Path
from typing import Set

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # 网页端访问密码保护（留空则不开启密码拦截）
    gateway_password: str = ""

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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def is_heavy(self, path: str) -> bool:
        """判断路径是否为重负载请求。"""
        return any(path.startswith(p) for p in self.heavy_prefixes)

    def get_upstream_url(self, api_path: str) -> str:
        """根据 API 路径选择对应的上游服务器。

        image.novelai.net: 图像生成、标签建议、Vibe 编码、导演工具(augment-image)
        api.novelai.net: 放大、annotate-image、用户数据、TTS
        text.novelai.net: 文本生成

        参考: NAI 官网 JS (_app-*.js) 中 BackendUrl/ImageBackendUrl 的定义:
        - AnnotateImage = BackendUrl + "/ai/annotate-image"  → api.novelai.net
        - ImageTool     = ImageBackendUrl + "/ai/augment-image" → image.novelai.net
        - GenerateImage = ImageBackendUrl + "/ai/generate-image" → image.novelai.net
        - TagSearch     = ImageBackendUrl + "/ai/generate-image/suggest-tags" → image.novelai.net
        - EncodeVibe    = ImageBackendUrl + "/ai/encode-vibe" → image.novelai.net
        - UpscaleImage  = BackendUrl + "/ai/upscale" → api.novelai.net
        - TextGenerate  = TextBackendUrl + "/ai/generate" → text.novelai.net
        """
        if api_path.startswith(("/ai/generate", "/ai/generate-stream")):
            return f"{self.novelai_text_url}{api_path}"
        if api_path.startswith((
            "/ai/generate-image",
            "/ai/augment-image",
            "/ai/encode-vibe",
        )):
            return f"{self.novelai_image_url}{api_path}"
        return f"{self.novelai_api_url}{api_path}"


settings = Settings()
