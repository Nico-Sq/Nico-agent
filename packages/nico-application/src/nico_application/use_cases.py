"""Chat, material and memory workflows shared by API and CLI."""

from collections.abc import Awaitable, Callable

from nico_contracts.events import RunEventSink
from nico_domain import (
    can_accept_chat_message,
    can_create_claim_from_material,
    is_current_chat_run,
    is_pending_memory_candidate,
    required_text,
)

from .contracts import (
    AuthPort,
    ChatSessionReader,
    ChatStorePort,
    InterviewStorePort,
    MaterialStorePort,
    MemoryStorePort,
    ModelCatalogPort,
    ModelPort,
    RepositoryPort,
    SessionDeletionStorePort,
    UnitOfWorkPort,
    UseCaseError,
)
from .interview_use_cases import (
    InterviewResultHandler,
    InterviewRunner,
    InterviewUseCases,
    commit_interview_result,
)


def commit_chat_result(store: ChatStorePort, session: dict, content: str) -> dict:
    committed = store.finish_run(session["id"], session["version"], content, "active", 0)
    if not committed:
        return {"interrupted": True, "session": store.get(session["id"], "chat")}
    return {"message": content, "session": store.get(session["id"], "chat")}


def delete_session_and_cancel_runs(store: SessionDeletionStorePort, session_id: str, kind: str,
                                   cancel_runs: Callable[[str], object]) -> None:
    deleted_ids = store.delete_session_tree(session_id, kind)
    if not deleted_ids:
        raise UseCaseError("not_found", "会话不存在")
    for deleted_id in deleted_ids:
        cancel_runs(deleted_id)


class ChatUseCases:
    def __init__(self, store: ChatStorePort, model_catalog: ModelCatalogPort,
                 run: Callable[..., Awaitable[dict]],
                 cancel_runs: Callable[[str, int], object]):
        self.store = store
        self.model_catalog = model_catalog
        self.run = run
        self.cancel_runs = cancel_runs

    def create(self, model_id: str = "default") -> dict:
        self.validate_model(model_id)
        return self.store.create("chat", model_id=model_id)

    def validate_model(self, model_id: str) -> None:
        model = self.model_catalog.get(model_id)
        error = model.configuration_error()
        if error:
            raise ValueError(error)

    def require_session(self, session_id: str) -> dict:
        session = self.store.get(session_id, "chat")
        if session is None:
            raise UseCaseError("not_found", "聊天会话不存在")
        return session

    def set_memory(self, session_id: str, text: str) -> str:
        text = text.strip()
        session = self.store.set_chat_memory(session_id, text)
        if session is None:
            raise UseCaseError("not_found", "聊天会话不存在")
        self.cancel_runs(session_id, session["version"])
        return text

    def _claim_message(self, session_id: str, text: str) -> dict:
        session = self.require_session(session_id)
        if not can_accept_chat_message(session["status"]):
            raise UseCaseError("conflict", "当前不能发送消息")
        try:
            text = required_text(text)
        except ValueError as exc:
            raise UseCaseError("invalid", str(exc)) from exc
        session = self.store.claim_run(
            session_id, "chat", "active", text,
        )
        if session is None:
            raise UseCaseError("conflict", "当前不能发送消息")
        self.cancel_runs(session_id, session["version"])
        return session

    async def send_message(self, session_id: str, text: str,
                           on_event: RunEventSink | None = None) -> dict:
        return await self._run(self._claim_message(session_id, text), on_event)

    async def _run(self, session: dict, on_event: RunEventSink | None = None) -> dict:
        if on_event is None:
            return await self.run(session)
        return await self.run(session, on_event)

    def claim_message(self, session_id: str, text: str) -> dict:
        return self._claim_message(session_id, text)

    async def run_claimed_message(self, session_id: str, version: int,
                                  on_event: RunEventSink | None = None) -> dict:
        session = self.require_session(session_id)
        if not is_current_chat_run(session["status"], session["version"], version):
            return {"interrupted": True, "session": session}
        return await self._run(session, on_event)

    async def retry_message(self, session_id: str,
                            on_event: RunEventSink | None = None) -> dict:
        self.require_session(session_id)
        session = self.store.retry_chat(session_id)
        if session is None:
            raise UseCaseError("conflict", "当前没有待重试的消息")
        self.cancel_runs(session_id, session["version"])
        return await self._run(session, on_event)

    def interrupt(self, session_id: str) -> dict:
        self.require_session(session_id)
        session = self.store.interrupt(session_id, "chat")
        if session is None:
            raise UseCaseError("conflict", "当前没有可中断的运行")
        self.cancel_runs(session_id, session["version"])
        return session


