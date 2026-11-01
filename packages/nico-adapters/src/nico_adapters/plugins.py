"""Explicitly loaded, trusted local tool plugins."""

import asyncio
import importlib.util
import json
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable

import jsonschema
from nico_domain import tool_is_read_only

from nico_application.tools import resolve_external_tool_name

PLUGIN_TIMEOUT_SECONDS = 10.0


class PluginTimeout(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _PluginTool:
    definition: str
    run: Callable


class LocalPlugins:
    def __init__(self, path: Path | None = None):
        if path is None:
            configured = os.getenv("LOCAL_PLUGINS_PATH")
            path = Path(configured) if configured else None
        self.path = path
        self._loaded_tools = ()
        self._prepared_tools = MappingProxyType({})
        self._active_tools = MappingProxyType({})
        self._loaded = False
        self._prepared = False
        self.load()
        self.prepare()
        self.activate()

    def load(self) -> None:
        if self._loaded:
            return
        pending = []
        if self.path:
            # 配置文件可能带 Windows 编辑器写入的 BOM，utf-8-sig 一并处理。
            entries = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list):
                raise ValueError("插件配置必须是 JSON 数组")
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                    raise ValueError("插件路径无效")
                source = Path(entry["path"])
                if not source.is_absolute():
                    source = self.path.parent / source
                if not source.is_file() or source.suffix != ".py" or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", source.stem):
                    raise ValueError("插件必须是本地 Python 文件")
                spec = importlib.util.spec_from_file_location(f"nico_plugin_{source.stem}", source)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                definitions = getattr(module, "TOOLS", None)
                if not isinstance(definitions, list):
                    raise ValueError("插件必须提供 TOOLS 列表")
                for item in definitions:
                    pending.append((source.stem, item))
        self._loaded_tools = tuple(pending)
        self._loaded = True

    def prepare(self) -> None:
        if not self._loaded:
            raise RuntimeError("plugins must be loaded before prepare")
        if self._prepared:
            return
        pending = []
        for plugin_name, item in self._loaded_tools:
            if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", str(item.get("name", ""))):
                raise ValueError("插件工具名称无效")
            schema = item.get("parameters")
            if not isinstance(schema, dict) or schema.get("type") != "object" or not callable(item.get("run")):
                raise ValueError("插件工具必须提供 object 参数模式和 run 函数")
            jsonschema.Draft202012Validator.check_schema(schema)
            base = f"plugin_{plugin_name}_{item['name']}"
            pending.append((base, f"{plugin_name}\0{item['name']}", {
                "source": "plugin", "description": str(item.get("description", ""))[:300],
                "parameters": schema, "read_only": tool_is_read_only(item.get("read_only")),
                "run": item["run"],
            }))
        counts = Counter(base for base, _, _ in pending)
        snapshot = {}
        for base, identity, item in pending:
            name = resolve_external_tool_name(base, identity, force_hash=counts[base] > 1)
            if name in snapshot:
                raise ValueError(f"插件工具名称重复：{name}")
            item["name"] = name
            descriptor = {key: value for key, value in item.items() if key != "run"}
            snapshot[name] = _PluginTool(json.dumps(descriptor, ensure_ascii=False), item["run"])
        self._prepared_tools = MappingProxyType(snapshot)
        self._prepared = True

    def activate(self) -> None:
        if not self._loaded:
            self.load()
        if not self._prepared:
            self.prepare()
        self._active_tools = self._prepared_tools

    def retire(self) -> None:
        self._active_tools = MappingProxyType({})

    def discover(self) -> list[dict]:
        return [json.loads(item.definition) for item in self._active_tools.values()]

    def status(self) -> dict[str, str]:
        return {}

    async def call(self, item: dict, arguments: dict) -> dict:
        tool = self._active_tools.get(item.get("name"))
        if tool is None:
            raise ValueError("插件工具已移除")
        definition = json.loads(tool.definition)
        jsonschema.validate(arguments, definition["parameters"])
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(tool.run, arguments),
                PLUGIN_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise PluginTimeout("插件调用超时，后台线程可能仍在运行") from exc
        return {"text": str(result)[:8000]}

    async def close(self) -> None:
        self.retire()
        self._loaded_tools = ()
        self._prepared_tools = MappingProxyType({})
        self._loaded = False
        self._prepared = False
