"""
模型注册表。

启动时读取 config/models.toml，构建模型映射表。
提供模型查找、列表、开关检查等功能。
"""

import tomllib
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("gateway")


@dataclass
class ModelEntry:
    type: str               # "image" | "chat" | "tts"
    model_identifier: str   # 对外暴露的模型 ID
    name: str               # NAI 内部模型名 / TTS 参数串


class ModelRegistry:
    """模型注册表，从 models.toml 加载。"""

    def __init__(self, config_path: Path):
        self._entries: dict[str, ModelEntry] = {}   # model_identifier → ModelEntry
        self._name_index: dict[str, ModelEntry] = {}  # NAI name → ModelEntry (反向查找)
        self._image_enabled: bool = True
        self._chat_enabled: bool = False
        self._tts_enabled: bool = False
        self._load(config_path)

    def _load(self, config_path: Path):
        """加载 models.toml。"""
        if not config_path.exists():
            # 尝试从 example 复制
            example_path = config_path.with_suffix(".toml.example")
            if not example_path.exists():
                example_path = config_path.parent / "models.toml.example"
            if example_path.exists():
                import shutil
                shutil.copy(example_path, config_path)
                logger.info(f"📋 已从 {example_path} 复制创建 {config_path}")
            else:
                raise FileNotFoundError(
                    f"models.toml not found at {config_path}. "
                    f"Please create it. See config/models.toml.example for reference."
                )

        try:
            with open(config_path, "rb") as f:
                data = tomllib.load(f)
        except Exception as e:
            logger.error(f"❌ models.toml 解析失败: {e}")
            raise

        # 读取开关
        self._image_enabled = data.get("image_enabled", True)
        self._chat_enabled = data.get("chat_enabled", False)
        self._tts_enabled = data.get("tts_enabled", False)

        # 读取模型
        for section_key, section_type in [
            ("image", "image"),
            ("chat", "chat"),
            ("tts", "tts"),
        ]:
            section = data.get(section_key, {})
            models_list = section.get("models", []) if isinstance(section, dict) else []
            for entry in models_list:
                try:
                    model_id = entry["model_identifier"]
                    model_entry = ModelEntry(
                        type=entry.get("type", section_type),
                        model_identifier=model_id,
                        name=entry["name"],
                    )
                    self._entries[model_id] = model_entry
                    # 为图像模型建立 name → entry 反向索引（向后兼容）
                    if model_entry.type == "image":
                        self._name_index[model_entry.name] = model_entry
                except KeyError as e:
                    logger.warning(f"⚠️ models.toml 条目缺少字段 {e}，已跳过: {entry}")

        logger.info(
            f"📦 模型注册表已加载: {len(self._entries)} 个模型 "
            f"(image={self._image_enabled}, chat={self._chat_enabled}, tts={self._tts_enabled})"
        )

    def lookup(self, model_identifier: str) -> Optional[ModelEntry]:
        """根据 model_identifier 查找模型。"""
        return self._entries.get(model_identifier)

    def lookup_by_name(self, nai_name: str) -> Optional[ModelEntry]:
        """根据 NAI 内部名查找（向后兼容旧调用）。"""
        return self._name_index.get(nai_name)

    def resolve_image_model(self, model: str) -> str:
        """
        解析图像模型名，返回 NAI 内部名。
        支持 model_identifier 或直接传 NAI 内部名（向后兼容）。
        """
        # 优先查 model_identifier
        entry = self.lookup(model)
        if entry and entry.type == "image":
            return entry.name
        # 向后兼容：直接传入 NAI 内部名
        if self.lookup_by_name(model):
            return model
        # 最终 fallback：直接使用传入值（兼容未在 toml 中配置的模型）
        return model

    def is_enabled(self, model_type: str) -> bool:
        """检查某类模型是否启用。"""
        if model_type == "image":
            return self._image_enabled
        elif model_type == "chat":
            return self._chat_enabled
        elif model_type == "tts":
            return self._tts_enabled
        return False

    def list_models(self) -> list[dict]:
        """返回所有启用的模型列表（用于 /v1/models）。"""
        result = []
        for entry in self._entries.values():
            if not self.is_enabled(entry.type):
                continue
            result.append({
                "id": entry.model_identifier,
                "object": "model",
                "created": 1700000000,
                "owned_by": "novelai",
            })
        return result
