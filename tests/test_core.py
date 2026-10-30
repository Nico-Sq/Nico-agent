import json
import tempfile
import unittest
from pathlib import Path

from career_agent.provider import Provider
from career_agent.runtime import AgentRuntime
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
        selected = self.store.claim_run(chat["id"], "chat", "active", f"请解释面试 {interview['id']} 的反馈")
        allowed = self.runtime._execute_tool_sync(selected, call)
        self.assertEqual(json.loads(allowed["content"])["goal"], "开发岗位")

    def test_interrupted_run_cannot_commit_late_reply(self):
        interview = self.store.create("interview", "开发岗位")
        running = self.store.claim_run(interview["id"], "interview", "new")
        self.store.interrupt(interview["id"], "interview")
        result = run_demo(self.runtime.run(running))
        self.assertTrue(result["interrupted"])
        self.assertEqual(self.store.get(interview["id"], "interview")["status"], "paused")
        self.assertEqual(self.store.messages(interview["id"], "interview"), [])

    def test_three_question_flow(self):
        interview = self.store.create("interview", "开发岗位")
        running = self.store.claim_run(interview["id"], "interview", "new")
        run_demo(self.runtime.run(running))
        for number in range(1, 4):
            current = self.store.claim_run(interview["id"], "interview", "awaiting_answer", f"第 {number} 题回答")
            run_demo(self.runtime.run(current))
        final = self.store.get(interview["id"], "interview")
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["question_count"], 3)
        self.assertEqual(len(self.store.messages(interview["id"], "interview")), 7)

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

    def test_demo_question_uses_explicitly_selected_claim(self):
        claim = self.store.create_claim("优化接口响应时间")
        self.store.confirm_claim(claim["id"])
        interview = self.store.create("interview", "后端开发", claim_ids=(claim["id"],))
        running = self.store.claim_run(interview["id"], "interview", "new")
        reply = run_demo(self.runtime.run(running))
        self.assertIn("优化接口响应时间", reply["message"])
