import asyncio
import json
import unittest

from nico_application import (
    ChatUseCases,
    InterviewUseCases,
    MaterialUseCases,
    MemoryUseCases,
    UseCaseError,
    commit_interview_result,
)


class FakeStore:
    def __init__(self):
        self.session = {"id": "chat-1", "kind": "chat", "status": "active", "version": 1}
        self.candidate = {"id": "candidate-1", "status": "pending"}
        self.calls = []

    def create(self, kind, model_id="default"):
        self.calls.append(("create", kind, model_id))
        return {"id": "chat-1", "kind": kind, "model_id": model_id}

    def get(self, session_id, kind):
        return self.session if session_id == self.session["id"] and kind == "chat" else None

    def set_chat_memory(self, session_id, text):
        self.calls.append(("set_chat_memory", session_id, text))
        return self.session if session_id == self.session["id"] else None

    def claim_run(self, session_id, kind, expected, text):
        self.calls.append(("claim_run", session_id, text))
        return {**self.session, "status": "running", "version": 2}

    def retry_chat(self, session_id):
        self.calls.append(("retry_chat", session_id))
        return {**self.session, "status": "running", "version": 2}

    def interrupt(self, session_id, kind):
        self.calls.append(("interrupt", session_id, kind))
        return {**self.session, "status": "active"}

    def list_materials(self):
        return []

    def create_material(self, kind, title, content):
        self.calls.append(("create_material", kind, title, content))
        return {"kind": kind, "title": title, "content": content}

    def get_material(self, material_id):
        return {"id": material_id, "kind": "resume", "revoked_at": None}

    def create_claim(self, text, material_id=None):
        self.calls.append(("create_claim", text, material_id))
        return {"text": text, "material_id": material_id}

    def search_memories(self, query=""):
        return [{"content": query}] if query else []

    def add_memory(self, content):
        return {"content": content, "created": True}

    def delete_memory(self, memory_id):
        return memory_id == "memory-1"

    def list_memory_candidates(self, session_id=None, status=None):
        return [self.candidate]

    def memory_candidate(self, candidate_id):
        return self.candidate if candidate_id == self.candidate["id"] else None

    def update_memory_candidate(self, candidate_id, content):
        self.calls.append(("update_candidate", candidate_id, content))
        return {**self.candidate, "content": content}

    def confirm_memory_candidate(self, candidate_id):
        self.candidate = {**self.candidate, "status": "confirmed"}
        return self.candidate

    def delete_memory_candidate(self, candidate_id):
        self.calls.append(("delete_candidate", candidate_id))
        return True

    def create_memory_candidate(self, session_id, content, source_ids):
        if session_id != self.session["id"]:
            raise ValueError("候选记忆只能来自聊天会话")
        return {"session_id": session_id, "content": content, "sources": source_ids}


class FakeModel:
    def configuration_error(self):
        return None


class FakeCatalog:
    def get(self, model_id):
        return FakeModel()


class InterviewFakeStore:
    def __init__(self, status="new", question_count=0):
        self.session = {
            "id": "interview-1", "kind": "interview", "status": status,
            "version": 1, "question_count": question_count, "max_questions": 1,
        }
        self.items = []
        self.latest_message = None
        self.finish_calls = []
        self.commit = True

    def get(self, session_id, kind):
        if session_id != self.session["id"] or kind != "interview":
            return None
        return dict(self.session)

    def interview_report(self, session_id):
        if session_id != self.session["id"]:
            return None
        return {"items": list(self.items)}

    def messages(self, session_id, kind, limit=None):
        if self.latest_message is None:
            return []
        return [dict(self.latest_message)]

    def claim_run(self, session_id, kind, expected, user_text=None, expected_question=None):
        if self.session["status"] != expected:
            return None
        self.session["status"] = "running"
        self.session["version"] += 1
        return dict(self.session)

    def finish_run(self, session_id, version, content, status, question_count,
                   question=None, feedback=None, evidence=()):
        self.finish_calls.append((session_id, version, content, status, question_count,
                                  question, feedback, evidence))
        if not self.commit:
            return False
        self.session.update(status=status, question_count=question_count)
        if question is not None:
            self.items.append({"number": question_count, "question": question,
                               "answer": None, "feedback": feedback,
                               "skipped": False, "evidence": list(evidence)})
        return True


