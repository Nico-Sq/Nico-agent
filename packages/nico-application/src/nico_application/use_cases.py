"""Chat, material and memory workflows shared by API and CLI."""

from collections.abc import Awaitable, Callable
from typing import Literal, Protocol

from nico_domain import (
    can_accept_chat_message,
    can_create_claim_from_material,
    is_current_chat_run,
    is_pending_memory_candidate,
    required_text,
)


class ChatSessionReader(Protocol):
    def get(self, session_id: str, kind: str) -> dict | None: ...


class ChatStorePort(ChatSessionReader, Protocol):
    def create(self, kind: str, *args, **kwargs) -> dict: ...
    def set_chat_memory(self, session_id: str, text: str) -> dict | None: ...
    def claim_run(self, session_id: str, kind: str, expected: str,
                  user_text: str | None = None) -> dict | None: ...
    def retry_chat(self, session_id: str) -> dict | None: ...
    def interrupt(self, session_id: str, kind: str) -> dict | None: ...


class MaterialStorePort(Protocol):
    def list_materials(self) -> list[dict]: ...
    def get_material(self, material_id: str) -> dict | None: ...
    def create_material(self, kind: str, title: str, content: str) -> dict: ...
    def replace_material(self, material_id: str, title: str,
                         content: str) -> dict | None: ...
    def revoke_material(self, material_id: str) -> bool: ...
    def list_claims(self) -> list[dict]: ...
    def create_claim(self, text: str, material_id: str | None = None) -> dict: ...
    def replace_claim(self, claim_id: str, text: str) -> dict | None: ...
    def revoke_claim(self, claim_id: str) -> bool: ...
    def confirm_claim(self, claim_id: str) -> dict | None: ...


class MemoryStorePort(ChatSessionReader, Protocol):
    def search_memories(self, query: str = "") -> list[dict]: ...
    def add_memory(self, content: str) -> dict: ...
    def delete_memory(self, memory_id: str) -> bool: ...
    def list_memory_candidates(self, session_id: str | None = None,
                               status: str | None = None) -> list[dict]: ...
    def memory_candidate(self, candidate_id: str) -> dict | None: ...
    def create_memory_candidate(self, session_id: str, content: str,
                                source_message_ids: list[int]) -> dict: ...
    def update_memory_candidate(self, candidate_id: str, content: str) -> dict | None: ...
    def confirm_memory_candidate(self, candidate_id: str) -> dict | None: ...
    def delete_memory_candidate(self, candidate_id: str) -> bool: ...


class ModelPort(Protocol):
    def configuration_error(self) -> str | None: ...


class ModelCatalogPort(Protocol):
    def get(self, model_id: str) -> ModelPort: ...


class UseCaseError(ValueError):
    def __init__(self, kind: Literal["not_found", "conflict", "invalid"], message: str):
        super().__init__(message)
        self.kind = kind


class ChatUseCases:
    def __init__(self, store: ChatStorePort, model_catalog: ModelCatalogPort,
                 run: Callable[[dict], Awaitable[dict]],
                 cancel_runs: Callable[[str], object]):
        self.store = store
        self.model_catalog = model_catalog
        self.run = run
        self.cancel_runs = cancel_runs

    def create(self, model_id: str = "default") -> dict:
        model = self.model_catalog.get(model_id)
        if model.configuration_error():
            raise ValueError(model.configuration_error())
        return self.store.create("chat", model_id=model_id)

    def require_session(self, session_id: str) -> dict:
        session = self.store.get(session_id, "chat")
        if session is None:
            raise UseCaseError("not_found", "聊天会话不存在")
        return session

    def set_memory(self, session_id: str, text: str) -> str:
        text = text.strip()
        if self.store.set_chat_memory(session_id, text) is None:
            raise UseCaseError("not_found", "聊天会话不存在")
        self.cancel_runs(session_id)
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
        self.cancel_runs(session_id)
        return session

    async def send_message(self, session_id: str, text: str) -> dict:
        return await self.run(self._claim_message(session_id, text))

    def claim_message(self, session_id: str, text: str) -> dict:
        return self._claim_message(session_id, text)

    async def run_claimed_message(self, session_id: str, version: int) -> dict:
        session = self.require_session(session_id)
        if not is_current_chat_run(session["status"], session["version"], version):
            return {"interrupted": True, "session": session}
        return await self.run(session)

    async def retry_message(self, session_id: str) -> dict:
        self.require_session(session_id)
        session = self.store.retry_chat(session_id)
        if session is None:
            raise UseCaseError("conflict", "当前没有待重试的消息")
        self.cancel_runs(session_id)
        return await self.run(session)

    def interrupt(self, session_id: str) -> dict:
        self.require_session(session_id)
        session = self.store.interrupt(session_id, "chat")
        if session is None:
            raise UseCaseError("conflict", "当前没有可中断的运行")
        self.cancel_runs(session_id)
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
