"""Ports used by the provider-independent agent core."""

from typing import Protocol

from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition


class ModelPort(Protocol):
    async def complete(self, messages: list[AgentMessage],
                       tools: list[ToolDefinition]) -> ModelReply: ...


class ToolSnapshot(Protocol):
    definitions: tuple[ToolDefinition, ...]

    async def execute(self, name: str, args: dict) -> dict: ...

    def can_execute_in_parallel(self, name: str) -> bool: ...