class InterviewResultTests(unittest.TestCase):
    def setUp(self):
        self.store = InterviewFakeStore("running")

    def test_result_uses_one_version_protected_commit(self):
        session = dict(self.store.session)
        result = commit_interview_result(
            self.store, session,
            json.dumps({"feedback": "", "question": "请介绍项目", "finished": False,
                        "evidence": []}),
        )
        self.assertEqual(result["message"], "第 1 题：请介绍项目")
        self.assertEqual(len(self.store.finish_calls), 1)
        self.assertEqual(self.store.finish_calls[0][1], session["version"])

    def test_invalid_result_does_not_commit(self):
        session = {**self.store.session, "question_count": 1}
        self.store.items.append({"number": 1, "question": "题目", "answer": "回答",
                                 "feedback": None, "skipped": False, "evidence": []})
        self.store.latest_message = {"role": "user", "content": "原始回答"}
        with self.assertRaisesRegex(ValueError, "原文短句"):
            commit_interview_result(
                self.store, session,
                json.dumps({"feedback": "评价", "question": "", "finished": True,
                            "evidence": ["虚构证据"], "summary": "总结"}),
            )
        self.assertEqual(self.store.finish_calls, [])

    def test_stale_commit_returns_interrupted_after_one_commit_attempt(self):
        self.store.commit = False
        result = commit_interview_result(
            self.store, dict(self.store.session),
            json.dumps({"feedback": "", "question": "请介绍项目", "finished": False,
                        "evidence": []}),
        )
        self.assertTrue(result["interrupted"])
        self.assertEqual(len(self.store.finish_calls), 1)


class InterviewUseCaseTests(unittest.TestCase):
    def test_fake_runner_uses_application_result_handler(self):
        store = InterviewFakeStore()
        runs = []

        async def run(session, result_handler):
            runs.append(session["version"])
            return result_handler(session, json.dumps({
                "feedback": "", "question": "请介绍项目", "finished": False,
                "evidence": [],
            }))

        interviews = InterviewUseCases(store, FakeCatalog(), run, lambda _: None)
        result = asyncio.run(interviews.start("interview-1"))
        self.assertEqual(result["message"], "第 1 题：请介绍项目")
        self.assertEqual(runs, [2])
        self.assertEqual(len(store.finish_calls), 1)

    def test_illegal_skip_does_not_write(self):
        store = InterviewFakeStore()

        async def run(session, result_handler):
            return result_handler(session, "{}")

        interviews = InterviewUseCases(store, FakeCatalog(), run, lambda _: None)
        with self.assertRaises(UseCaseError):
            asyncio.run(interviews.skip("interview-1", 1))
        self.assertEqual(store.finish_calls, [])
        self.assertEqual(store.session["status"], "new")


class UseCaseTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()

    def test_chat_send_claims_input_cancels_old_run_and_calls_runner(self):
        effects = []

        async def run(session):
            effects.append(("run", session["version"]))
            return {"message": "reply"}

        chat = ChatUseCases(
            self.store,
            FakeCatalog(),
            run,
            lambda session_id: effects.append(("cancel", session_id)),
        )
        result = asyncio.run(chat.send_message("chat-1", "  hello  "))
        self.assertEqual(result, {"message": "reply"})
        self.assertEqual(self.store.calls[0], ("claim_run", "chat-1", "hello"))
        self.assertEqual(effects, [("cancel", "chat-1"), ("run", 2)])

    def test_chat_rejects_blank_input_before_claiming(self):
        async def run(session):
            return {"message": "reply"}

        chat = ChatUseCases(self.store, FakeCatalog(), run, lambda _: None)
        with self.assertRaises(UseCaseError) as raised:
            asyncio.run(chat.send_message("chat-1", "  "))
        self.assertEqual(raised.exception.kind, "invalid")
        self.assertEqual(self.store.calls, [])

    def test_memory_use_cases_preserve_confirmed_candidate(self):
        memories = MemoryUseCases(self.store)
        memories.confirm_candidate("candidate-1")
        with self.assertRaisesRegex(UseCaseError, "不能修改"):
            memories.edit_candidate("candidate-1", "changed")
        with self.assertRaisesRegex(UseCaseError, "不能删除"):
            memories.delete_candidate("candidate-1")
        self.assertNotIn(("update_candidate", "candidate-1", "changed"), self.store.calls)
        self.assertNotIn(("delete_candidate", "candidate-1"), self.store.calls)

    def test_memory_candidate_workflow_uses_storage_validation(self):
        memories = MemoryUseCases(self.store)
        with self.assertRaisesRegex(ValueError, "只能来自聊天会话"):
            memories.create_candidate("missing", "fact", [1])

    def test_material_use_case_normalizes_input_before_storage(self):
        materials = MaterialUseCases(self.store)
        created = materials.create_material("resume", " Resume ", " text ")
        claim = materials.create_claim("  experience  ", "material-1")
        self.assertEqual(created["title"], "Resume")
        self.assertEqual(created["content"], "text")
        self.assertEqual(claim["text"], "experience")


if __name__ == "__main__":
    unittest.main()
