"""Interview workflow orchestration and result persistence."""

from collections.abc import Awaitable, Callable

from nico_contracts.events import RunEventSink
from nico_domain import (
    can_answer_interview,
    can_cancel_interview,
    can_pause_interview,
    can_practice_interview,
    can_resume_interview,
    can_skip_interview,
    can_start_interview,
    is_current_interview_run,
    parse_interview_result,
    required_text,
)

from .contracts import InterviewStorePort, ModelCatalogPort, UseCaseError


InterviewResultHandler = Callable[[dict, str], dict]
InterviewRunner = Callable[..., Awaitable[dict]]


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

    async def _run(self, session: dict, on_event: RunEventSink | None = None) -> dict:
        result_handler = lambda current, raw: commit_interview_result(self.store, current, raw)
        if on_event is None:
            return await self.run(session, result_handler)
        return await self.run(session, result_handler, on_event)

    def claim_start(self, session_id: str) -> dict:
        session = self.require_session(session_id)
        if not can_start_interview(session["status"]):
            raise UseCaseError("conflict", "面试已开始")
        running = self.store.claim_run(session_id, "interview", "new")
        if running is None:
            raise UseCaseError("conflict", "面试已开始")
        return running

    async def start(self, session_id: str, on_event: RunEventSink | None = None) -> dict:
        return await self._run(self.claim_start(session_id), on_event)

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

    async def answer(self, session_id: str, text: str,
                     on_event: RunEventSink | None = None) -> dict:
        return await self._run(self.claim_answer(session_id, text), on_event)

    async def answer_current(self, session_id: str, text: str,
                             on_event: RunEventSink | None = None) -> dict:
        session = self.require_session(session_id)
        try:
            text = required_text(text, "回答不能为空")
        except ValueError as exc:
            raise UseCaseError("invalid", str(exc)) from exc
        if len(text) > 12000:
            raise UseCaseError("invalid", "回答不能超过 12000 字")
        if session["question_count"] < 1:
            raise UseCaseError("conflict", "面试尚未生成题目")
        return await self._run(self.claim_answer(session_id, text, session["question_count"]), on_event)

    async def run_claimed_answer(self, session_id: str, version: int,
                                 on_event: RunEventSink | None = None) -> dict:
        session = self.require_session(session_id)
        if not is_current_interview_run(session["status"], session["version"], version):
            return {"interrupted": True, "session": session}
        return await self._run(session, on_event)

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

    async def skip(self, session_id: str, question_number: int,
                   on_event: RunEventSink | None = None) -> dict:
        return await self._run(self.claim_skip(session_id, question_number), on_event)

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

    async def resume(self, session_id: str, on_event: RunEventSink | None = None) -> dict:
        session = self.require_session(session_id)
        if not can_resume_interview(session["status"]):
            raise UseCaseError("conflict", "当前不能恢复")
        resumed = self.store.resume(session_id)
        if resumed is None:
            raise UseCaseError("conflict", "当前不能恢复")
        resumed_session, needs_run = resumed
        return await self._run(resumed_session, on_event) if needs_run else {"session": resumed_session}

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
