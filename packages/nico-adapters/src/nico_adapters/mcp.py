"""A small stdio MCP client for explicitly configured local servers."""

import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path

import jsonschema
from nico_domain import tool_is_read_only

from nico_application.tools import resolve_external_tool_name


class MCPClient:
    def __init__(self, command: list[str]):
        self.command = command
        self.process = None
        self.loop = None
        self.next_id = 0

    async def connect(self) -> "MCPClient":
        if (self.process is not None and self.process.returncode is None
                and self.loop is asyncio.get_running_loop()):
            return self
        await self.close()
        self.loop = asyncio.get_running_loop()
        self.process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, limit=1_000_000,
        )
        try:
            reply = await self.request("initialize", {"protocolVersion": "2025-11-25",
                "capabilities": {}, "clientInfo": {"name": "nico-agent", "version": "0.1.0"}})
            if not isinstance(reply.get("protocolVersion"), str):
                raise ValueError("MCP 初始化响应无效")
            await self.notify("notifications/initialized")
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        process = self.process
        if process is None or process.returncode is not None:
            return
        if self.loop is not asyncio.get_running_loop():
            # 旧事件循环已关闭，只能同步结束子进程。
            process.kill()
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 2)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *_):
        await self.close()

    async def _send(self, message: dict):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def notify(self, method: str):
        await self._send({"jsonrpc": "2.0", "method": method})

    async def request(self, method: str, params: dict) -> dict:
        self.next_id += 1
        request_id = self.next_id
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        async with asyncio.timeout(20):
            for _ in range(100):
                line = await self.process.stdout.readline()
                if not line or len(line) > 1_000_000:
                    raise ValueError("MCP 连接已关闭或响应过大")
                reply = json.loads(line)
                if not isinstance(reply, dict):
                    raise ValueError("MCP 响应无效")
                if reply.get("id") != request_id:
                    continue
                if "error" in reply or not isinstance(reply.get("result"), dict):
                    raise ValueError(f"MCP {method} 失败")
                return reply["result"]
        raise ValueError("MCP 响应无效")

    async def tools(self) -> list[dict]:
        result = []
        cursor = None
        for _ in range(10):
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            entries = page.get("tools")
            if not isinstance(entries, list):
                raise ValueError("MCP 工具列表无效")
            result.extend(entries)
            cursor = page.get("nextCursor")
            if not cursor:
                return result
        raise ValueError("MCP 工具列表页数超限")

    async def call(self, name: str, arguments: dict) -> dict:
        return await self.request("tools/call", {"name": name, "arguments": arguments})


class MCPServers:
    """按服务器持有常驻连接；单个服务器的故障不影响其他服务器。"""

    def __init__(self, path: Path | None = None):
        if path is None:
            configured = os.getenv("MCP_SERVERS_PATH")
            path = Path(configured) if configured else None
        self.servers = {}
        if path:
            # 配置文件可能带 Windows 编辑器写入的 BOM，utf-8-sig 一并处理。
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("MCP 配置必须是对象")
            for name, config in data.items():
                if (not re.fullmatch(r"[A-Za-z0-9_]{1,32}", name) or not isinstance(config, dict)
                        or not isinstance(config.get("command"), list)
                        or not config["command"] or not all(isinstance(part, str) and part for part in config["command"])
                        or not isinstance(config.get("enabled_tools"), list)
                        or not all(isinstance(tool, str) for tool in config["enabled_tools"])):
                    raise ValueError(f"MCP 服务器 {name} 配置无效")
                self.servers[name] = config
        # 每个服务器持有常驻连接；同一服务器的请求串行，不同服务器互不影响。
        self.clients: dict[str, MCPClient] = {}
        self.catalogs: dict[str, list[dict]] = {}
        self.failures: dict[str, str] = {}
        self.locks = {name: asyncio.Lock() for name in self.servers}

    def status(self) -> dict[str, str]:
        """Return the error code of every server whose last attempt failed."""
        return dict(self.failures)

    async def report(self) -> list[dict]:
        """Describe every configured server: exposed tool count and last error code."""
        await self.discover()
        return [{"name": name, "tools": len(self.catalogs.get(name, [])),
                 "error": self.failures.get(name)} for name in self.servers]

    async def _drop(self, server: str):
        self.catalogs.pop(server, None)
        client = self.clients.pop(server, None)
        if client is not None:
            await client.close()

    async def _client(self, server: str) -> MCPClient:
        client = self.clients.get(server)
        if (client is None or client.process is None or client.process.returncode is not None
                or client.loop is not asyncio.get_running_loop()):
            await self._drop(server)
            client = MCPClient(self.servers[server]["command"])
            await client.connect()
            self.clients[server] = client
        return client

    async def _tools(self, server: str) -> list[dict]:
        if server in self.catalogs:
            return self.catalogs[server]
        client = await self._client(server)
        config = self.servers[server]
        tools = []
        enabled_names = set()
        for item in await client.tools():
            if not isinstance(item, dict):
                raise ValueError("MCP 工具定义无效")
            if item.get("name") not in config["enabled_tools"]:
                continue
            name = item["name"]
            schema = item.get("inputSchema")
            if (not re.fullmatch(r"[A-Za-z0-9_]{1,32}", name)
                    or not isinstance(schema, dict) or schema.get("type") != "object"):
                raise ValueError("MCP 工具定义无效")
            if name in enabled_names:
                raise ValueError("MCP 工具定义重复")
            enabled_names.add(name)
            jsonschema.Draft202012Validator.check_schema(schema)
            annotations = item.get("annotations")
            tools.append({"name": f"mcp_{server}_{name}", "server": server,
                          "remote_name": name, "description": str(item.get("description", ""))[:300],
                          "parameters": schema,
                          "read_only": isinstance(annotations, dict)
                          and tool_is_read_only(annotations.get("readOnlyHint"))})
        self.catalogs[server] = tools
        return tools

    async def discover(self) -> list[dict]:
        tools = []
        for server in self.servers:
            async with self.locks[server]:
                try:
                    tools.extend(await self._tools(server))
                    self.failures.pop(server, None)
                except Exception:
                    self.failures[server] = "mcp_server_unavailable"
                    await self._drop(server)
        counts = Counter(item["name"] for item in tools)
        resolved = []
        for item in tools:
            name = resolve_external_tool_name(
                item["name"], f"{item['server']}\0{item['remote_name']}",
                force_hash=counts[item["name"]] > 1,
            )
            resolved.append({**item, "name": name})
        return resolved

    async def call(self, tool: dict, arguments: dict) -> dict:
        jsonschema.validate(arguments, tool["parameters"])
        server = tool["server"]
        if tool["remote_name"] not in self.servers[server]["enabled_tools"]:
            raise ValueError("MCP 工具未启用")
        catalog = self.catalogs.get(server)
        if catalog is not None and tool["remote_name"] not in {item["remote_name"] for item in catalog}:
            raise ValueError("MCP 工具已不存在")
        async with self.locks[server]:
            try:
                client = await self._client(server)
                result = await client.call(tool["remote_name"], arguments)
                self.failures.pop(server, None)
            except Exception:
                self.failures[server] = "mcp_server_unavailable"
                await self._drop(server)
                raise
        content = result.get("content", [])
        if not isinstance(content, list):
            raise ValueError("MCP 工具结果无效")
        text = "\n".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
        return {"error": "mcp_tool_failed"} if result.get("isError") else {"text": text[:8000]}

    async def close(self):
        for server in list(self.clients):
            await self._drop(server)
        self.catalogs.clear()
