"""Ports used by the provider-independent agent core."""

from typing import Protocol

from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition


class ModelPort(Protocol):
    async def complete(self, messages: list[AgentMessage],
                       tools: list[ToolDefinition]) -> ModelReply: ...
