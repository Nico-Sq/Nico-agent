import asyncio
import tempfile
import unittest
from pathlib import Path

from career_agent.cli import run_channel
from career_agent.plugins import LocalPlugins
from career_agent.runtime import AgentRuntime
from career_agent.store import Store
from career_agent.tools import READ_ONLY_TOOLS, schemas


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent / ".tmp")
        self.store = Store(Path(self.temp.name) / "agent.sqlite3")
        self.plugins = LocalPlugins(Path(__file__).resolve().parent.parent / "examples" / "plugins.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_example_plugin_requires_grant_and_validates_arguments(self):
        item = self.plugins.discover()[0]
        self.assertEqual(item["name"], "plugin_echo_plugin_echo")
        self.assertIs(item["read_only"], True)
        self.assertEqual(asyncio.run(self.plugins.call(item, {"text": "hello"}))["text"], "收到：hello")
        with self.assertRaises(Exception):
            asyncio.run(self.plugins.call(item, {"bad": 1}))

        class Provider:
            async def complete(self, messages, tools):
                if messages[-1]["role"] == "tool":
                    return {"content": str(messages[-1]["content"])}
                return {"tool_calls": [{"id": "one", "function": {
                    "name": item["name"], "arguments": '{"text":"hello"}',
                }}]}

        chat = self.store.create("chat")
        self.store.set_tool_grants(chat["id"], [item["name"]])
        pending = self.store.claim_run(chat["id"], "chat", "active", "test")
        response = asyncio.run(AgentRuntime(self.store, Provider(), plugins=self.plugins).run(pending))
        self.assertIn("收到：hello", response["message"])

    def test_tool_batches_keep_read_only_calls_parallel_and_side_effects_serial(self):
        state = {"active": 0, "peak": 0}

        async def fake(session, call, external=None):
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.01)
            state["active"] -= 1
            return {"role": "tool", "tool_call_id": call["id"], "content": "{}"}

        runtime = AgentRuntime(self.store, object(), plugins=self.plugins)
        runtime._execute_tool = fake
        read = {"id": "a", "function": {"name": "read_material"}}
        write = {"id": "b", "function": {"name": "plugin_echo_plugin_echo"}}
        external = {"plugin_echo_plugin_echo": {"name": "plugin_echo_plugin_echo", "read_only": False}}

        parallel = asyncio.run(runtime._execute_tools(None, [read, read], external))
        self.assertEqual(state["peak"], 2)
        self.assertEqual([item["tool_call_id"] for item in parallel], ["a", "a"])

        state["peak"] = 0
        serial = asyncio.run(runtime._execute_tools(None, [read, write, read], external))
        self.assertEqual(state["peak"], 1)
        self.assertEqual([item["tool_call_id"] for item in serial], ["a", "b", "a"])

    def test_builtin_tool_schemas_are_shared_and_read_only(self):
        chat = [item["function"]["name"] for item in schemas("chat")]
        interview = [item["function"]["name"] for item in schemas("interview")]
        self.assertEqual(chat, ["read_interview_feedback", "read_confirmed_claims",
                                "read_material", "search_memories"])
        self.assertEqual(interview, ["read_interview_goal", "read_interview_materials"])
        self.assertTrue(set(chat + interview) <= READ_ONLY_TOOLS)

    def test_plugin_config_tolerates_a_byte_order_mark(self):
        plugin = (Path(__file__).resolve().parent.parent / "examples" / "echo_plugin.py").as_posix()
        config = Path(self.temp.name) / "plugins-bom.json"
        config.write_text('[{"path": "%s"}]' % plugin, encoding="utf-8-sig")
        self.assertEqual(LocalPlugins(config).discover()[0]["name"], "plugin_echo_plugin_echo")

    def test_cli_channel_uses_runtime_and_persists_session(self):
        class Channel:
            def __init__(self):
                self.inputs = iter(["你好", None])
                self.outputs = []

            async def receive(self):
                return next(self.inputs)

            async def send(self, text):
                self.outputs.append(text)

        channel = Channel()
        session_id = asyncio.run(run_channel(channel, self.store))
        self.assertIn("演示模式已收到：你好", channel.outputs[-1])
        self.assertEqual(len(self.store.messages(session_id, "chat")), 2)
