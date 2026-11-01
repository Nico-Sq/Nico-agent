"""Compatibility imports for the model provider."""

from nico_adapters.models import ADAPTERS, ModelError, demo, transport
from nico_adapters.models.provider import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_MAX_OUTPUT_TOKENS,
    MIN_MAX_OUTPUT_TOKENS,
    MODES,
    Provider,
)

__all__ = [
    "ADAPTERS", "DEFAULT_MAX_OUTPUT_TOKENS", "MAX_MAX_OUTPUT_TOKENS",
    "MIN_MAX_OUTPUT_TOKENS", "MODES", "ModelError", "Provider", "demo", "transport",
]
