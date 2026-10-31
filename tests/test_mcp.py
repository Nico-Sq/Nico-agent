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
tool_name = sys.argv[1] if len(sys.argv) > 1 else "echo"
label = sys.argv[2] if len(sys.argv) > 2 else ""
read_only = (sys.argv[3] if len(sys.argv) > 3 else "read") == "read"
marker = sys.argv[4] if len(sys.argv) > 4 else ""
exit_on_call = len(sys.argv) > 5 and sys.argv[5] == "exit"
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if "id" not in request:
        continue
    if method == "initialize":
        result = {"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"fixture","version":"1"}}
    elif method == "tools/list":
        result = {"tools":[{"name":tool_name,"description":"Fixture tool","annotations":{"readOnlyHint":read_only},"inputSchema":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"],"additionalProperties":False}}]}
    elif method == "tools/call":
        if marker:
            with open(marker, "a", encoding="utf-8") as output:
                output.write("call\\n")
        if exit_on_call:
            sys.exit(0)
        text = request["params"]["arguments"]["text"]
        result = {"content":[{"type":"text","text":(label + ":" if label else "") + text}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}), flush=True)
'''


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / ".tmp")
        root = Path(self.temp.name)
        self.root = root
        script = root / "server.py"
        script.write_text(SERVER, encoding="utf-8")
        self.script = script
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
        self.store.set_tool_grants(chat["id"], [])
        third = self.store.claim_run(chat["id"], "chat", "active", "撤销后检查")
        result = self.run_async(AgentRuntime(self.store, provider, self.mcp).run(third))
        self.assertEqual(result["message"], "未授权")
        self.assertNotIn("mcp_fixture_echo", provider.tools[-1])

    def test_non_read_only_tool_is_hidden_and_execution_denied(self):
        marker = self.root / "write-called.txt"
        config = self.root / "write.json"
        config.write_text(json.dumps({"writer": {
            "command": [sys.executable, str(self.script), "save", "writer", "write", str(marker)],
            "enabled_tools": ["save"],
        }}), encoding="utf-8")
        manager = MCPServers(config)

        class CallingProvider:
            def __init__(self):
                self.tools = []

            async def complete(self, messages, tools):
                self.tools.append([item["function"]["name"] for item in tools])
                if messages[-1]["role"] == "tool":
                    return {"content": messages[-1]["content"]}
                return {"tool_calls": [{"id": "write-1", "function": {
                    "name": "mcp_writer_save", "arguments": '{"text":"secret"}',
                }}]}

        try:
            chat = self.store.create("chat")
            self.store.set_tool_grants(chat["id"], ["mcp_writer_save"])
            running = self.store.claim_run(chat["id"], "chat", "active", "保存")
            provider = CallingProvider()
            result = self.run_async(AgentRuntime(self.store, provider, manager).run(running))
            self.assertNotIn("mcp_writer_save", provider.tools[0])
            self.assertIn("tool_write_disabled", result["message"])
            self.assertFalse(marker.exists())
            event = next(item for item in self.store.events(chat["id"], "chat")
                         if item["kind"] == "tool")
            self.assertEqual(json.loads(event["detail"])["result"], "tool_write_disabled")
        finally:
            self.run_async(manager.close())

    def test_api_rejects_non_read_only_tool_grant(self):
        config = self.root / "write-api.json"
        config.write_text(json.dumps({"writer": {
            "command": [sys.executable, str(self.script), "save", "writer", "write"],
            "enabled_tools": ["save"],
        }}), encoding="utf-8")
        manager = MCPServers(config)
        original = main.store
        main.store = self.store
        try:
            with patch.object(main, "mcp_servers", manager):
                with TestClient(main.app) as client:
                    chat = client.post("/api/chat/sessions").json()
                    response = client.put(f"/api/chat/sessions/{chat['id']}/tools", json={
                        "names": ["mcp_writer_save"],
                    })
                    self.assertEqual(response.status_code, 422)
                    self.assertIn("非只读", response.json()["detail"])
        finally:
            main.store = original
            self.run_async(manager.close())

    def test_tool_names_are_bounded_and_collision_free(self):
        config = self.root / "names.json"
        config.write_text(json.dumps({
            "a_b": {"command": [sys.executable, str(self.script), "c", "a_b"],
                    "enabled_tools": ["c"]},
            "a": {"command": [sys.executable, str(self.script), "b_c", "a"],
                  "enabled_tools": ["b_c"]},
            "s" * 32: {"command": [sys.executable, str(self.script), "t" * 32, "long"],
                       "enabled_tools": ["t" * 32]},
        }), encoding="utf-8")
        manager = MCPServers(config)

        async def run():
            tools = await manager.discover()
            names = [item["name"] for item in tools]
            labels = {"a_b": "a_b", "a": "a", "s" * 32: "long"}
            self.assertEqual(len(names), 3)
            self.assertEqual(len(set(names)), 3)
            self.assertTrue(all(len(name) <= 64 for name in names))
            self.assertNotIn("mcp_a_b_c", names)
            for item in tools:
                result = await manager.call(item, {"text": "ok"})
                self.assertEqual(result["text"], f"{labels[item['server']]}:ok")

        try:
            self.run_async(run())
        finally:
            self.run_async(manager.close())

    def test_disconnected_call_is_not_retried(self):
        marker = self.root / "disconnect-called.txt"
        config = self.root / "disconnect.json"
        config.write_text(json.dumps({"fixture": {
            "command": [sys.executable, str(self.script), "echo", "fixture", "read",
                        str(marker), "exit"],
            "enabled_tools": ["echo"],
        }}), encoding="utf-8")
        manager = MCPServers(config)

        class CallingProvider:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                if messages[-1]["role"] == "tool":
                    return {"content": messages[-1]["content"]}
                return {"tool_calls": [{"id": "disconnect-1", "function": {
                    "name": "mcp_fixture_echo", "arguments": '{"text":"once"}',
                }}]}

        try:
            chat = self.store.create("chat")
            self.store.set_tool_grants(chat["id"], ["mcp_fixture_echo"])
            running = self.store.claim_run(chat["id"], "chat", "active", "只调用一次")
            provider = CallingProvider()
            result = self.run_async(AgentRuntime(self.store, provider, manager).run(running))
            self.assertEqual(provider.calls, 2)
            self.assertIn("mcp_tool_failed", result["message"])
            self.assertEqual(marker.read_text(encoding="utf-8"), "call\n")
        finally:
            self.run_async(manager.close())

    def test_invalid_arguments_are_rejected_before_mcp_call(self):
        marker = self.root / "invalid-args-called.txt"
        config = self.root / "invalid-args.json"
        config.write_text(json.dumps({"fixture": {
            "command": [sys.executable, str(self.script), "echo", "fixture", "read", str(marker)],
            "enabled_tools": ["echo"],
        }}), encoding="utf-8")
        manager = MCPServers(config)

        class CallingProvider:
            async def complete(self, messages, tools):
                if messages[-1]["role"] == "tool":
                    return {"content": messages[-1]["content"]}
                return {"tool_calls": [{"id": "invalid-1", "function": {
                    "name": "mcp_fixture_echo", "arguments": '{"unexpected":"value"}',
                }}]}

        try:
            chat = self.store.create("chat")
            self.store.set_tool_grants(chat["id"], ["mcp_fixture_echo"])
            running = self.store.claim_run(chat["id"], "chat", "active", "无效参数")
            result = self.run_async(AgentRuntime(self.store, CallingProvider(), manager).run(running))
            self.assertIn("invalid_arguments", result["message"])
            self.assertFalse(marker.exists())
        finally:
            self.run_async(manager.close())

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
