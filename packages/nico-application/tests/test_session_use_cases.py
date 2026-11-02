import unittest

from nico_application import SessionUseCases, UseCaseError


class FakeStore:
    def __init__(self):
        self.sessions = {
            "chat-1": {"id": "chat-1", "kind": "chat", "title": "", "chat_memory": "备注"},
            "interview-1": {
                "id": "interview-1", "kind": "interview", "title": "面试", "goal": "后端",
                "status": "finished",
            },
        }

    def get(self, session_id, kind):
        session = self.sessions.get(session_id)
        return session if session and session["kind"] == kind else None

    def list_sessions(self, kind, query=""):
        return [session for session in self.sessions.values() if session["kind"] == kind]

    def session_snapshot(self, session_id, kind):
        session = self.get(session_id, kind)
        if session is None:
            return None
        result = {
            "session": session,
            "messages": self.messages(session_id, kind),
            "events": self.events(session_id, kind),
        }
        if kind == "interview":
            result["resources"] = self.interview_resources(session_id)
            result["practice_source"] = self.practice_source(session_id)
        return result

    def messages(self, session_id, kind, limit=None):
        return [{"role": "user", "content": "你好"}]

    def events(self, session_id, kind):
        return [{"kind": "created"}]

    def set_session_title(self, session_id, kind, title):
        session = self.get(session_id, kind)
        if session is None:
            return None
        session["title"] = title
        return session

    def interview_resources(self, session_id, include_content=False):
        return {"materials": [], "claims": []}

    def practice_source(self, session_id):
        return None

    def interview_report(self, session_id):
        return {"practice_source": None, "items": [], "summary": None}


class SessionUseCaseTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.use_cases = SessionUseCases(self.store)

    def test_detail_assembles_interview_resources_and_practice_source(self):
        result = self.use_cases.detail("interview-1", "interview")
        self.assertEqual(result["session"]["id"], "interview-1")
        self.assertEqual(result["messages"][0]["content"], "你好")
        self.assertEqual(result["events"], [{"kind": "created"}])
        self.assertEqual(result["resources"], {"materials": [], "claims": []})
        self.assertIsNone(result["practice_source"])

    def test_title_normalization_stays_in_application_layer(self):
        session = self.use_cases.set_title("chat-1", "chat", "  后端\n开发  ")
        self.assertEqual(session["title"], "后端 开发")

    def test_missing_session_uses_not_found_error(self):
        with self.assertRaises(UseCaseError) as raised:
            self.use_cases.detail("missing", "chat")
        self.assertEqual(raised.exception.kind, "not_found")
        self.assertEqual(str(raised.exception), "会话不存在")

    def test_chat_export_uses_shared_markdown_renderer(self):
        markdown = self.use_cases.export_markdown("chat-1", "chat")
        self.assertIn("# 聊天记录", markdown)
        self.assertIn("## 固定备注", markdown)
        self.assertIn("你好", markdown)


if __name__ == "__main__":
    unittest.main()
