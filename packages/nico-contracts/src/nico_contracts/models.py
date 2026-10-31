"""Model adapter and normalized response contracts."""

from typing import Protocol, TypedDict

from .messages import AgentMessage, ToolCall
from .tools import ToolDefinition


class UsageReport(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ModelReply(TypedDict, total=False):
    role: str
    content: str | None
    tool_calls: list[ToolCall]
    finish_reason: str
    usage: UsageReport | None


class ModelAdapter(Protocol):
    def request(self, model: str, messages: list[AgentMessage], tools: list[ToolDefinition],
                max_tokens: int) -> tuple[str, dict, dict]:
        """Translate one normalized request to a wire request."""
        ...

    def parse(self, data: dict) -> ModelReply:
        """Translate one provider response to the normalized reply shape."""
        ...
