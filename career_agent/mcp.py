"""A small stdio MCP client for explicitly configured local servers."""

import asyncio
import json
import os
import re
from pathlib import Path

import jsonschema


class MCPClient:
    def __init__(self, command: list[str]):
        self.command = command
        self.process = None
        self.next_id = 0

    async def __aenter__(self):
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
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

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
    def __init__(self, path: Path | None = None):
        if path is None:
            configured = os.getenv("MCP_SERVERS_PATH")
            path = Path(configured) if configured else None
        self.servers = {}
        if path:
            data = json.loads(path.read_text(encoding="utf-8"))
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

    async def discover(self) -> list[dict]:
        tools = []
        for server, config in self.servers.items():
            async with MCPClient(config["command"]) as client:
                for item in await client.tools():
                    if item.get("name") not in config["enabled_tools"]:
                        continue
                    name = item["name"]
                    schema = item.get("inputSchema")
                    if (not re.fullmatch(r"[A-Za-z0-9_]{1,32}", name)
                            or not isinstance(schema, dict) or schema.get("type") != "object"):
                        raise ValueError("MCP 工具定义无效")
                    jsonschema.Draft202012Validator.check_schema(schema)
                    tools.append({"name": f"mcp_{server}_{name}", "server": server,
                                  "remote_name": name, "description": str(item.get("description", ""))[:300],
                                  "parameters": schema})
        return tools

    async def call(self, tool: dict, arguments: dict) -> dict:
        jsonschema.validate(arguments, tool["parameters"])
        config = self.servers[tool["server"]]
        if tool["remote_name"] not in config["enabled_tools"]:
            raise ValueError("MCP 工具未启用")
        async with MCPClient(config["command"]) as client:
            names = {item.get("name") for item in await client.tools()}
            if tool["remote_name"] not in names:
                raise ValueError("MCP 工具已不存在")
            result = await client.call(tool["remote_name"], arguments)
        content = result.get("content", [])
        if not isinstance(content, list):
            raise ValueError("MCP 工具结果无效")
        text = "\n".join(str(part.get("text", "")) for part in content if part.get("type") == "text")
        return {"error": "mcp_tool_failed"} if result.get("isError") else {"text": text[:8000]}
