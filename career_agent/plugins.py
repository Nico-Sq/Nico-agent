"""Explicitly loaded, trusted local tool plugins."""

import asyncio
import importlib.util
import json
import os
import re
from collections import Counter
from pathlib import Path

import jsonschema

from .tools import resolve_external_tool_name


class LocalPlugins:
    def __init__(self, path: Path | None = None):
        if path is None:
            configured = os.getenv("LOCAL_PLUGINS_PATH")
            path = Path(configured) if configured else None
        self.tools = {}
        if not path:
            return
        # 配置文件可能带 Windows 编辑器写入的 BOM，utf-8-sig 一并处理。
        entries = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(entries, list):
            raise ValueError("插件配置必须是 JSON 数组")
        pending = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("插件路径无效")
            source = Path(entry["path"])
            if not source.is_absolute():
                source = path.parent / source
            if not source.is_file() or source.suffix != ".py" or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", source.stem):
                raise ValueError("插件必须是本地 Python 文件")
            spec = importlib.util.spec_from_file_location(f"nico_plugin_{source.stem}", source)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            definitions = getattr(module, "TOOLS", None)
            if not isinstance(definitions, list):
                raise ValueError("插件必须提供 TOOLS 列表")
            for item in definitions:
                if not isinstance(item, dict) or not re.fullmatch(r"[A-Za-z0-9_]{1,32}", str(item.get("name", ""))):
                    raise ValueError("插件工具名称无效")
                schema = item.get("parameters")
                if not isinstance(schema, dict) or schema.get("type") != "object" or not callable(item.get("run")):
                    raise ValueError("插件工具必须提供 object 参数模式和 run 函数")
                jsonschema.Draft202012Validator.check_schema(schema)
                base = f"plugin_{source.stem}_{item['name']}"
                pending.append((base, f"{source.stem}\0{item['name']}", {
                    "source": "plugin", "description": str(item.get("description", ""))[:300],
                    "parameters": schema, "read_only": item.get("read_only") is True,
                    "run": item["run"],
                }))
        counts = Counter(base for base, _, _ in pending)
        for base, identity, item in pending:
            name = resolve_external_tool_name(base, identity, force_hash=counts[base] > 1)
            if name in self.tools:
                raise ValueError(f"插件工具名称重复：{name}")
            item["name"] = name
            self.tools[name] = item

    def discover(self) -> list[dict]:
        return [{key: value for key, value in item.items() if key != "run"} for item in self.tools.values()]

    async def call(self, item: dict, arguments: dict) -> dict:
        jsonschema.validate(arguments, item["parameters"])
        result = await asyncio.wait_for(asyncio.to_thread(self.tools[item["name"]]["run"], arguments), 10)
        return {"text": str(result)[:8000]}
