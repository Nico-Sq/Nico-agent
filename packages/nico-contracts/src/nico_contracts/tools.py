"""Provider-neutral tool declaration shapes."""

from typing import Any, Literal, TypedDict


class ToolFunctionDefinition(TypedDict):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolDefinition(TypedDict):
    type: Literal["function"]
    function: ToolFunctionDefinition
