"""Model adapters: one module per wire protocol, selected by `MODEL_PROVIDER`."""

from . import anthropic, demo, openai_chat
from .base import Adapter
from .errors import ModelError

ADAPTERS: dict[str, Adapter] = {
    "openai": openai_chat,
    "openai_compatible": openai_chat,
    "anthropic": anthropic,
}

__all__ = ["ADAPTERS", "Adapter", "ModelError", "anthropic", "demo", "openai_chat"]
