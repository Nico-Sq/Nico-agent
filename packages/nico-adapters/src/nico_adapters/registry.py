"""Explicit entry-point discovery for configured tool adapters."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from collections.abc import Iterable


TOOL_ADAPTER_ENTRY_POINT_GROUP = "nico_agent.tool_adapters"
FIRST_PARTY_ADAPTERS = {
    "stdio_mcp": "nico_adapters.mcp:MCPServers",
    "trusted_local_plugins": "nico_adapters.plugins:LocalPlugins",
}


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


__all__ = ["FIRST_PARTY_ADAPTERS", "TOOL_ADAPTER_ENTRY_POINT_GROUP", "load_tool_adapters"]
