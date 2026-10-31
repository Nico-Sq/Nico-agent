"""Provider-neutral message and tool-call shapes."""

from typing import Literal, TypedDict


MessageRole = Literal["system", "user", "assistant", "tool"]


class ToolFunctionCall(TypedDict):
    name: str
    arguments: str


class ToolCall(TypedDict, total=False):
    id: str
    type: Literal["function"]
    function: ToolFunctionCall


class AgentMessage(TypedDict, total=False):
    role: MessageRole
    content: str | None
    tool_calls: list[ToolCall]
    tool_call_id: str
