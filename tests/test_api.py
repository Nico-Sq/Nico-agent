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
