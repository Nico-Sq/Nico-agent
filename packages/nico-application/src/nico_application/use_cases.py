"""Chat, material and memory workflows shared by API and CLI."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import Literal, Protocol

from nico_domain import (
    can_accept_chat_message,
    can_answer_interview,
    can_cancel_interview,
    can_create_claim_from_material,
    can_pause_interview,
    can_practice_interview,
    can_resume_interview,
    can_skip_interview,
    can_start_interview,
    is_current_interview_run,
    is_current_chat_run,
    is_pending_memory_candidate,
    parse_interview_result,
    required_text,
)


class RepositoryPort(Protocol):
    def get(self, session_id: str, kind: str) -> dict | None: ...


class UnitOfWorkPort(Protocol):
    def transaction(self) -> AbstractContextManager[object]: ...


class AuthPort(Protocol):
    def register(self, username: str, password: str) -> str | None: ...
    def login(self, username: str, password: str) -> str | None: ...
    def issue_token(self, user_id: str) -> str: ...
    def resolve_token(self, token: str | None) -> str | None: ...
    def revoke_token(self, token: str | None) -> None: ...
    def username(self, user_id: str) -> str | None: ...


class ChannelLedgerPort(Protocol):
    def claim_inbound(self, sender_id: str, conversation_id: str, message_id: str) -> bool: ...
    def store_reply(self, message_id: str, response: str, delivery_id: str) -> None: ...
    def mark_delivered(self, delivery_id: str) -> None: ...


class ChatSessionReader(RepositoryPort, Protocol):
    pass


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


class InterviewStorePort(ChatSessionReader, Protocol):
    def create(self, kind: str, goal: str, material_ids: tuple[str, ...],
               claim_ids: tuple[str, ...], max_questions: int, model_id: str) -> dict: ...
    def list_sessions(self, kind: str, query: str = "") -> list[dict]: ...
    def interview_report(self, session_id: str) -> dict | None: ...
    def messages(self, session_id: str, kind: str, limit: int | None = None) -> list[dict]: ...
    def claim_run(self, session_id: str, kind: str, expected: str,
                  user_text: str | None = None,
                  expected_question: int | None = None) -> dict | None: ...
    def skip_interview_question(self, session_id: str, question_number: int) -> dict | None: ...
    def finish_run(self, session_id: str, version: int, content: str, status: str,
                   question_count: int, question: str | None = None,
                   feedback: str | None = None, evidence: tuple[str, ...] = ()) -> bool: ...
    def interrupt(self, session_id: str, kind: str) -> dict | None: ...
    def cancel_interview(self, session_id: str) -> dict | None: ...
    def resume(self, session_id: str) -> tuple[dict, bool] | None: ...
    def create_practice(self, source_session_id: str, question_number: int) -> dict: ...


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


InterviewResultHandler = Callable[[dict, str], dict]
InterviewRunner = Callable[[dict, InterviewResultHandler], Awaitable[dict]]


def commit_interview_result(
    store: InterviewStorePort,
    session: dict,
    raw: str,
) -> dict:
    report = store.interview_report(session["id"])
    if report is None:
        return {"interrupted": True, "session": store.get(session["id"], "interview")}
    items = report["items"]
    skipped = bool(items and items[-1]["skipped"])
    latest = store.messages(session["id"], "interview", limit=1)
    latest_answer = latest[0]["content"] if latest and latest[0]["role"] == "user" else ""
    result = parse_interview_result(session, raw, latest_answer, skipped)
    committed = store.finish_run(
        session["id"], session["version"], result.content, result.status,
        result.question_count, question=result.question, feedback=result.feedback,
        evidence=result.evidence,
    )
    if not committed:
        return {"interrupted": True, "session": store.get(session["id"], "interview")}
    return {"message": result.content, "session": store.get(session["id"], "interview")}


class InterviewUseCases:
    def __init__(self, store: InterviewStorePort, model_catalog: ModelCatalogPort,
                 run: InterviewRunner, cancel_runs: Callable[[str], object]):
        self.store = store
        self.model_catalog = model_catalog
        self.run = run
        self.cancel_runs = cancel_runs

    def create(self, goal: str, material_ids: tuple[str, ...] = (),
               claim_ids: tuple[str, ...] = (), max_questions: int = 3,
               model_id: str = "default") -> dict:
        goal = required_text(goal, "岗位目标不能为空")
        if len(goal) > 500 or len(material_ids) > 5 or len(claim_ids) > 10:
            raise ValueError("岗位目标或关联资源数量超出限制")
        model = self.model_catalog.get(model_id)
        if model.configuration_error():
            raise ValueError(model.configuration_error())
        return self.store.create(
            "interview", goal, tuple(material_ids), tuple(claim_ids), max_questions, model_id,
        )

    def list(self, query: str = "") -> list[dict]:
        return self.store.list_sessions("interview", query)

    def require_session(self, session_id: str) -> dict:
        session = self.store.get(session_id, "interview")
        if session is None:
            raise UseCaseError("not_found", "面试会话不存在")
        return session

    def report(self, session_id: str) -> dict:
        report = self.store.interview_report(session_id)
        if report is None:
            raise UseCaseError("not_found", "面试不存在")
        return report

    async def _run(self, session: dict) -> dict:
        return await self.run(
            session, lambda current, raw: commit_interview_result(self.store, current, raw),
        )

    def claim_start(self, session_id: str) -> dict:
        session = self.require_session(session_id)
        if not can_start_interview(session["status"]):
            raise UseCaseError("conflict", "面试已开始")
        running = self.store.claim_run(session_id, "interview", "new")
        if running is None:
            raise UseCaseError("conflict", "面试已开始")
        return running

    async def start(self, session_id: str) -> dict:
        return await self._run(self.claim_start(session_id))

    def claim_answer(self, session_id: str, text: str,
                     expected_question: int | None = None) -> dict:
        session = self.require_session(session_id)
        try:
            text = required_text(text, "回答不能为空")
        except ValueError as exc:
            raise UseCaseError("invalid", str(exc)) from exc
        if len(text) > 12000:
            raise UseCaseError("invalid", "回答不能超过 12000 字")
        if not can_answer_interview(session["status"], session["question_count"], expected_question):
            message = "当前题目已变化，无法提交回答" if expected_question is not None else "当前不能提交回答"
            raise UseCaseError("conflict", message)
        running = self.store.claim_run(
            session_id, "interview", "awaiting_answer", text,
            expected_question=expected_question,
        )
        if running is None:
            message = "当前题目已变化，无法提交回答" if expected_question is not None else "当前不能提交回答"
            raise UseCaseError("conflict", message)
        self.cancel_runs(session_id)
        return running

    async def answer(self, session_id: str, text: str) -> dict:
        return await self._run(self.claim_answer(session_id, text))

    async def answer_current(self, session_id: str, text: str) -> dict:
        session = self.require_session(session_id)
        try:
            text = required_text(text, "回答不能为空")
        except ValueError as exc:
            raise UseCaseError("invalid", str(exc)) from exc
        if len(text) > 12000:
            raise UseCaseError("invalid", "回答不能超过 12000 字")
        if session["question_count"] < 1:
            raise UseCaseError("conflict", "面试尚未生成题目")
        return await self._run(self.claim_answer(session_id, text, session["question_count"]))

    async def run_claimed_answer(self, session_id: str, version: int) -> dict:
        session = self.require_session(session_id)
        if not is_current_interview_run(session["status"], session["version"], version):
            return {"interrupted": True, "session": session}
        return await self._run(session)

    def claim_skip(self, session_id: str, question_number: int) -> dict:
        session = self.require_session(session_id)
        if question_number < 1:
            raise UseCaseError("invalid", "题号必须大于 0")
        if not can_skip_interview(session["status"], session["question_count"], question_number):
            raise UseCaseError("conflict", "当前题目已变化，无法跳过")
        running = self.store.skip_interview_question(session_id, question_number)
        if running is None:
            raise UseCaseError("conflict", "当前题目已变化，无法跳过")
        self.cancel_runs(session_id)
        return running

    async def skip(self, session_id: str, question_number: int) -> dict:
        return await self._run(self.claim_skip(session_id, question_number))

    def pause(self, session_id: str) -> dict:
        session = self.require_session(session_id)
        if not can_pause_interview(session["status"]):
            raise UseCaseError("conflict", "当前不能暂停")
        paused = self.store.interrupt(session_id, "interview")
        if paused is None:
            raise UseCaseError("conflict", "当前不能暂停")
        self.cancel_runs(session_id)
        return paused

    def cancel(self, session_id: str) -> dict:
        session = self.require_session(session_id)
        if not can_cancel_interview(session["status"]):
            raise UseCaseError("conflict", "当前不能结束面试")
        cancelled = self.store.cancel_interview(session_id)
        if cancelled is None:
            raise UseCaseError("conflict", "当前不能结束面试")
        self.cancel_runs(session_id)
        return cancelled

    async def resume(self, session_id: str) -> dict:
        session = self.require_session(session_id)
        if not can_resume_interview(session["status"]):
            raise UseCaseError("conflict", "当前不能恢复")
        resumed = self.store.resume(session_id)
        if resumed is None:
            raise UseCaseError("conflict", "当前不能恢复")
        resumed_session, needs_run = resumed
        return await self._run(resumed_session) if needs_run else {"session": resumed_session}

    def create_practice(self, session_id: str, question_number: int) -> dict:
        session = self.require_session(session_id)
        if question_number < 1:
            raise UseCaseError("invalid", "题号必须大于 0")
        report = self.report(session_id)
        item = next((item for item in report["items"] if item["number"] == question_number), None)
        has_feedback = item is not None and item["feedback"] is not None
        if not can_practice_interview(session["status"], question_number, has_feedback):
            raise UseCaseError("conflict", "只能复练已完成面试中有反馈的题目")
        return self.store.create_practice(session_id, question_number)

    async def practice(self, session_id: str, question_number: int) -> dict:
        practice = self.create_practice(session_id, question_number)
        return await self.start(practice["id"])
