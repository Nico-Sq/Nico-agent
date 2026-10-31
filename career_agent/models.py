"""Local model choices; API keys stay in environment variables."""

import json
import os
from pathlib import Path

from .provider import Provider


class ModelCatalog:
    def __init__(self, path: Path | None = None):
        self.models: dict[str, Provider] = {"default": Provider()}
        if path is None:
            configured = os.getenv("MODEL_CATALOG_PATH")
            path = Path(configured) if configured else None
        if path:
            entries = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(entries, list):
                raise ValueError("模型目录必须是 JSON 数组")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"].strip():
                    raise ValueError("模型目录中的每项都需要 id")
                model_id = entry["id"].strip()
                if model_id in self.models:
                    raise ValueError(f"模型 ID 重复：{model_id}")
                if entry.get("supports_tools") is not True:
                    raise ValueError(f"模型 {model_id} 必须声明支持工具调用")
                key_name = entry.get("api_key_env", "MODEL_API_KEY")
                if not isinstance(key_name, str) or not key_name.startswith("MODEL_"):
                    raise ValueError("api_key_env 必须是 MODEL_ 开头的环境变量名")
                provider = Provider(mode=entry.get("provider"), model=entry.get("model"),
                                    base_url=entry.get("base_url"), api_key=os.getenv(key_name, ""))
                error = provider.configuration_error()
                if error:
                    raise ValueError(f"模型 {model_id} 配置无效：{error}")
                self.models[model_id] = provider

    def get(self, model_id: str) -> Provider:
        try:
            return self.models[model_id]
        except KeyError as exc:
            raise ValueError("模型不存在") from exc

    def list(self) -> list[dict]:
        return [{"id": model_id, "provider": item.mode, "model": item.model,
                 "supports_tools": True, "config_error": item.configuration_error()}
                for model_id, item in self.models.items()]
