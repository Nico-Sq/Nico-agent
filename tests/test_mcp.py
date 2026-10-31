import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from career_agent import main
from career_agent.mcp import MCPServers
from career_agent.runtime import AgentRuntime
from career_agent.store import Store


SERVER = '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}
    elif method == "tools/list":
        result = {"tools":[{"name":"echo","description":"Echo text","annotations":{"readOnlyHint":True},"inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False}}]}
    elif method == "tools/call":
        result = {"content":[{"type":"text","text":request["params"]["arguments"]["text"]}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}), flush=True)
'''


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / ".tmp")
        root = Path(self.temp.name)
        script = root / "server.py"
        script.write_text(SERVER, encoding="utf-8")
        config = root / "mcp.json"
        config.write_text(json.dumps({"fixture": {"command": [sys.executable, str(script)],
                                                 "enabled_tools": ["echo"]}}), encoding="utf-8")
        self.mcp = MCPServers(config)
        self.store = Store(root / "agent.sqlite3")
        self.loop = asyncio.new_event_loop()

    def run_async(self, coro):
        return self.loop.run_until_complete(coro)

    def tearDown(self):
        self.run_async(self.mcp.close())
        self.loop.close()
        self.temp.cleanup()

    def test_discover_validate_and_call(self):
        async def run():
            tools = await self.mcp.discover()
            self.assertEqual(tools[0]["name"], "mcp_fixture_echo")
            self.assertIs(tools[0]["read_only"], True)
            self.assertEqual((await self.mcp.call(tools[0], {"text": "hello"}))["text"], "hello")
            with self.assertRaises(Exception):
                await self.mcp.call(tools[0], {"unexpected": "value"})
        self.run_async(run())

    def test_discovery_and_calls_share_one_server_connection(self):
        original = asyncio.create_subprocess_exec
        spawned = []

        async def counting(*args, **kwargs):
            spawned.append(args[0])
            return await original(*args, **kwargs)

        async def run():
            tools = await self.mcp.discover()
            client = self.mcp.clients["fixture"]
            for _ in range(3):
                self.assertEqual((await self.mcp.call(tools[0], {"text": "hi"}))["text"], "hi")
                self.assertIs(self.mcp.clients["fixture"], client)
            self.assertIsNone(client.process.returncode)
            self.assertEqual(self.mcp.status(), {})
            await self.mcp.close()
            self.assertEqual(self.mcp.clients, {})
            self.assertIsNotNone(client.process.returncode)

        with patch("career_agent.mcp.asyncio.create_subprocess_exec", counting):
            self.run_async(run())
        self.assertEqual(len(spawned), 1)

    def test_broken_server_does_not_block_other_servers(self):
        config = Path(self.temp.name) / "broken.json"
        config.write_text(json.dumps({
            "fixture": {"command": [sys.executable, str(Path(self.temp.name) / "server.py")],
                        "enabled_tools": ["echo"]},
            "broken": {"command": [sys.executable, "-c", "import sys; sys.exit(3)"],
                       "enabled_tools": ["echo"]},
        }), encoding="utf-8")
        manager = MCPServers(config)

        async def run():
            tools = await manager.discover()
            self.assertEqual([item["name"] for item in tools], ["mcp_fixture_echo"])
            self.assertEqual(manager.status(), {"broken": "mcp_server_unavailable"})
            self.assertEqual(await manager.report(), [
                {"name": "fixture", "tools": 1, "error": None},
                {"name": "broken", "tools": 0, "error": "mcp_server_unavailable"},
            ])
            self.assertEqual((await manager.call(tools[0], {"text": "still"}))["text"], "still")
            await manager.close()

        self.run_async(run())

    def test_api_reports_server_status(self):
        with patch.object(main, "mcp_servers", self.mcp):
            with TestClient(main.app) as client:
                self.assertEqual(client.get("/api/mcp/servers").json(), {
                    "servers": [{"name": "fixture", "tools": 1, "error": None}],
                })

    def test_config_tolerates_a_byte_order_mark(self):
        config = Path(self.temp.name) / "mcp-bom.json"
        config.write_text(json.dumps({"fixture": {
            "command": [sys.executable, str(Path(self.temp.name) / "server.py")],
            "enabled_tools": ["echo"]}}), encoding="utf-8-sig")
        manager = MCPServers(config)

        async def run():
            try:
                return await manager.discover()
            finally:
                await manager.close()

        self.assertEqual([item["name"] for item in self.run_async(run())], ["mcp_fixture_echo"])

    def test_grant_is_required_before_model_sees_tool(self):
        class CallingProvider:
            def __init__(self):
                self.tools = []

            async def complete(self, messages, tools):
                self.tools.append([item["function"]["name"] for item in tools])
                if "mcp_fixture_echo" not in self.tools[-1]:
                    return {"content": "未授权"}
                if messages[-1]["role"] == "tool":
                    return {"content": json.loads(messages[-1]["content"])["text"]}
                return {"content": None, "tool_calls": [{"id": "1", "function": {
                    "name": "mcp_fixture_echo", "arguments": '{"text":"hello"}',
                }}]}

        chat = self.store.create("chat")
        provider = CallingProvider()
        first = self.store.claim_run(chat["id"], "chat", "active", "先检查")
        result = self.run_async(AgentRuntime(self.store, provider, self.mcp).run(first))
        self.assertEqual(result["message"], "未授权")
        self.store.set_tool_grants(chat["id"], ["mcp_fixture_echo"])
        second = self.store.claim_run(chat["id"], "chat", "active", "再检查")
        result = self.run_async(AgentRuntime(self.store, provider, self.mcp).run(second))
        self.assertEqual(result["message"], "hello")
        self.assertIn("mcp_fixture_echo", provider.tools[-1])

    def test_api_rejects_unknown_grant(self):
        original = main.store
        main.store = self.store
        try:
            with patch.object(main, "mcp_servers", self.mcp):
                with TestClient(main.app) as client:
                    chat = client.post("/api/chat/sessions").json()
                    self.assertEqual(client.put(f"/api/chat/sessions/{chat['id']}/tools", json={
                        "names": ["mcp_fixture_missing"],
                    }).status_code, 422)
                    approved = client.put(f"/api/chat/sessions/{chat['id']}/tools", json={
                        "names": ["mcp_fixture_echo"],
                    })
                    self.assertEqual(approved.json()["names"], ["mcp_fixture_echo"])
        finally:
            main.store = original
