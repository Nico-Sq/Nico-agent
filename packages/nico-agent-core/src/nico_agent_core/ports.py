"""Ports used by the provider-independent agent core."""

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition


class ModelPort(Protocol):
    async def complete(self, messages: list[AgentMessage],
                       tools: list[ToolDefinition]) -> ModelReply: ...


@runtime_checkable
class StreamingModelPort(ModelPort, Protocol):
    async def stream(self, messages: list[AgentMessage], tools: list[ToolDefinition],
                     on_delta: Callable[[str], None]) -> ModelReply: ...


class ToolSnapshot(Protocol):
    definitions: tuple[ToolDefinition, ...]

    async def execute(self, name: str, args: dict) -> dict: ...

    def can_execute_in_parallel(self, name: str) -> bool: ...
