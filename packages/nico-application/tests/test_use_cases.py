import asyncio
import unittest

from nico_application import (
    ChatUseCases,
    MaterialUseCases,
    MemoryUseCases,
    UseCaseError,
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
