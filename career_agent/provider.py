"""Model configuration and the single call path shared by every adapter."""

import os
from urllib.parse import urlsplit

from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition

from .providers import ADAPTERS, ModelError, demo, transport

DEFAULT_MAX_OUTPUT_TOKENS = 2048
MIN_MAX_OUTPUT_TOKENS = 256
MAX_MAX_OUTPUT_TOKENS = 8192

MODES = ("demo", "openai", "openai_compatible", "anthropic")


class Provider:
    def __init__(self, mode: str | None = None, model: str | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 max_output: int | str | None = None):
        self.mode = mode if mode is not None else os.getenv("MODEL_PROVIDER", "demo")
        self.model = model if model is not None else os.getenv("MODEL_NAME", "").strip()
        default_url = "https://api.anthropic.com/v1" if self.mode == "anthropic" else "https://api.openai.com/v1"
        self.base_url = base_url if base_url is not None else os.getenv("MODEL_API_BASE_URL", default_url).strip()
        self.api_key = api_key if api_key is not None else os.getenv("MODEL_API_KEY", "")
        env_output = os.getenv("MODEL_MAX_OUTPUT_TOKENS", "")
        self.max_output = max_output if max_output is not None else (env_output or DEFAULT_MAX_OUTPUT_TOKENS)

    def max_output_tokens(self) -> int | None:
        """Return the validated output budget, or None when the value is unusable."""
        try:
            value = int(self.max_output)
        except (TypeError, ValueError):
            return None
        return value if MIN_MAX_OUTPUT_TOKENS <= value <= MAX_MAX_OUTPUT_TOKENS else None

    def configuration_error(self) -> str | None:
        if self.mode not in MODES:
            return "MODEL_PROVIDER 仅支持 demo、openai、openai_compatible 或 anthropic"
        if self.max_output_tokens() is None:
            return "MODEL_MAX_OUTPUT_TOKENS 需在 256–8192 之间"
        if self.mode == "demo":
            return None
        if not self.model.strip():
            return "请设置 MODEL_NAME"
        if self.mode in ("openai", "anthropic") and not self.api_key:
            return "请设置 MODEL_API_KEY"
        try:
            url = urlsplit(self.base_url)
            port = url.port
        except ValueError:
            return "MODEL_API_BASE_URL 必须是 http:// 或 https:// 地址"
        if url.scheme not in ("http", "https") or not url.hostname or port == 0:
            return "MODEL_API_BASE_URL 必须是 http:// 或 https:// 地址"
        return None

    async def complete(self, messages: list[AgentMessage],
                       tools: list[ToolDefinition]) -> ModelReply:
        config_error = self.configuration_error()
        if config_error:
            raise ModelError("model_config_invalid", config_error)
        if self.mode == "demo":
            return demo.complete(messages, tools)
        adapter = ADAPTERS[self.mode]
        endpoint, extra, payload = adapter.request(self.model, messages, tools, self.max_output_tokens())
        headers = {"Content-Type": "application/json", **extra}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = await transport.post_json(self.base_url, endpoint, headers, payload)
        try:
            return adapter.parse(data)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("model_invalid_response", "模型服务返回了无效响应") from exc
