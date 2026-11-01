"""Compatibility imports for model adapters moved to ``nico_adapters``."""

from nico_adapters.models import ADAPTERS, Adapter, ModelError, anthropic, demo, openai_chat, transport

__all__ = ["ADAPTERS", "Adapter", "ModelError", "anthropic", "demo", "openai_chat", "transport"]
