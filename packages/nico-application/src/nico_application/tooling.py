"""Tool catalog, run snapshots and grant rules shared by every interface."""

from __future__ import annotations

import json
import inspect
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol

from nico_contracts.tools import ToolDefinition
from nico_domain import tool_is_read_only


class ToolGrantPort(Protocol):
    def get(self, session_id: str, kind: str) -> dict | None: ...
    def tool_grants(self, session_id: str) -> list[str]: ...
    def set_tool_grants(self, session_id: str, names: list[str]) -> list[str]: ...


class ToolAdapterPort(Protocol):
    def discover(self) -> list[dict] | Awaitable[list[dict]]: ...
    def status(self) -> dict[str, str]: ...
    async def call(self, tool: dict, arguments: dict) -> dict: ...
    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolCatalogSnapshot:
    """Immutable schema and descriptor snapshot for one model run."""

    _definitions: tuple[str, ...]
    _external_tools: tuple[tuple[str, str], ...]
    _read_only_names: frozenset[str]
    _handlers: tuple[tuple[str, ToolAdapterPort], ...]

    def definitions(self) -> list[ToolDefinition]:
        return [json.loads(value) for value in self._definitions]

    def external_tool(self, name: str) -> dict | None:
        for tool_name, value in self._external_tools:
            if tool_name == name:
                return json.loads(value)
        return None

    def external_adapter(self, name: str) -> ToolAdapterPort | None:
        for tool_name, adapter in self._handlers:
            if tool_name == name:
                return adapter
        return None

    def is_read_only(self, name: str) -> bool:
        return name in self._read_only_names


class ToolCatalogPort(Protocol):
    def snapshot(
        self,
        builtin_tools: Iterable[ToolDefinition],
        external_tools: Iterable[dict],
        grants: Iterable[str],
        builtin_read_only_names: Iterable[str],
        handlers: dict[str, ToolAdapterPort] | None = None,
    ) -> ToolCatalogSnapshot: ...


class ToolCatalog:
    def snapshot(
        self,
        builtin_tools: Iterable[ToolDefinition],
        external_tools: Iterable[dict],
        grants: Iterable[str],
        builtin_read_only_names: Iterable[str],
        handlers: dict[str, ToolAdapterPort] | None = None,
    ) -> ToolCatalogSnapshot:
        definitions = [dict(item) for item in builtin_tools]
        names = {
            item.get("function", {}).get("name")
            for item in definitions
            if isinstance(item.get("function"), dict)
        }
        external_snapshot: list[tuple[str, str]] = []
        handler_snapshot: list[tuple[str, ToolAdapterPort]] = []
        read_only_names = set(builtin_read_only_names)
        granted_names = set(grants)
        for item in external_tools:
            name = item.get("name")
            if not isinstance(name, str) or name not in granted_names:
                continue
            parameters = item.get("parameters")
            if not isinstance(parameters, dict):
                continue
            if name in names:
                raise ValueError("external tool name conflicts with a built-in tool")
            names.add(name)
            external_snapshot.append((name, json.dumps(item, ensure_ascii=False)))
            if handlers is not None and name in handlers:
                handler_snapshot.append((name, handlers[name]))
            if tool_is_read_only(item.get("read_only")):
                read_only_names.add(name)
                definitions.append({"type": "function", "function": {
                    "name": name,
                    "description": item.get("description", ""),
                    "parameters": parameters,
                }})
        return ToolCatalogSnapshot(
            tuple(json.dumps(item, ensure_ascii=False) for item in definitions),
            tuple(external_snapshot),
            frozenset(read_only_names),
            tuple(handler_snapshot),
        )


class ToolGrantError(ValueError):
    def __init__(self, kind: Literal["unavailable", "write_disabled"], message: str):
        super().__init__(message)
        self.kind = kind


class ToolGrantUseCases:
    def __init__(self, store: ToolGrantPort):
        self.store = store

    def require_session(self, session_id: str, kind: str) -> dict:
        session = self.store.get(session_id, kind)
        if session is None:
            raise ValueError(f"需要现有{kind}会话 ID")
        return session

    def list(self, session_id: str) -> list[str]:
        return self.store.tool_grants(session_id)

    def replace(self, session_id: str, names: list[str], available: Iterable[dict]) -> list[str]:
        tools = {item.get("name"): item for item in available if isinstance(item, dict)}
        if not set(names) <= tools.keys():
            raise ToolGrantError("unavailable", "工具不存在或未启用")
        if any(not tool_is_read_only(tools[name].get("read_only")) for name in names):
            raise ToolGrantError("write_disabled", "非只读工具尚未开放")
        return self.store.set_tool_grants(session_id, names)

    def grant(self, session_id: str, name: str, available: Iterable[dict]) -> list[str]:
        tools = {item.get("name"): item for item in available if isinstance(item, dict)}
        if name not in tools:
            raise ToolGrantError("unavailable", "工具不存在或未启用")
        if not tool_is_read_only(tools[name].get("read_only")):
            raise ToolGrantError("write_disabled", "非只读工具尚未开放")
        names = set(self.store.tool_grants(session_id))
        names.add(name)
        return self.store.set_tool_grants(session_id, sorted(names))

    def revoke(self, session_id: str, name: str) -> list[str]:
        names = set(self.store.tool_grants(session_id))
        names.discard(name)
        return self.store.set_tool_grants(session_id, sorted(names))


class ToolDiscoveryUseCases:
    def __init__(self, adapters: Iterable[ToolAdapterPort]):
        self.adapters = tuple(adapters)

    async def list(self) -> tuple[list[dict], dict[str, str]]:
        tools = []
        statuses = {}
        for adapter in self.adapters:
            discovered = adapter.discover()
            if inspect.isawaitable(discovered):
                discovered = await discovered
            tools.extend(discovered)
            statuses.update(adapter.status())
        return tools, statuses
