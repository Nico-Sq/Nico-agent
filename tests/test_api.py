import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from career_agent import main
from career_agent.provider import Provider
from career_agent.runtime import AgentRuntime
from career_agent.store import Store


class InterviewApiTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.original_store, self.original_runtime = main.store, main.runtime
        main.store = Store(Path(self.temp.name) / "api.sqlite3")
        provider = Provider()
        provider.mode = "demo"
        main.runtime = AgentRuntime(main.store, provider)
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        main.store, main.runtime = self.original_store, self.original_runtime
        self.temp.cleanup()

    def test_material_to_report_to_practice(self):
        material = self.client.post("/api/materials", json={
            "kind": "resume", "title": "项目简历", "content": "我负责性能优化。",
        }).json()
        claim = self.client.post("/api/claims", json={
            "text": "接口延迟下降 40%", "material_id": material["id"],
        }).json()
        denied = self.client.post("/api/interview/sessions", json={
            "goal": "后端开发", "claim_ids": [claim["id"]],
        })
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(self.client.post(f"/api/claims/{claim['id']}/confirm").status_code, 200)
        interview = self.client.post("/api/interview/sessions", json={
            "goal": "后端开发", "material_ids": [material["id"]], "claim_ids": [claim["id"]],
        }).json()
        session_id = interview["id"]
        self.assertEqual(self.client.post(f"/api/interview/sessions/{session_id}/start").status_code, 200)
        for number in range(3):
            reply = self.client.post(
                f"/api/interview/sessions/{session_id}/answers", json={"text": f"回答 {number}"}
            )
            self.assertEqual(reply.status_code, 200)
        report = self.client.get(f"/api/interview/sessions/{session_id}/report").json()
        self.assertEqual(report["status"], "finished")
        self.assertEqual(len(report["items"]), 3)
        self.assertEqual(report["items"][0]["answer"], "回答 0")
        self.assertEqual(report["items"][0]["evidence"], ["回答 0"])
        practice = self.client.post(
            f"/api/interview/sessions/{session_id}/practice", json={"question_number": 2}
        ).json()
        practice_id = practice["id"]
        self.assertEqual(self.client.post(f"/api/interview/sessions/{practice_id}/start").status_code, 200)
        self.assertEqual(self.client.post(
            f"/api/interview/sessions/{practice_id}/answers", json={"text": "复练回答"}
        ).status_code, 200)
        practice_report = self.client.get(f"/api/interview/sessions/{practice_id}/report").json()
        self.assertEqual(practice_report["status"], "finished")
        self.assertEqual(len(practice_report["items"]), 1)
        self.assertEqual(len(self.client.get(
            f"/api/interview/sessions/{practice_id}"
        ).json()["resources"]["materials"]), 1)

    def test_chat_uses_confirmed_claims_and_explicit_resources(self):
        claim = self.client.post("/api/claims", json={"text": "我优化了查询性能"}).json()
        material = self.client.post("/api/materials", json={
            "kind": "project", "title": "查询项目", "content": "使用索引减少了扫描行数。",
        }).json()
        chat = self.client.post("/api/chat/sessions").json()
        chat_id = chat["id"]
        first_response = self.client.post(f"/api/chat/sessions/{chat_id}/messages", json={
            "text": "请整理我的已确认经历",
        })
        self.assertEqual(first_response.status_code, 200, first_response.text)
        first = first_response.json()
        self.assertIn("没有已确认", first["message"])
        self.client.post(f"/api/claims/{claim['id']}/confirm")
        confirmed = self.client.post(f"/api/chat/sessions/{chat_id}/messages", json={
            "text": "请整理我的已确认经历",
        }).json()
        self.assertIn("我优化了查询性能", confirmed["message"])
        named_material = self.client.post(f"/api/chat/sessions/{chat_id}/messages", json={
            "text": f"请解释材料 {material['id']}",
        }).json()
        self.assertIn("使用索引减少了扫描行数", named_material["message"])
        interview = self.client.post("/api/interview/sessions", json={"goal": "后端开发"}).json()
        interview_id = interview["id"]
        self.client.post(f"/api/interview/sessions/{interview_id}/start")
        for _ in range(3):
            self.client.post(f"/api/interview/sessions/{interview_id}/answers", json={
                "text": "PRIVATE_INTERVIEW_ANSWER",
            })
        discussed = self.client.post(f"/api/chat/sessions/{chat_id}/messages", json={
            "text": f"请解释面试 {interview_id} 的反馈",
        }).json()
        self.assertIn("面试总结", discussed["message"])
        self.assertNotIn("PRIVATE_INTERVIEW_ANSWER", discussed["message"])
        interview_view = self.client.get(f"/api/interview/sessions/{interview_id}").json()
        self.assertNotIn("请整理我的已确认经历", str(interview_view["messages"]))

    def test_edit_and_revoke_preserve_old_interview_resources(self):
        material = self.client.post("/api/materials", json={
            "kind": "project", "title": "旧项目", "content": "旧版实施细节",
        }).json()
        claim = self.client.post("/api/claims", json={
            "text": "旧版个人贡献", "material_id": material["id"],
        }).json()
        self.client.post(f"/api/claims/{claim['id']}/confirm")
        interview = self.client.post("/api/interview/sessions", json={
            "goal": "后端开发", "material_ids": [material["id"]], "claim_ids": [claim["id"]],
        }).json()
        revised = self.client.put(f"/api/materials/{material['id']}", json={
            "title": "新版项目", "content": "新版实施细节",
        }).json()
        self.assertNotEqual(revised["id"], material["id"])
        self.assertEqual([item["id"] for item in self.client.get("/api/materials").json()], [revised["id"]])
        self.assertEqual(self.client.get("/api/claims").json(), [])
        self.assertEqual(self.client.post("/api/interview/sessions", json={
            "goal": "后端开发", "material_ids": [material["id"]],
        }).status_code, 422)
        self.assertEqual(self.client.post("/api/interview/sessions", json={
            "goal": "后端开发", "claim_ids": [claim["id"]],
        }).status_code, 422)
        old_view = self.client.get(f"/api/interview/sessions/{interview['id']}").json()
        self.assertEqual(old_view["resources"]["materials"][0]["title"], "旧项目")
        self.assertEqual(old_view["resources"]["claims"][0]["text"], "旧版个人贡献")
        self.assertEqual(self.client.delete(f"/api/materials/{revised['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/materials").json(), [])

    def test_edited_claim_requires_new_confirmation(self):
        claim = self.client.post("/api/claims", json={"text": "原贡献"}).json()
        self.client.post(f"/api/claims/{claim['id']}/confirm")
        edited = self.client.put(f"/api/claims/{claim['id']}", json={"text": "修正贡献"}).json()
        self.assertEqual(edited["status"], "draft")
        self.assertEqual(self.client.post("/api/interview/sessions", json={
            "goal": "开发", "claim_ids": [edited["id"]],
        }).status_code, 422)
        self.assertEqual(self.client.post(f"/api/claims/{edited['id']}/confirm").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/claims/{edited['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/claims").json(), [])

    def test_chat_followup_supersedes_older_run_without_losing_messages(self):
        chat_id = self.client.post("/api/chat/sessions").json()["id"]
        first = self.client.post(f"/api/chat/sessions/{chat_id}/messages/claim", json={
            "text": "先讨论岗位",
        }).json()
        second = self.client.post(f"/api/chat/sessions/{chat_id}/messages/claim", json={
            "text": "补充：重点是后端开发",
        }).json()
        old = self.client.post(f"/api/chat/sessions/{chat_id}/messages/run", json={
            "version": first["version"],
        }).json()
        self.assertTrue(old["interrupted"])
        latest = self.client.post(f"/api/chat/sessions/{chat_id}/messages/run", json={
            "version": second["version"],
        }).json()
        self.assertIn("补充：重点是后端开发", latest["message"])
        messages = self.client.get(f"/api/chat/sessions/{chat_id}").json()["messages"]
        self.assertEqual([item["role"] for item in messages], ["user", "user", "assistant"])
        self.assertEqual([item["content"] for item in messages[:2]], ["先讨论岗位", "补充：重点是后端开发"])
