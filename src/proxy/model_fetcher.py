"""
NAI 上游模型抓取器。

从 novelai.net 网页端 JS bundle 中解析可用的模型 ID，
写入 config/models_suggested.toml 供用户参考。
"""

import re
import logging
from pathlib import Path

from fastapi import Request, Response
from .forwarder import get_client
from .config import settings

logger = logging.getLogger("gateway")

# 匹配图像模型 ID 的正则
_IMAGE_MODEL_RE = re.compile(r'"(nai-diffusion[\w-]*)"')


async def handle_refresh_upstream_models(request: Request) -> Response:
    """
    抓取 NAI 网页端 JS bundle，解析模型 ID，写入 models_suggested.toml。
    """
    import json

    try:
        client = await get_client()

        # 1. 拉取主页 HTML
        resp = await client.get(
            f"{settings.novelai_base_url}/",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        html = resp.text

        # 2. 提取 JS bundle URL
        js_urls = re.findall(r'src="(/_astro/[^"]+\.js)"', html)
        if not js_urls:
            js_urls = re.findall(r'src="(/[^"]+\.js)"', html)

        discovered_models: set[str] = set()

        # 3. 遍历 JS bundle 查找模型 ID
        for js_path in js_urls[:20]:  # 限制数量避免过多请求
            js_url = f"{settings.novelai_base_url}{js_path}"
            try:
                js_resp = await client.get(js_url, headers={"User-Agent": "Mozilla/5.0"})
                if js_resp.status_code == 200:
                    matches = _IMAGE_MODEL_RE.findall(js_resp.text)
                    discovered_models.update(matches)
            except Exception:
                continue

        if not discovered_models:
            return Response(
                content=json.dumps({"error": "No models found in JS bundles", "searched": len(js_urls)}),
                status_code=404,
                media_type="application/json",
            )

        # 4. 生成 suggested toml
        lines = [
            "# 自动生成的建议模型配置",
            "# 由 /admin/refresh-upstream-models 端点生成",
            "# 将需要的条目复制到 models.toml 中使用",
            "",
            "image_enabled = true",
            "chat_enabled = false",
            "tts_enabled = false",
            "",
        ]

        sorted_models = sorted(discovered_models)
        for model_name in sorted_models:
            # 生成 model_identifier（简化名）
            identifier = model_name.replace("nai-diffusion-", "nai-v").replace("-full", "-full").replace("-curated", "-curated")
            lines.append("[[image.models]]")
            lines.append('type = "image"')
            lines.append(f'model_identifier = "{identifier}"')
            lines.append(f'name = "{model_name}"')
            lines.append("")

            # 自动添加 inpainting 变体
            inpaint_name = f"{model_name}-inpainting"
            lines.append("[[image.models]]")
            lines.append('type = "image"')
            lines.append(f'model_identifier = "{identifier}-inpaint"')
            lines.append(f'name = "{inpaint_name}"')
            lines.append("")

        # 5. 写入文件
        suggested_path = Path("config/models_suggested.toml")
        suggested_path.parent.mkdir(parents=True, exist_ok=True)
        suggested_path.write_text("\n".join(lines), encoding="utf-8")

        result = {
            "message": "Models refreshed successfully",
            "models_found": sorted_models,
            "count": len(sorted_models),
            "output_file": str(suggested_path),
        }

        logger.info(f"🔄 模型抓取完成: 发现 {len(sorted_models)} 个模型")
        return Response(
            content=json.dumps(result, ensure_ascii=False),
            status_code=200,
            media_type="application/json",
        )

    except Exception as e:
        logger.error(f"❌ 模型抓取失败: {e}")
        return Response(
            content=json.dumps({"error": str(e)}),
            status_code=500,
            media_type="application/json",
        )
