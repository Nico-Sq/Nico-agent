"""Model configuration and single call path shared by every adapter."""

import math
import os
from collections.abc import Callable
from urllib.parse import urlsplit, urlunsplit

from nico_agent_core import ModelPort
from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition

from . import ADAPTERS, ModelError, demo, openai_chat, transport

DEFAULT_MAX_OUTPUT_TOKENS = 2048
MIN_MAX_OUTPUT_TOKENS = 256
MAX_MAX_OUTPUT_TOKENS = 8192
MODES = ("demo", "openai", "deepseek", "openai_compatible", "anthropic")


class Provider(ModelPort):
    def __init__(self, mode: str | None = None, model: str | None = None,
                 base_url: str | None = None, api_key: str | None = None,
                 max_output: int | str | None = None,
                 temperature: float | str | None = None):
        self.mode = mode if mode is not None else os.getenv("MODEL_PROVIDER", "demo")
        self.model = model if model is not None else os.getenv("MODEL_NAME", "").strip()
        default_url = {
            "anthropic": "https://api.anthropic.com/v1",
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com/v1",
        }.get(self.mode, "https://api.openai.com/v1")
        self.base_url = base_url if base_url is not None else os.getenv("MODEL_API_BASE_URL", default_url).strip()
        if self.mode == "openai":
            try:
                parts = urlsplit(self.base_url)
            except ValueError:
                parts = None
            if parts is not None and not parts.path.strip("/"):
                self.base_url = urlunsplit((parts.scheme, parts.netloc, "/v1", parts.query, parts.fragment))
        if api_key is not None:
            self.api_key = api_key
        elif self.mode == "openai":
            self.api_key = os.getenv("OPENAI_API_KEY", os.getenv("MODEL_API_KEY", ""))
        elif self.mode == "deepseek":
            self.api_key = os.getenv("DEEPSEEK_API_KEY", "")
        else:
            self.api_key = os.getenv("MODEL_API_KEY", "")
        env_output = os.getenv("MODEL_MAX_OUTPUT_TOKENS", "")
        self.max_output = max_output if max_output is not None else (env_output or DEFAULT_MAX_OUTPUT_TOKENS)
        env_temperature = os.getenv("MODEL_TEMPERATURE", "")
        self.temperature = (temperature if temperature is not None else
                            env_temperature or ("0.7" if self.mode == "openai" else None))
        if self.mode not in ("openai", "deepseek", "openai_compatible"):
            self.temperature = None

    def max_output_tokens(self) -> int | None:
        try:
            value = int(self.max_output)
        except (TypeError, ValueError):
            return None
        return value if MIN_MAX_OUTPUT_TOKENS <= value <= MAX_MAX_OUTPUT_TOKENS else None

    def temperature_value(self) -> float | None:
        if self.temperature is None:
            return None
        if isinstance(self.temperature, bool):
            return None
        try:
            value = float(self.temperature)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and 0 <= value <= 2 else None

    def configuration_error(self) -> str | None:
        if self.mode not in MODES:
            return "MODEL_PROVIDER 仅支持 demo、openai、deepseek、openai_compatible 或 anthropic"
        if self.max_output_tokens() is None:
            return "MODEL_MAX_OUTPUT_TOKENS 需在 256–8192 之间"
        if self.temperature is not None and self.temperature_value() is None:
            return "MODEL_TEMPERATURE 需在 0–2 之间"
        if self.mode == "demo":
            return None
        if not self.model.strip():
            return "请设置 MODEL_NAME"
        if self.mode in ("openai", "anthropic", "deepseek") and not self.api_key:
            key_name = {"openai": "OPENAI_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}.get(
                self.mode, "MODEL_API_KEY",
            )
            return f"请设置 {key_name}"
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
        endpoint, extra, payload = adapter.request(
            self.model, messages, tools, self.max_output_tokens(),
            self.temperature_value() if self.mode in ("openai", "deepseek", "openai_compatible") else None,
        )
        headers = {"Content-Type": "application/json", **extra}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = await transport.post_json(self.base_url, endpoint, headers, payload)
        try:
            return adapter.parse(data)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ModelError("model_invalid_response", "模型服务返回了无效响应") from exc

    async def stream(self, messages: list[AgentMessage], tools: list[ToolDefinition],
                     on_delta: Callable[[str], None]) -> ModelReply:
        if self.mode not in ("openai", "deepseek", "openai_compatible"):
            reply = await self.complete(messages, tools)
            content = reply.get("content")
            if isinstance(content, str) and content:
                on_delta(content)
            return reply
        config_error = self.configuration_error()
        if config_error:
            raise ModelError("model_config_invalid", config_error)
        endpoint, extra, payload = ADAPTERS[self.mode].request(
            self.model, messages, tools, self.max_output_tokens(), self.temperature_value(),
        )
        payload["stream"] = True
        headers = {"Content-Type": "application/json", **extra}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        chunks = transport.post_sse(self.base_url, endpoint, headers, payload)
        return await openai_chat.parse_stream(chunks, on_delta)
