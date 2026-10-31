import asyncio
import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from career_agent.channels import (
    ChannelAccessError,
    ChannelDeliveryError,
    ChannelRouter,
    ConsoleChannel,
    TextMessage,
)
from career_agent.cli import (
    build_parser,
    cli_error_text,
    main as cli_main,
    run_channel,
    run_interview_action,
    run_memory_candidate_action,
    session_banner,
)
from career_agent.events import Usage
from career_agent.mcp import MCPServers
from career_agent.models import ModelCatalog
from career_agent.plugins import LocalPlugins, PluginTimeout
from career_agent.providers import ModelError
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

    def test_usage_accounting_ignores_malformed_reports(self):
        usage = Usage()
        usage.request_count = 1
        usage.add(None)
        usage.add({"total_tokens": True, "prompt_tokens": -1})
        self.assertEqual((usage.total_tokens, usage.coverage()), (0, "unavailable"))
        usage.add({"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7})
        self.assertEqual(usage.as_dict(), {"request_count": 1, "covered_requests": 1,
                                          "prompt_tokens": 5, "completion_tokens": 2,
                                          "total_tokens": 7})
        self.assertEqual(usage.coverage(), "exact")
        usage.request_count = 2
        self.assertEqual(usage.coverage(), "partial")

    def test_builtin_tool_schemas_keep_memory_proposal_out_of_read_only_set(self):
        chat = [item["function"]["name"] for item in schemas("chat")]
        interview = [item["function"]["name"] for item in schemas("interview")]
        self.assertEqual(chat, ["read_interview_feedback", "read_confirmed_claims",
                                "read_material", "propose_memory", "search_memories"])
        self.assertEqual(interview, ["read_interview_goal", "read_interview_materials"])
        self.assertNotIn("propose_memory", READ_ONLY_TOOLS)
        self.assertTrue(set(chat + interview) - {"propose_memory"} <= READ_ONLY_TOOLS)

    def test_plugin_config_tolerates_a_byte_order_mark(self):
        plugin = (Path(__file__).resolve().parent.parent / "examples" / "echo_plugin.py").as_posix()
        config = Path(self.temp.name) / "plugins-bom.json"
        config.write_text('[{"path": "%s"}]' % plugin, encoding="utf-8-sig")
        self.assertEqual(LocalPlugins(config).discover()[0]["name"], "plugin_echo_plugin_echo")

    def test_plugin_tool_names_are_bounded_and_collision_free(self):
        source = '''def run(arguments):
    return arguments["text"]

TOOLS = [{"name": %r, "description": "test", "read_only": True,
          "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                         "required": ["text"], "additionalProperties": False},
          "run": run}]
'''
        root = Path(self.temp.name)
        first = root / "a_b.py"
        second = root / "a.py"
        long_name = root / ("n" * 32 + ".py")
        first.write_text(source % "c", encoding="utf-8")
        second.write_text(source % "b_c", encoding="utf-8")
        long_name.write_text(source % ("t" * 32), encoding="utf-8")
        config = root / "names.json"
        config.write_text(json.dumps([
            {"path": first.name}, {"path": second.name}, {"path": long_name.name},
        ]), encoding="utf-8")

        names = [item["name"] for item in LocalPlugins(config).discover()]

        self.assertEqual(len(names), 3)
        self.assertEqual(len(set(names)), 3)
        self.assertTrue(all(len(name) <= 64 for name in names))
        self.assertNotIn("plugin_a_b_c", names)

    def test_channel_router_rejects_unknown_sender_and_deduplicates_messages(self):
        router = ChannelRouter(self.store, "test", {"alice", "bob"})
        message = TextMessage("alice", "conversation", "message-1", "你好")
        handled = []
        delivered = []

        async def handler(session, incoming):
            handled.append((session["id"], incoming.message_id))
            return "收到"

        async def send(conversation_id, text, delivery_id):
            delivered.append((conversation_id, text, delivery_id))

        async def scenario():
            first = await router.process(message, handler, send)
            duplicate = await router.process(message, handler, send)
            with self.assertRaisesRegex(ChannelAccessError, "其他用户"):
                router.ensure_session("bob", "conversation")
            with self.assertRaisesRegex(ChannelAccessError, "接入权限"):
                router.ensure_session("charlie", "new-conversation")
            return first, duplicate

        first, duplicate = asyncio.run(scenario())
        self.assertEqual(first.response, "收到")
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(len(handled), 1)
        self.assertEqual(len(delivered), 1)
        self.assertEqual(len(self.store.list_sessions("chat")), 1)

    def test_channel_delivery_failure_retries_without_rerunning_handler(self):
        router = ChannelRouter(self.store, "test", {"alice"})
        message = TextMessage("alice", "conversation", "message-2", "你好")
        handled = []
        sends = []

        async def handler(session, incoming):
            handled.append(incoming.message_id)
            return "只生成一次"

        async def failing_send(conversation_id, text, delivery_id):
            sends.append(delivery_id)
            raise ConnectionError("down")

        async def successful_send(conversation_id, text, delivery_id):
            sends.append(delivery_id)

        async def scenario():
            with self.assertRaises(ChannelDeliveryError):
                await router.process(message, handler, failing_send)
            retried = await router.retry_delivery(message.message_id, "conversation", successful_send)
            duplicate = await router.process(message, handler, successful_send)
            return retried, duplicate

        retried, duplicate = asyncio.run(scenario())
        self.assertTrue(retried.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(handled, ["message-2"])
        self.assertEqual(sends[0], sends[1])

    def test_plugin_timeout_is_reported_as_possibly_still_running(self):
        root = Path(self.temp.name)
        plugin = root / "slow_plugin.py"
        plugin.write_text(
            "import time\n"
            "def slow(arguments):\n"
            "    time.sleep(0.05)\n"
            "    return arguments['text']\n"
            "TOOLS = [{'name':'slow','description':'slow','read_only':True,"
            "'parameters':{'type':'object','properties':{'text':{'type':'string'}},"
            "'required':['text'],'additionalProperties':False},'run':slow}]\n",
            encoding="utf-8",
        )
        config = root / "slow.json"
        config.write_text(json.dumps([{"path": plugin.name}]), encoding="utf-8")
        plugins = LocalPlugins(config)
        item = plugins.discover()[0]
        with patch("career_agent.plugins.PLUGIN_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(PluginTimeout):
                asyncio.run(plugins.call(item, {"text": "hello"}))

            class Provider:
                def __init__(self):
                    self.turn = 0

                async def complete(self, messages, tools):
                    self.turn += 1
                    if self.turn == 1:
                        return {"tool_calls": [{"id": "slow-1", "function": {
                            "name": item["name"], "arguments": '{"text":"hello"}',
                        }}]}
                    return {"content": messages[-1]["content"]}

            chat = self.store.create("chat")
            self.store.set_tool_grants(chat["id"], [item["name"]])
            running = self.store.claim_run(chat["id"], "chat", "active", "调用插件")
            result = asyncio.run(AgentRuntime(self.store, Provider(), plugins=plugins).run(running))
        self.assertIn('"error": "plugin_timeout"', result["message"])
        self.assertIn('"may_still_be_running": true', result["message"])

    def test_plugin_exception_has_stable_error_code(self):
        root = Path(self.temp.name)
        plugin = root / "broken_plugin.py"
        plugin.write_text(
            "def broken(arguments):\n"
            "    raise RuntimeError('private detail')\n"
            "TOOLS = [{'name':'broken','description':'broken','read_only':True,"
            "'parameters':{'type':'object','properties':{},'additionalProperties':False},"
            "'run':broken}]\n",
            encoding="utf-8",
        )
        config = root / "broken.json"
        config.write_text(json.dumps([{"path": plugin.name}]), encoding="utf-8")
        plugins = LocalPlugins(config)
        item = plugins.discover()[0]

        class Provider:
            def __init__(self):
                self.turn = 0

            async def complete(self, messages, tools):
                self.turn += 1
                if self.turn == 1:
                    return {"tool_calls": [{"id": "broken-1", "function": {
                        "name": item["name"], "arguments": "{}",
                    }}]}
                return {"content": messages[-1]["content"]}

        chat = self.store.create("chat")
        self.store.set_tool_grants(chat["id"], [item["name"]])
        running = self.store.claim_run(chat["id"], "chat", "active", "调用故障插件")
        result = asyncio.run(AgentRuntime(self.store, Provider(), plugins=plugins).run(running))
        self.assertIn('"error": "plugin_tool_failed"', result["message"])
        self.assertNotIn("private detail", result["message"])

    def test_console_channel_ends_on_exit_and_end_of_input(self):
        channel = ConsoleChannel()
        with patch("builtins.input", side_effect=["你好", "  /exit  "]):
            self.assertEqual(asyncio.run(channel.receive()).text, "你好")
            self.assertIsNone(asyncio.run(channel.receive()))
        with patch("builtins.input", side_effect=EOFError):
            self.assertIsNone(asyncio.run(channel.receive()))
        with patch("builtins.print") as output:
            asyncio.run(channel.send("console", "文本", "delivery-1"))
        output.assert_called_once_with("文本")

    def test_cli_channel_uses_runtime_and_persists_session(self):
        class Channel:
            def __init__(self):
                self.inputs = iter([TextMessage("local", "test", "message-1", "你好"), None])
                self.outputs = []
                self.sender_id = "local"
                self.conversation_id = "test"
                self.allowed_senders = {"local"}
                self.name = "test"

            async def receive(self):
                return next(self.inputs)

            async def send(self, conversation_id, text, delivery_id):
                self.outputs.append(text)

        channel = Channel()
        session_id = asyncio.run(run_channel(channel, self.store))
        self.assertIn("演示模式已收到：你好", channel.outputs[-1])
        self.assertEqual(len(self.store.messages(session_id, "chat")), 2)

    def test_cli_channel_retry_reuses_unanswered_message(self):
        class Channel:
            def __init__(self):
                self.inputs = iter([TextMessage("local", "test", "message-2", "/retry"), None])
                self.outputs = []
                self.sender_id = "local"
                self.conversation_id = "test"
                self.allowed_senders = {"local"}
                self.name = "test"

            async def receive(self):
                return next(self.inputs)

            async def send(self, conversation_id, text, delivery_id):
                self.outputs.append(text)

        chat = self.store.create("chat")
        pending = self.store.claim_run(chat["id"], "chat", "active", "只保存一次")
        self.store.fail_run(chat["id"], pending["version"], "chat", "model_unavailable")
        channel = Channel()
        session_id = asyncio.run(run_channel(channel, self.store, session_id=chat["id"]))
        self.assertEqual(session_id, chat["id"])
        messages = self.store.messages(chat["id"], "chat")
        self.assertEqual([item["role"] for item in messages], ["user", "assistant"])
        self.assertEqual(messages[0]["content"], "只保存一次")

    def test_cli_interview_flow_reuses_store_and_runtime(self):
        catalog = ModelCatalog()
        mcp = MCPServers()
        plugins = LocalPlugins()

        async def scenario():
            created = await run_interview_action(argparse.Namespace(
                action="create", goal="后端开发", questions=1, model="default",
                material=[], claim=[],
            ), self.store, catalog, mcp, plugins)
            interview = created["session"]
            with self.assertRaisesRegex(ValueError, "无法跳过"):
                await run_interview_action(argparse.Namespace(
                    action="skip", session_id=interview["id"], question_number=1,
                ), self.store, catalog, mcp, plugins)
            self.assertEqual(self.store.get(interview["id"], "interview")["status"], "new")

            started = await run_interview_action(argparse.Namespace(
                action="start", session_id=interview["id"],
            ), self.store, catalog, mcp, plugins)
            self.assertIn("第 1 题", started["message"])
            answered = await run_interview_action(argparse.Namespace(
                action="answer", session_id=interview["id"], text="我负责后端接口优化",
            ), self.store, catalog, mcp, plugins)
            self.assertEqual(answered["session"]["status"], "finished")
            report = await run_interview_action(argparse.Namespace(
                action="report", session_id=interview["id"],
            ), self.store, catalog, mcp, plugins)
            self.assertEqual(report["report"]["status"], "finished")

            practice = await run_interview_action(argparse.Namespace(
                action="practice", session_id=interview["id"], question_number=1,
            ), self.store, catalog, mcp, plugins)
            practice_id = practice["session"]["id"]
            paused = await run_interview_action(argparse.Namespace(
                action="pause", session_id=practice_id,
            ), self.store, catalog, mcp, plugins)
            self.assertEqual(paused["session"]["status"], "paused")
            resumed = await run_interview_action(argparse.Namespace(
                action="resume", session_id=practice_id,
            ), self.store, catalog, mcp, plugins)
            self.assertEqual(resumed["session"]["status"], "awaiting_answer")
            cancelled = await run_interview_action(argparse.Namespace(
                action="cancel", session_id=practice_id,
            ), self.store, catalog, mcp, plugins)
            self.assertEqual(cancelled["session"]["status"], "cancelled")
            return interview

        interview = asyncio.run(scenario())
        self.assertIn("模型 default", session_banner(self.store, interview))
        self.assertIn("model_invalid_output", cli_error_text(
            ModelError("model_invalid_output", "bad")
        ))

    def test_cli_memory_candidate_actions(self):
        chat = self.store.create("chat")
        self.store.claim_run(chat["id"], "chat", "active", "我偏好后端")
        message_id = self.store.messages(chat["id"], "chat")[0]["id"]
        created = run_memory_candidate_action(argparse.Namespace(
            action="create", session_id=chat["id"], content="偏好后端", source=[message_id],
        ), self.store)["candidate"]
        edited = run_memory_candidate_action(argparse.Namespace(
            action="edit", candidate_id=created["id"], content="长期偏好后端",
        ), self.store)["candidate"]
        self.assertEqual(edited["content"], "长期偏好后端")
        confirmed = run_memory_candidate_action(argparse.Namespace(
            action="confirm", candidate_id=created["id"],
        ), self.store)["candidate"]
        self.assertEqual(confirmed["status"], "confirmed")
        listed = run_memory_candidate_action(argparse.Namespace(
            action="list", session_id=chat["id"], status="confirmed",
        ), self.store)["candidates"]
        self.assertEqual([item["id"] for item in listed], [created["id"]])
        with self.assertRaisesRegex(ValueError, "不能删除"):
            run_memory_candidate_action(argparse.Namespace(
                action="delete", candidate_id=created["id"],
            ), self.store)

    def test_cli_parser_exposes_interview_and_candidate_workflows(self):
        parser = build_parser()
        interview = parser.parse_args([
            "interview", "create", "后端开发", "--questions", "2",
            "--material", "m1", "--claim", "c1",
        ])
        self.assertEqual((interview.action, interview.questions), ("create", 2))
        self.assertEqual(interview.material, ["m1"])
        self.assertEqual(interview.claim, ["c1"])
        answer = parser.parse_args(["interview", "answer", "session-1", "回答内容"])
        self.assertEqual(answer.text, "回答内容")
        candidate = parser.parse_args([
            "candidate", "create", "chat-1", "偏好后端", "--source", "7",
        ])
        self.assertEqual(candidate.candidate_action, "create")
        self.assertEqual(candidate.source, [7])

    def test_cli_main_creates_interview_in_selected_data_dir(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent / ".tmp") as directory:
            environment = {
                "DATA_DIR": directory,
                "AUTH_REQUIRED": "false",
                "MODEL_PROVIDER": "demo",
            }
            with patch.dict(os.environ, environment), patch.object(sys, "argv", [
                "nico-agent", "interview", "create", "后端开发", "--questions", "1",
            ]), patch("builtins.print"):
                cli_main()
            sessions = Store(Path(directory) / "career_agent.sqlite3").list_sessions("interview")
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0]["status"], "new")
