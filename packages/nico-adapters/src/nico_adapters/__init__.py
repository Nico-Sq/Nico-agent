"""Compatibility-friendly model adapter distribution."""

from .models import ADAPTERS, ModelError
from .models.provider import Provider
from .registry import load_channels, load_tool_adapters
from .console import ConsoleChannel

__all__ = [
    "ADAPTERS", "ConsoleChannel", "ModelError", "Provider", "load_channels",
    "load_tool_adapters",
]
