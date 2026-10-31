"""Stable contracts shared across Nico-agent packages."""

from .channels import ChannelResult, TextChannel, TextMessage
from .errors import ChannelAccessError, ChannelDeliveryError, ModelError
from .events import MAX_RUN_TOKENS, USAGE_KEYS, RunEvent, Usage, run_event
from .messages import AgentMessage, MessageRole, ToolCall, ToolFunctionCall
from .models import ModelAdapter, ModelReply, UsageReport
from .tools import ToolDefinition, ToolFunctionDefinition

__all__ = [
    "AgentMessage",
    "ChannelAccessError",
    "ChannelDeliveryError",
    "ChannelResult",
    "MAX_RUN_TOKENS",
    "MessageRole",
    "ModelAdapter",
    "ModelError",
    "ModelReply",
    "RunEvent",
    "TextChannel",
    "TextMessage",
    "ToolCall",
    "ToolDefinition",
    "ToolFunctionCall",
    "ToolFunctionDefinition",
    "USAGE_KEYS",
    "Usage",
    "UsageReport",
    "run_event",
]
