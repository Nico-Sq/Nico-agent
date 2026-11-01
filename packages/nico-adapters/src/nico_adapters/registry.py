"""Explicit entry-point discovery for configured tool adapters."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from collections.abc import Iterable


TOOL_ADAPTER_ENTRY_POINT_GROUP = "nico_agent.tool_adapters"
CHANNEL_ENTRY_POINT_GROUP = "nico_agent.channels"
FIRST_PARTY_ADAPTERS = {
    "stdio_mcp": "nico_adapters.mcp:MCPServers",
    "trusted_local_plugins": "nico_adapters.plugins:LocalPlugins",
}
FIRST_PARTY_CHANNELS = {"console": "nico_adapters.console:ConsoleChannel"}


def _load_target(target: str):
    module_name, attribute = target.split(":", 1)
    return getattr(import_module(module_name), attribute)


def load_tool_adapters(names: Iterable[str]) -> dict[str, type]:
    """Load only requested adapters; unconfigured third-party entry points stay inert."""
    requested = list(dict.fromkeys(names))
    candidates = list(entry_points(group=TOOL_ADAPTER_ENTRY_POINT_GROUP))
    result = {}
    for name in requested:
        matching = [item for item in candidates if item.name == name]
        if name in FIRST_PARTY_ADAPTERS:
            matching = [item for item in matching
                        if getattr(getattr(item, "dist", None), "name", "") == "nico-adapters"]
            if len(matching) > 1:
                raise ValueError(f"tool adapter entry point is ambiguous: {name}")
            result[name] = matching[0].load() if matching else _load_target(FIRST_PARTY_ADAPTERS[name])
        else:
            if len(matching) != 1:
                raise ValueError(f"tool adapter is unavailable or ambiguous: {name}")
            result[name] = matching[0].load()
    return result


def load_channels(names: Iterable[str]) -> dict[str, type]:
    """Load only selected channels; third-party entry points are inert until requested."""
    requested = list(dict.fromkeys(names))
    candidates = list(entry_points(group=CHANNEL_ENTRY_POINT_GROUP))
    result = {}
    for name in requested:
        matching = [item for item in candidates if item.name == name]
        if name in FIRST_PARTY_CHANNELS:
            matching = [item for item in matching
                        if getattr(getattr(item, "dist", None), "name", "") == "nico-adapters"]
            if len(matching) > 1:
                raise ValueError(f"channel entry point is ambiguous: {name}")
            result[name] = matching[0].load() if matching else _load_target(FIRST_PARTY_CHANNELS[name])
        else:
            if len(matching) != 1:
                raise ValueError(f"channel is unavailable or ambiguous: {name}")
            result[name] = matching[0].load()
    return result


__all__ = [
    "CHANNEL_ENTRY_POINT_GROUP", "FIRST_PARTY_ADAPTERS", "FIRST_PARTY_CHANNELS",
    "TOOL_ADAPTER_ENTRY_POINT_GROUP", "load_channels", "load_tool_adapters",
]
