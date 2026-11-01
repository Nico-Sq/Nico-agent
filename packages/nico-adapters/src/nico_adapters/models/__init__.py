"""Model protocols and normalized model provider."""

from nico_contracts.errors import ModelError
from nico_contracts.models import ModelAdapter as Adapter

from . import anthropic, demo, openai_chat, transport

ADAPTERS: dict[str, Adapter] = {
    "openai": openai_chat,
    "deepseek": openai_chat,
    "openai_compatible": openai_chat,
    "anthropic": anthropic,
}

__all__ = [
    "ADAPTERS", "Adapter", "ModelError", "anthropic", "demo", "openai_chat", "transport",
]
