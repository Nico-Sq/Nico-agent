"""OpenAI Chat Completions, shared by `openai` and `openai_compatible`."""

from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition


def request(model: str, messages: list[AgentMessage], tools: list[ToolDefinition],
            max_tokens: int) -> tuple[str, dict, dict]:
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
    return "chat/completions", {}, payload


def parse(data: dict) -> ModelReply:
    choice = data["choices"][0]
    message = choice["message"]
    if not isinstance(message, dict):
        raise TypeError("模型消息不是对象")
    return {**message, "usage": data.get("usage"), "finish_reason": choice.get("finish_reason")}
