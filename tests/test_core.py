import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import httpx

from career_agent.context import MAX_CONTEXT_CHARS
from career_agent.models import ModelCatalog
from career_agent.provider import ModelError, Provider
from career_agent.runtime import MAX_MODEL_OUTPUT_CHARS, AgentRuntime
from career_agent.store import Store


def run_demo(coroutine):
    """在真实事件循环中运行演示调用。"""
    return asyncio.run(coroutine)


class CoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.store = Store(Path(self.temp.name) / "test.sqlite3")
        self.runtime = AgentRuntime(self.store, Provider())

    def finish_interview(self, interview, answer_prefix="第"):
        running = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(running))
        for number in range(1, 4):
            current = self.store.claim_run(
                interview["id"], "interview", "awaiting_answer", f"{answer_prefix} {number} 题回答"
            )
            run_demo(self.runtime.run(current))

    def tearDown(self):
        self.temp.cleanup()

    def test_compatible_provider_sends_output_limit_and_keeps_usage(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, headers, json):
                self.payload = json
                return httpx.Response(200, json={
                    "choices": [{"message": {"content": "回复"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                }, request=httpx.Request("POST", url))

        client = Client()
        provider = Provider()
        provider.mode = "openai_compatible"
        provider.model = "test-model"
        with patch("career_agent.providers.transport.httpx.AsyncClient", return_value=client):
            reply = asyncio.run(provider.complete([{"role": "user", "content": "问题"}], []))
        self.assertEqual(client.payload["max_tokens"], 2048)
        self.assertEqual(reply["usage"]["total_tokens"], 6)
        self.assertEqual(reply["finish_reason"], "stop")

    def test_anthropic_provider_runs_tool_cycle_with_native_messages(self):
        class Client:
            def __init__(self):
                self.requests = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, headers, json):
                self.requests.append((url, headers, json))
                if len(self.requests) == 1:
                    body = {"content": [{"type": "tool_use", "id": "tool-1",
                                         "name": "read_confirmed_claims", "input": {}}],
                            "stop_reason": "tool_use", "usage": {"input_tokens": 12, "output_tokens": 4}}
                else:
                    body = {"content": [{"type": "text", "text": "当前没有已确认的经历。"}],
                            "stop_reason": "end_turn", "usage": {"input_tokens": 20, "output_tokens": 8}}
                return httpx.Response(200, json=body, request=httpx.Request("POST", url))

        client = Client()
        provider = Provider()
        provider.mode = "anthropic"
        provider.model = "claude-test"
        provider.api_key = "test-key"
        provider.base_url = "https://api.anthropic.com/v1"
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "看看我已确认的经历")
        with patch("career_agent.providers.transport.httpx.AsyncClient", return_value=client):
            result = run_demo(AgentRuntime(self.store, provider).run(running))
        self.assertIn("没有已确认的经历", result["message"])
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.requests[0][0], "https://api.anthropic.com/v1/messages")
        self.assertEqual(client.requests[0][1]["anthropic-version"], "2026-10-21")
        self.assertIn("求职", client.requests[0][2]["system"])
        self.assertEqual(client.requests[0][2]["tools"][1]["name"], "read_confirmed_claims")
        second = client.requests[1][2]["messages"]
        self.assertEqual(second[-2]["content"][0]["type"], "tool_use")
        self.assertEqual(second[-1]["content"][0]["tool_use_id"], "tool-1")
        run = next(item for item in self.store.events(chat["id"], "chat") if item["kind"] == "model_run")
        self.assertEqual(json.loads(run["detail"])["usage"]["total_tokens"], 44)

    def test_anthropic_truncated_tool_call_is_not_executed(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, headers, json):
                return httpx.Response(200, json={
                    "content": [{"type": "tool_use", "id": "tool-1", "name": "read_confirmed_claims",
                                 "input": {}}],
                    "stop_reason": "max_tokens", "usage": {"input_tokens": 10, "output_tokens": 20},
                }, request=httpx.Request("POST", url))

        provider = Provider()
        provider.mode = "anthropic"
        provider.model = "claude-test"
        provider.api_key = "test-key"
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "看看我已确认的经历")
        with patch("career_agent.providers.transport.httpx.AsyncClient", return_value=Client()):
            with self.assertRaises(ModelError) as caught:
                run_demo(AgentRuntime(self.store, provider).run(running))
        self.assertEqual(caught.exception.code, "model_output_truncated")
        self.assertFalse(any(item["kind"] == "tool" for item in self.store.events(chat["id"], "chat")))

    def test_provider_reports_configuration_errors_before_request(self):
        provider = Provider()
        provider.mode = "openai_compatible"
        provider.model = ""
        self.assertEqual(provider.configuration_error(), "请设置 MODEL_NAME")
        with self.assertRaises(ModelError) as caught:
            run_demo(provider.complete([], []))
        self.assertEqual(caught.exception.code, "model_config_invalid")
        provider.model = "test-model"
        provider.base_url = "not-a-url"
        self.assertIn("MODEL_API_BASE_URL", provider.configuration_error())
        provider.base_url = "http://[invalid"
        self.assertIn("MODEL_API_BASE_URL", provider.configuration_error())

    def test_model_catalog_requires_tools_and_keeps_keys_out_of_listing(self):
        path = Path(self.temp.name) / "models.json"
        path.write_text(json.dumps([{"id": "claude", "provider": "anthropic", "model": "claude-test",
                                     "supports_tools": True, "api_key_env": "MODEL_CLAUDE_KEY"}]),
                        encoding="utf-8")
        with patch.dict("os.environ", {"MODEL_CLAUDE_KEY": "private-key"}):
            catalog = ModelCatalog(path)
        self.assertEqual(catalog.get("claude").api_key, "private-key")
        self.assertNotIn("private-key", json.dumps(catalog.list()))
        with self.assertRaises(ValueError):
            catalog.get("missing")
        path.write_text(json.dumps([{"id": "no-tools", "provider": "anthropic", "model": "claude-test",
                                     "supports_tools": False}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            ModelCatalog(path)

    def test_truncated_model_reply_keeps_input_and_skips_tools(self):
        class TruncatedProvider:
            async def complete(self, messages, tools):
                return {"content": None, "finish_reason": "length", "usage": {
                    "prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6,
                }, "tool_calls": [{"id": "incomplete", "function": {
                    "name": "read_confirmed_claims", "arguments": "{",
                }}]}

        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "请整理我的经历")
        with self.assertRaises(ModelError) as caught:
            run_demo(AgentRuntime(self.store, TruncatedProvider()).run(running))
        self.assertEqual(caught.exception.code, "model_output_truncated")
        self.assertEqual([item["role"] for item in self.store.messages(chat["id"], "chat")], ["user"])
        events = self.store.events(chat["id"], "chat")
        self.assertFalse(any(item["kind"] == "tool" for item in events))
        detail = json.loads(next(item["detail"] for item in events if item["kind"] == "model_run"))
        self.assertEqual(detail["usage"]["total_tokens"], 6)

    def test_incomplete_model_reply_keeps_input_and_skips_tools(self):
        class FilteredProvider:
            async def complete(self, messages, tools):
                return {"content": "不完整的回复", "finish_reason": "content_filter", "tool_calls": [{
                    "id": "filtered", "function": {"name": "read_confirmed_claims", "arguments": "{}"},
                }]}

        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "请整理我的经历")
        with self.assertRaises(ModelError) as caught:
            run_demo(AgentRuntime(self.store, FilteredProvider()).run(running))
        self.assertEqual(caught.exception.code, "model_incomplete")
        self.assertEqual(self.store.get(chat["id"], "chat")["status"], "active")
        self.assertEqual([item["role"] for item in self.store.messages(chat["id"], "chat")], ["user"])
        events = self.store.events(chat["id"], "chat")
        self.assertFalse(any(item["kind"] == "tool" for item in events))
        detail = json.loads(next(item["detail"] for item in events if item["kind"] == "model_run"))
        self.assertEqual(detail["error_code"], "model_incomplete")

    def test_tool_finish_without_calls_is_rejected(self):
        class BrokenProvider:
            async def complete(self, messages, tools):
                return {"content": "看似完整", "finish_reason": "tool_calls"}

        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "请整理我的经历")
        with self.assertRaises(ModelError) as caught:
            run_demo(AgentRuntime(self.store, BrokenProvider()).run(running))
        self.assertEqual(caught.exception.code, "model_invalid_response")
        self.assertEqual([item["role"] for item in self.store.messages(chat["id"], "chat")], ["user"])

    def test_interview_accepts_fenced_json_with_valid_schema(self):
        class FencedProvider:
            async def complete(self, messages, tools):
                content = json.dumps({"feedback": "", "question": "请介绍一个项目", "finished": False,
                                      "evidence": [], "summary": ""}, ensure_ascii=False)
                return {"content": f"```json\n{content}\n```", "finish_reason": "stop"}

        interview = self.store.create("interview", "开发岗位")
        running = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(AgentRuntime(self.store, FencedProvider()).run(running))
        self.assertEqual(self.store.interview_report(interview["id"])["items"][0]["question"], "请介绍一个项目")

    def test_provider_error_does_not_expose_upstream_response(self):
        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, url, headers, json):
                return httpx.Response(400, text="PRIVATE_UPSTREAM_BODY", request=httpx.Request("POST", url))

        provider = Provider()
        provider.mode = "openai_compatible"
        provider.model = "test-model"
        with patch("career_agent.providers.transport.httpx.AsyncClient", return_value=Client()):
            with self.assertRaises(ModelError) as caught:
                asyncio.run(provider.complete([{"role": "user", "content": "问题"}], []))
        self.assertEqual(caught.exception.code, "model_request_rejected")
        self.assertNotIn("PRIVATE_UPSTREAM_BODY", str(caught.exception))

    def test_existing_database_gains_new_columns_without_losing_rows(self):
        legacy_path = Path(self.temp.name) / "legacy.sqlite3"
        with closing(sqlite3.connect(legacy_path)) as db:
            db.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, kind TEXT, status TEXT, goal TEXT,
                    question_count INTEGER, version INTEGER, created_at TEXT
                );
                CREATE TABLE materials (
                    id TEXT PRIMARY KEY, kind TEXT, title TEXT, content TEXT, created_at TEXT
                );
                CREATE TABLE claims (
                    id TEXT PRIMARY KEY, material_id TEXT, text TEXT, status TEXT, created_at TEXT
                );
                CREATE TABLE interview_items (
                    session_id TEXT, number INTEGER, question TEXT, answer TEXT, feedback TEXT,
                    PRIMARY KEY (session_id, number)
                );
                INSERT INTO sessions VALUES ('legacy-chat', 'chat', 'active', '', 0, 0, '2026-01-01');
                INSERT INTO materials VALUES ('legacy-material', 'project', '旧材料', '原始内容', '2026-01-01');
                INSERT INTO claims VALUES ('legacy-claim', 'legacy-material', '原始经历', 'confirmed', '2026-01-01');
            """)
        upgraded = Store(legacy_path)
        self.assertEqual(upgraded.get("legacy-chat", "chat")["max_questions"], 3)
        self.assertEqual(upgraded.get("legacy-chat", "chat")["chat_memory"], "")
        self.assertEqual(upgraded.get("legacy-chat", "chat")["title"], "")
        upgraded.set_chat_memory("legacy-chat", "迁移后备注")
        self.assertEqual(upgraded.get("legacy-chat", "chat")["chat_memory"], "迁移后备注")
        with closing(sqlite3.connect(legacy_path)) as db:
            self.assertIn("skipped", [row[1] for row in db.execute("PRAGMA table_info(interview_items)")])
        self.assertEqual(upgraded.list_materials()[0]["content"], "原始内容")
        self.assertEqual(upgraded.list_claims()[0]["text"], "原始经历")
        self.assertTrue(upgraded.revoke_material("legacy-material"))
        self.assertEqual(upgraded.list_claims(), [])

    def test_interview_context_excludes_chat_history(self):
        chat = self.store.create("chat")
        self.store.set_chat_memory(chat["id"], "CHAT_MEMORY_SECRET")
        chat_run = self.store.claim_run(chat["id"], "chat", "active", "CHAT_SECRET_123")
        self.store.finish_run(chat["id"], chat_run["version"], "私密聊天回复", "active", 0)
        interview = self.store.create("interview", "后端工程师")
        interview_run = self.store.claim_run(interview["id"], "interview", "new")
        context = json.dumps(self.runtime.build_context(interview_run), ensure_ascii=False)
        self.assertNotIn("CHAT_SECRET_123", context)
        self.assertNotIn("CHAT_MEMORY_SECRET", context)
        self.assertNotIn("私密聊天回复", context)
        self.assertIn("后端工程师", context)

    def test_skipped_question_rejects_invented_feedback(self):
        class InventingProvider:
            async def complete(self, messages, tools):
                return {"content": json.dumps({
                    "feedback": "这题回答很好", "question": "", "finished": True,
                    "evidence": ["跳过本题"], "summary": "演示总结",
                }, ensure_ascii=False)}

        interview = self.store.create("interview", "后端工程师", max_questions=1)
        first = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(first))
        skipped = self.store.skip_interview_question(interview["id"], 1)
        with self.assertRaisesRegex(ValueError, "跳过的题目"):
            run_demo(AgentRuntime(self.store, InventingProvider()).run(skipped))
        report = self.store.interview_report(interview["id"])
        self.assertEqual(report["status"], "paused")
        self.assertIsNone(report["items"][0]["feedback"])

    def test_interview_rejects_invalid_first_question_output(self):
        class FixedProvider:
            def __init__(self, content):
                self.content = content

            async def complete(self, messages, tools):
                return {"content": self.content}

        cases = [
            json.dumps(["不是对象"]),
            json.dumps({"feedback": "", "question": "题目", "finished": False}),
            json.dumps({"feedback": "虚构反馈", "question": "题目", "finished": False,
                        "evidence": [], "summary": ""}),
            json.dumps({"feedback": "", "question": "题目", "finished": False,
                        "evidence": [], "summary": "提前总结"}),
            json.dumps({"feedback": "", "question": "题目" * 501, "finished": False,
                        "evidence": [], "summary": ""}),
        ]
        for content in cases:
            with self.subTest(content=content[:60]):
                interview = self.store.create("interview", "后端工程师")
                running = self.store.claim_run(interview["id"], "interview", "new")
                with self.assertRaises(ValueError):
                    run_demo(AgentRuntime(self.store, FixedProvider(content)).run(running))
                self.assertEqual(self.store.interview_report(interview["id"])["items"], [])

    def test_chat_rejects_oversized_model_output(self):
        class OversizedProvider:
            async def complete(self, messages, tools):
                return {"content": "字" * (MAX_MODEL_OUTPUT_CHARS + 1)}

        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "请回复")
        with self.assertRaises(ValueError):
            run_demo(AgentRuntime(self.store, OversizedProvider()).run(running))
        self.assertEqual([row["role"] for row in self.store.messages(chat["id"], "chat")], ["user"])

    def test_demo_chat_accepts_maximum_length_input(self):
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "甲" * 12000)
        result = run_demo(self.runtime.run(running))
        self.assertLess(len(result["message"]), MAX_MODEL_OUTPUT_CHARS)
        self.assertIn("演示模式已收到：", result["message"])
        self.assertEqual(self.store.get(chat["id"], "chat")["status"], "active")

    def test_interview_rejects_next_question_after_last_answer(self):
        class InvalidFinalProvider:
            async def complete(self, messages, tools):
                return {"content": json.dumps({
                    "feedback": "回答有依据", "question": "多余题目", "finished": True,
                    "evidence": ["原始回答"], "summary": "全场总结",
                }, ensure_ascii=False)}

        interview = self.store.create("interview", "后端工程师", max_questions=1)
        first = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(first))
        answer = self.store.claim_run(interview["id"], "interview", "awaiting_answer", "原始回答")
        with self.assertRaisesRegex(ValueError, "结束时不能生成下一题"):
            run_demo(AgentRuntime(self.store, InvalidFinalProvider()).run(answer))
        report = self.store.interview_report(interview["id"])
        self.assertEqual(report["items"][0]["answer"], "原始回答")
        self.assertIsNone(report["items"][0]["feedback"])

    def test_chat_memory_survives_context_window(self):
        chat = self.store.create("chat")
        self.store.set_chat_memory(chat["id"], "重点准备后端岗位")
        for number in range(9):
            running = self.store.claim_run(chat["id"], "chat", "active", f"旧消息-{number}")
            self.store.finish_run(chat["id"], running["version"], "收到", "active", 0)
        latest = self.store.claim_run(chat["id"], "chat", "active", "继续讨论")
        context = json.dumps(self.runtime.build_context(latest), ensure_ascii=False)
        self.assertIn("[CHAT_MEMORY]", context)
        self.assertIn("重点准备后端岗位", context)
        self.assertNotIn("旧消息-0", context)

    def test_ten_question_summary_context_keeps_early_evidence(self):
        interview = self.store.create("interview", "后端工程师", max_questions=10)
        first = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(first))
        for number in range(1, 10):
            answer = self.store.claim_run(
                interview["id"], "interview", "awaiting_answer", f"第 {number} 题的独有回答"
            )
            run_demo(self.runtime.run(answer))
        final = self.store.claim_run(interview["id"], "interview", "awaiting_answer", "最后一题回答")
        messages = self.runtime.build_context(final)
        context = json.dumps(messages, ensure_ascii=False)
        self.assertIn("[INTERVIEW_PROGRESS]", context)
        self.assertIn("第 1 题的独有回答", context)
        self.assertLessEqual(sum(len(item["content"]) for item in messages), MAX_CONTEXT_CHARS)

    def test_model_context_is_bounded_without_truncating_transcript(self):
        chat = self.store.create("chat")
        for number in range(7):
            running = self.store.claim_run(
                chat["id"], "chat", "active", f"用户-{number}-" + "甲" * 4000
            )
            self.store.finish_run(chat["id"], running["version"], "回复-" + "乙" * 4000, "active", 0)
        latest = self.store.claim_run(chat["id"], "chat", "active", "LATEST_USER_MESSAGE")
        context = self.runtime.build_context(latest)
        self.assertLessEqual(sum(len(item["content"]) for item in context), MAX_CONTEXT_CHARS)
        self.assertEqual(context[-1]["content"], "LATEST_USER_MESSAGE")
        self.assertNotIn("用户-0-", json.dumps(context, ensure_ascii=False))
        self.assertEqual(len(self.store.messages(chat["id"], "chat")), 15)

    def test_oversized_chat_memory_yields_to_the_latest_message(self):
        chat = self.store.create("chat")
        self.store.set_chat_memory(chat["id"], "备" * MAX_CONTEXT_CHARS)
        latest = self.store.claim_run(chat["id"], "chat", "active", "LATEST_USER_MESSAGE")
        context = self.runtime.build_context(latest)
        self.assertLessEqual(sum(len(item["content"]) for item in context), MAX_CONTEXT_CHARS)
        self.assertEqual(context[-1]["content"], "LATEST_USER_MESSAGE")
        self.assertNotIn("[CHAT_MEMORY]", json.dumps(context, ensure_ascii=False))

    def test_run_without_room_for_the_latest_message_reports_a_budget_error(self):
        chat = self.store.create("chat")
        latest = self.store.claim_run(chat["id"], "chat", "active", "问" * (MAX_CONTEXT_CHARS + 1))
        with self.assertRaises(ModelError) as caught:
            run_demo(self.runtime.run(latest))
        self.assertEqual(caught.exception.code, "context_budget_exceeded")
        self.assertEqual(caught.exception.status_code, 413)
        self.assertEqual(self.store.get(chat["id"], "chat")["status"], "active")
        events = [event for event in self.store.events(chat["id"], "chat")
                  if event["kind"] == "model_run"]
        self.assertEqual(json.loads(events[-1]["detail"])["error_code"], "context_budget_exceeded")

    def test_run_event_records_metadata_without_message_content(self):
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "PRIVATE_CHAT_TEXT")
        run_demo(self.runtime.run(running))
        events = [event for event in self.store.events(chat["id"], "chat") if event["kind"] == "model_run"]
        self.assertEqual(len(events), 1)
        detail = json.loads(events[0]["detail"])
        self.assertEqual(detail["status"], "completed")
        self.assertGreater(detail["context_chars"], 0)
        self.assertNotIn("PRIVATE_CHAT_TEXT", events[0]["detail"])

    def test_run_events_keep_only_the_newest_per_session(self):
        chat = self.store.create("chat")
        other = self.store.create("interview", "后端工程师")
        with patch("career_agent.store.MAX_EVENTS_PER_SESSION", 5):
            for number in range(8):
                self.store.event(chat["id"], "model_run", {"version": number})
            self.store.event(other["id"], "model_run", {"version": 0})
        kept = [json.loads(item["detail"])["version"] for item in self.store.events(chat["id"], "chat")]
        self.assertEqual(kept, [3, 4, 5, 6, 7])
        self.assertEqual(len(self.store.events(other["id"], "interview")), 1)

    def test_model_usage_and_prompt_version_are_recorded(self):
        class MeteredProvider:
            async def complete(self, messages, tools):
                return {"content": "回复", "usage": {
                    "prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                }}

        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "测试用量")
        run_demo(AgentRuntime(self.store, MeteredProvider()).run(running))
        event = next(item for item in self.store.events(chat["id"], "chat") if item["kind"] == "model_run")
        detail = json.loads(event["detail"])
        self.assertEqual(detail["usage"]["total_tokens"], 15)
        self.assertEqual(detail["usage_coverage"], "exact")
        self.assertTrue(detail["prompt_version"])

    def test_duplicate_run_does_not_call_model_twice(self):
        class SlowProvider:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                self.started.set()
                await self.release.wait()
                return {"content": "只生成一次"}

        async def scenario():
            provider = SlowProvider()
            runtime = AgentRuntime(self.store, provider)
            chat = self.store.create("chat")
            running = self.store.claim_run(chat["id"], "chat", "active", "问题")
            first = asyncio.create_task(runtime.run(running))
            await provider.started.wait()
            duplicate = await AgentRuntime(self.store, provider).run(running)
            provider.release.set()
            completed = await first
            return provider.calls, duplicate, completed

        calls, duplicate, completed = asyncio.run(scenario())
        self.assertEqual(calls, 1)
        self.assertTrue(duplicate["in_progress"])
        self.assertEqual(completed["message"], "只生成一次")

    def test_tool_call_limit_rejects_oversized_batch(self):
        class ExcessProvider:
            async def complete(self, messages, tools):
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": str(number), "type": "function", "function": {
                        "name": "read_confirmed_claims", "arguments": "{}",
                    }} for number in range(9)
                ]}

        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "请整理经历")
        with self.assertRaises(ValueError):
            run_demo(AgentRuntime(self.store, ExcessProvider()).run(running))
        self.assertEqual(self.store.get(chat["id"], "chat")["status"], "active")
        events = [event for event in self.store.events(chat["id"], "chat") if event["kind"] == "model_run"]
        self.assertEqual(json.loads(events[0]["detail"])["status"], "failed")

    def test_tool_results_cannot_overflow_model_input(self):
        class RepeatingToolProvider:
            def __init__(self):
                self.calls = 0

            async def complete(self, messages, tools):
                self.calls += 1
                return {"role": "assistant", "content": None, "tool_calls": [
                    {"id": str(number), "type": "function", "function": {
                        "name": "read_confirmed_claims", "arguments": "{}",
                    }} for number in range(8)
                ]}

        for number in range(20):
            claim = self.store.create_claim(f"经历 {number} " + "甲" * 490)
            self.store.confirm_claim(claim["id"])
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "请整理经历")
        provider = RepeatingToolProvider()
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentRuntime(self.store, provider).run(running))
        self.assertEqual(caught.exception.code, "context_budget_exceeded")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(self.store.messages(chat["id"], "chat")[-1]["content"], "请整理经历")

    def test_confirmed_claim_tool_pages_and_filters_results(self):
        for number in range(25):
            claim = self.store.create_claim(f"经历 {number:02d}")
            self.store.confirm_claim(claim["id"])
        self.store.create_claim("未确认经历")
        revoked = self.store.create_claim("已撤回经历")
        self.store.confirm_claim(revoked["id"])
        self.store.revoke_claim(revoked["id"])
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", "整理经历")

        def read(arguments):
            call = {"id": "claim-page", "function": {
                "name": "read_confirmed_claims", "arguments": json.dumps(arguments),
            }}
            return json.loads(self.runtime._execute_tool_sync(running, call)["content"])

        first = read({})
        second = read({"offset": first["next_offset"]})
        self.assertEqual(len(first["claims"]), 20)
        self.assertEqual(len(second["claims"]), 5)
        self.assertEqual(second["next_offset"], None)
        self.assertEqual(len({item["id"] for item in first["claims"] + second["claims"]}), 25)
        self.assertEqual([item["text"] for item in read({"query": "经历 03"})["claims"]], ["经历 03"])
        for arguments in ({"offset": -1}, {"offset": True}, {"query": "x" * 101}, {"extra": "x"}):
            with self.subTest(arguments=arguments):
                self.assertEqual(read(arguments)["error"], "invalid_arguments")

    def test_tool_permission_and_explicit_report_selection(self):
        interview = self.store.create("interview", "开发岗位")
        call = {"id": "1", "function": {"name": "read_interview_feedback", "arguments": json.dumps({"interview_session_id": interview["id"]})}}
        blocked = self.runtime._execute_tool_sync(interview, call)
        self.assertEqual(json.loads(blocked["content"])["error"], "tool_not_allowed")

        chat = self.store.create("chat")
        chat_run = self.store.claim_run(chat["id"], "chat", "active", "帮我复盘面试")
        blocked = self.runtime._execute_tool_sync(chat_run, call)
        self.assertEqual(json.loads(blocked["content"])["error"], "resource_not_authorized")
        self.store.finish_run(chat["id"], chat_run["version"], "好的", "active", 0)
        self.finish_interview(interview, "SECRET_ANSWER")
        selected = self.store.claim_run(chat["id"], "chat", "active", f"请解释面试 {interview['id']} 的反馈")
        allowed = self.runtime._execute_tool_sync(selected, call)
        content = json.loads(allowed["content"])
        self.assertEqual(content["goal"], "开发岗位")
        self.assertEqual(len(content["feedback"]), 3)
        self.assertNotIn("SECRET_ANSWER", allowed["content"])

    def test_invalid_tool_arguments_do_not_read_material(self):
        material = self.store.create_material("project", "项目", "PRIVATE_MATERIAL")
        chat = self.store.create("chat")
        running = self.store.claim_run(chat["id"], "chat", "active", f"请读取 {material['id']}")
        for arguments in ("[]", json.dumps({"material_id": material["id"], "extra": "x"}),
                          json.dumps({"material_id": 123})):
            call = {"id": "1", "function": {"name": "read_material", "arguments": arguments}}
            result = json.loads(self.runtime._execute_tool_sync(running, call)["content"])
            self.assertEqual(result["error"], "invalid_arguments")

    def test_cancelled_report_can_be_discussed_without_answer_text(self):
        interview = self.store.create("interview", "开发岗位")
        first = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(first))
        answered = self.store.claim_run(interview["id"], "interview", "awaiting_answer", "SECRET_PARTIAL_ANSWER")
        run_demo(self.runtime.run(answered))
        self.store.cancel_interview(interview["id"])
        chat = self.store.create("chat")
        selected = self.store.claim_run(chat["id"], "chat", "active", f"请解释面试 {interview['id']} 的反馈")
        call = {"id": "1", "function": {"name": "read_interview_feedback", "arguments": json.dumps({
            "interview_session_id": interview["id"],
        })}}
        result = json.loads(self.runtime._execute_tool_sync(selected, call)["content"])
        self.assertEqual(len(result["feedback"]), 1)
        self.assertNotIn("SECRET_PARTIAL_ANSWER", json.dumps(result))

    def test_interrupted_run_cannot_commit_late_reply(self):
        interview = self.store.create("interview", "开发岗位")
        running = self.store.claim_run(interview["id"], "interview", "new")
        self.store.interrupt(interview["id"], "interview")
        result = run_demo(self.runtime.run(running))
        self.assertTrue(result["interrupted"])
        self.assertEqual(self.store.get(interview["id"], "interview")["status"], "paused")
        self.assertEqual(self.store.messages(interview["id"], "interview"), [])
        self.assertEqual(self.store.interview_report(interview["id"])["items"], [])

    def test_inflight_chat_followup_invalidates_old_reply(self):
        class SlowProvider:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def complete(self, messages, tools):
                if any(message.get("content") == "新消息" for message in messages):
                    return {"content": "新回复"}
                self.started.set()
                await self.release.wait()
                return {"content": "旧回复"}

        provider = SlowProvider()
        runtime = AgentRuntime(self.store, provider)
        chat = self.store.create("chat")

        async def scenario():
            old = self.store.claim_run(chat["id"], "chat", "active", "旧消息")
            old_task = asyncio.create_task(runtime.run(old))
            await provider.started.wait()
            new = self.store.claim_run(chat["id"], "chat", "active", "新消息")
            latest = await runtime.run(new)
            provider.release.set()
            stale = await old_task
            return latest, stale

        latest, stale = asyncio.run(scenario())
        self.assertEqual(latest["message"], "新回复")
        self.assertTrue(stale["interrupted"])
        self.assertEqual([item["content"] for item in self.store.messages(chat["id"], "chat")],
                         ["旧消息", "新消息", "新回复"])

    def test_deleting_session_during_model_run_discards_late_events(self):
        class SlowProvider:
            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def complete(self, messages, tools):
                self.started.set()
                await self.release.wait()
                return {"content": "迟到回复"}

        provider = SlowProvider()
        chat = self.store.create("chat")

        async def scenario():
            running = self.store.claim_run(chat["id"], "chat", "active", "问题")
            task = asyncio.create_task(AgentRuntime(self.store, provider).run(running))
            await provider.started.wait()
            self.assertTrue(self.store.delete_session(chat["id"], "chat"))
            provider.release.set()
            return await task

        self.assertTrue(asyncio.run(scenario())["interrupted"])
        self.assertIsNone(self.store.get(chat["id"], "chat"))

    def test_three_question_flow(self):
        interview = self.store.create("interview", "开发岗位")
        self.finish_interview(interview)
        final = self.store.get(interview["id"], "interview")
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["question_count"], 3)
        self.assertEqual(len(self.store.messages(interview["id"], "interview")), 7)
        report = self.store.interview_report(interview["id"])
        self.assertEqual(len(report["items"]), 3)
        self.assertEqual(report["items"][1]["answer"], "第 2 题回答")
        self.assertIn("演示反馈", report["items"][1]["feedback"])
        self.assertIn("演示反馈", report["items"][2]["feedback"])
        self.assertNotIn("演示总结", report["items"][2]["feedback"])
        self.assertEqual(report["items"][1]["evidence"], ["第 2 题回答"])
        self.assertIn("演示总结", report["summary"])

    def test_practice_uses_one_question_and_original_feedback(self):
        source = self.store.create("interview", "开发岗位")
        with self.assertRaises(ValueError):
            self.store.create_practice(source["id"], 1)
        self.finish_interview(source)
        practice = self.store.create_practice(source["id"], 2)
        context = json.dumps(self.runtime.build_context(practice), ensure_ascii=False)
        self.assertIn("[PRACTICE_SOURCE]", context)
        self.assertIn("第 2 题回答", context)
        running = self.store.claim_run(practice["id"], "interview", "new")
        first = run_demo(self.runtime.run(running))
        self.assertIn("复练", first["message"])
        answer = self.store.claim_run(practice["id"], "interview", "awaiting_answer", "换一个场景的回答")
        run_demo(self.runtime.run(answer))
        report = self.store.interview_report(practice["id"])
        self.assertEqual(report["status"], "finished")
        self.assertEqual(len(report["items"]), 1)
        self.assertEqual(report["items"][0]["answer"], "换一个场景的回答")
        self.assertIn("演示反馈", report["items"][0]["feedback"])
        self.assertIn("演示总结", report["summary"])

    def test_failed_model_call_keeps_answer_for_resume(self):
        class FailingProvider:
            async def complete(self, messages, tools):
                raise RuntimeError("模拟模型故障")

        interview = self.store.create("interview", "开发岗位")
        first = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(first))
        answer = self.store.claim_run(interview["id"], "interview", "awaiting_answer", "已保存的原始回答")
        with self.assertRaises(RuntimeError):
            run_demo(AgentRuntime(self.store, FailingProvider()).run(answer))
        self.assertEqual(self.store.get(interview["id"], "interview")["status"], "paused")
        self.assertEqual(self.store.interview_report(interview["id"])["items"][0]["answer"], "已保存的原始回答")
        resumed, needs_run = self.store.resume(interview["id"])
        self.assertTrue(needs_run)
        run_demo(self.runtime.run(resumed))
        self.assertIsNotNone(self.store.interview_report(interview["id"])["items"][0]["feedback"])

    def test_fabricated_answer_evidence_is_rejected(self):
        class FabricatingProvider:
            async def complete(self, messages, tools):
                return {"role": "assistant", "content": json.dumps({
                    "feedback": "模型声称用户有量化结果", "question": "请继续说明", "finished": False,
                    "evidence": ["不存在于回答的数字"],
                }, ensure_ascii=False)}

        interview = self.store.create("interview", "开发岗位")
        first = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(first))
        answer = self.store.claim_run(interview["id"], "interview", "awaiting_answer", "我负责排查接口问题")
        with self.assertRaises(ValueError):
            run_demo(AgentRuntime(self.store, FabricatingProvider()).run(answer))
        report = self.store.interview_report(interview["id"])
        self.assertEqual(report["items"][0]["answer"], "我负责排查接口问题")
        self.assertIsNone(report["items"][0]["feedback"])
        self.assertEqual(report["items"][0]["evidence"], [])

    def test_only_selected_materials_and_confirmed_claims_enter_interview(self):
        resume = self.store.create_material("resume", "我的简历", "已授权的项目原文")
        self.store.create_material("project", "其他项目", "UNSELECTED_SECRET")
        claim = self.store.create_claim("我负责接口延迟优化", resume["id"])
        unrelated_claim = self.store.create_claim("UNSELECTED_CLAIM")
        self.store.confirm_claim(unrelated_claim["id"])
        with self.assertRaises(ValueError):
            self.store.create("interview", "后端开发", (resume["id"],), (claim["id"],))
        self.store.confirm_claim(claim["id"])
        interview = self.store.create("interview", "后端开发", (resume["id"],), (claim["id"],))
        context = json.dumps(self.runtime.build_context(interview), ensure_ascii=False)
        self.assertIn("我的简历", context)
        self.assertIn("我负责接口延迟优化", context)
        self.assertNotIn("已授权的项目原文", context)
        self.assertNotIn("UNSELECTED_SECRET", context)
        self.assertNotIn("UNSELECTED_CLAIM", context)
        call = {"id": "1", "function": {"name": "read_interview_materials", "arguments": "{}"}}
        result = json.loads(self.runtime._execute_tool_sync(interview, call)["content"])
        self.assertEqual(result["materials"][0]["content"], "已授权的项目原文")
        self.assertEqual(len(result["materials"]), 1)
        self.assertEqual(result["claims"][0]["text"], "我负责接口延迟优化")
        self.assertNotIn("UNSELECTED_SECRET", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("UNSELECTED_CLAIM", json.dumps(result, ensure_ascii=False))
        chat = self.store.create("chat")
        blocked = json.loads(self.runtime._execute_tool_sync(chat, call)["content"])
        self.assertEqual(blocked["error"], "tool_not_allowed")

    def test_job_description_cannot_be_claim_source(self):
        jd = self.store.create_material("job_description", "岗位描述", "招聘要求")
        with self.assertRaises(ValueError):
            self.store.create_claim("我满足全部要求", jd["id"])

    def test_chat_material_requires_current_message_id(self):
        material = self.store.create_material("project", "项目", "PRIVATE_MATERIAL")
        call = {"id": "1", "function": {"name": "read_material", "arguments": json.dumps({"material_id": material["id"]})}}
        interview = self.store.create("interview", "开发岗位")
        denied = json.loads(self.runtime._execute_tool_sync(interview, call)["content"])
        self.assertEqual(denied["error"], "tool_not_allowed")
        chat = self.store.create("chat")
        first = self.store.claim_run(chat["id"], "chat", "active", "请分析我的项目")
        denied = json.loads(self.runtime._execute_tool_sync(first, call)["content"])
        self.assertEqual(denied["error"], "resource_not_authorized")
        self.store.finish_run(chat["id"], first["version"], "好的", "active", 0)
        selected = self.store.claim_run(chat["id"], "chat", "active", f"请分析材料 {material['id']}")
        allowed = json.loads(self.runtime._execute_tool_sync(selected, call)["content"])
        self.assertEqual(allowed["content"], "PRIVATE_MATERIAL")

    def test_old_chat_run_cannot_borrow_new_message_authorization(self):
        material = self.store.create_material("project", "项目", "PRIVATE_MATERIAL")
        chat = self.store.create("chat")
        old = self.store.claim_run(chat["id"], "chat", "active", "旧问题")
        self.store.claim_run(chat["id"], "chat", "active", f"请读取材料 {material['id']}")
        call = {"id": "1", "function": {"name": "read_material", "arguments": json.dumps({
            "material_id": material["id"],
        })}}
        result = json.loads(self.runtime._execute_tool_sync(old, call)["content"])
        self.assertEqual(result["error"], "resource_not_authorized")

    def test_demo_question_uses_explicitly_selected_claim(self):
        claim = self.store.create_claim("优化接口响应时间")
        self.store.confirm_claim(claim["id"])
        interview = self.store.create("interview", "后端开发", claim_ids=(claim["id"],))
        running = self.store.claim_run(interview["id"], "interview", "new")
        reply = run_demo(self.runtime.run(running))
        self.assertIn("优化接口响应时间", reply["message"])
