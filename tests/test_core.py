import json
import tempfile
import unittest
from pathlib import Path

from career_agent.provider import Provider
from career_agent.runtime import MAX_CONTEXT_CHARS, AgentRuntime
from career_agent.store import Store


def run_demo(coroutine):
    """Run a demo coroutine, which has no external I/O or event-loop dependency."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("演示模式意外等待了外部 I/O")


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

    def test_interview_context_excludes_chat_history(self):
        chat = self.store.create("chat")
        chat_run = self.store.claim_run(chat["id"], "chat", "active", "CHAT_SECRET_123")
        self.store.finish_run(chat["id"], chat_run["version"], "私密聊天回复", "active", 0)
        interview = self.store.create("interview", "后端工程师")
        interview_run = self.store.claim_run(interview["id"], "interview", "new")
        context = json.dumps(self.runtime.build_context(interview_run), ensure_ascii=False)
        self.assertNotIn("CHAT_SECRET_123", context)
        self.assertNotIn("私密聊天回复", context)
        self.assertIn("后端工程师", context)

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

    def test_interrupted_run_cannot_commit_late_reply(self):
        interview = self.store.create("interview", "开发岗位")
        running = self.store.claim_run(interview["id"], "interview", "new")
        self.store.interrupt(interview["id"], "interview")
        result = run_demo(self.runtime.run(running))
        self.assertTrue(result["interrupted"])
        self.assertEqual(self.store.get(interview["id"], "interview")["status"], "paused")
        self.assertEqual(self.store.messages(interview["id"], "interview"), [])
        self.assertEqual(self.store.interview_report(interview["id"])["items"], [])

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

    def test_demo_question_uses_explicitly_selected_claim(self):
        claim = self.store.create_claim("优化接口响应时间")
        self.store.confirm_claim(claim["id"])
        interview = self.store.create("interview", "后端开发", claim_ids=(claim["id"],))
        running = self.store.claim_run(interview["id"], "interview", "new")
        reply = run_demo(self.runtime.run(running))
        self.assertIn("优化接口响应时间", reply["message"])
