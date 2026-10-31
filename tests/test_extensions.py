import asyncio
import tempfile
import unittest
from pathlib import Path

from career_agent.cli import run_channel
from career_agent.plugins import LocalPlugins
from career_agent.runtime import AgentRuntime
from career_agent.store import Store


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