class MaterialUseCases:
    def __init__(self, store: MaterialStorePort):
        self.store = store

    def list_materials(self) -> list[dict]:
        return self.store.list_materials()

    def create_material(self, kind: str, title: str, content: str) -> dict:
        return self.store.create_material(
            kind, required_text(title), required_text(content),
        )

    def edit_material(self, material_id: str, title: str, content: str) -> dict | None:
        return self.store.replace_material(
            material_id, required_text(title), required_text(content),
        )

    def delete_material(self, material_id: str) -> bool:
        return self.store.revoke_material(material_id)

    def list_claims(self) -> list[dict]:
        return self.store.list_claims()

    def create_claim(self, text: str, material_id: str | None = None) -> dict:
        if material_id is not None:
            material = self.store.get_material(material_id)
            if material is None or not can_create_claim_from_material(
                material["kind"], material["revoked_at"],
            ):
                raise ValueError("经历来源必须是简历或项目材料")
        return self.store.create_claim(required_text(text), material_id)

    def edit_claim(self, claim_id: str, text: str) -> dict | None:
        return self.store.replace_claim(claim_id, required_text(text))

    def delete_claim(self, claim_id: str) -> bool:
        return self.store.revoke_claim(claim_id)

    def confirm_claim(self, claim_id: str) -> dict | None:
        return self.store.confirm_claim(claim_id)


class MemoryUseCases:
    def __init__(self, store: MemoryStorePort):
        self.store = store

    def search(self, query: str = "") -> list[dict]:
        return self.store.search_memories(query)

    def add(self, content: str) -> dict:
        return self.store.add_memory(required_text(content))

    def delete(self, memory_id: str) -> None:
        if not self.store.delete_memory(memory_id):
            raise UseCaseError("not_found", "记忆不存在")

    def list_candidates(self, session_id: str | None = None,
                        status: str | None = None) -> list[dict]:
        return self.store.list_memory_candidates(session_id, status)

    def get_candidate(self, candidate_id: str) -> dict:
        candidate = self.store.memory_candidate(candidate_id)
        if candidate is None:
            raise UseCaseError("not_found", "记忆候选不存在")
        return candidate

    def create_candidate(self, session_id: str, content: str,
                         source_message_ids: list[int]) -> dict:
        return self.store.create_memory_candidate(
            session_id, content, source_message_ids,
        )

    def edit_candidate(self, candidate_id: str, content: str) -> dict | None:
        candidate = self.get_candidate(candidate_id)
        if not is_pending_memory_candidate(candidate["status"]):
            raise UseCaseError("conflict", "已确认的记忆候选不能修改")
        return self.store.update_memory_candidate(candidate_id, content)

    def confirm_candidate(self, candidate_id: str) -> dict:
        candidate = self.store.confirm_memory_candidate(candidate_id)
        if candidate is None:
            raise UseCaseError("not_found", "记忆候选不存在")
        return candidate

    def delete_candidate(self, candidate_id: str) -> None:
        candidate = self.get_candidate(candidate_id)
        if not is_pending_memory_candidate(candidate["status"]):
            raise UseCaseError("conflict", "已确认的记忆候选不能删除")
        self.store.delete_memory_candidate(candidate_id)
