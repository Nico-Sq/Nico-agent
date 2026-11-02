"""HTTP routes and request models for Nico-agent."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from nico_application import (
    ChatUseCases,
    InterviewUseCases,
    MemoryUseCases,
    ToolGrantError,
    ToolGrantUseCases,
    UseCaseError,
    delete_session_and_cancel_runs,
)
from nico_application.export import render_session_markdown
from nico_contracts.errors import ModelError
from nico_contracts.events import RunEventSink

from .auth_routes import (
    Credentials,
    current_user,
    login,
    logout,
    register,
    router as auth_router,
    set_login_cookie,
)
from .material_routes import (
    ClaimEdit,
    ClaimInput,
    MaterialEdit,
    MaterialInput,
    active_material_use_cases,
    confirm_claim,
    create_claim,
    create_material,
    delete_claim,
    delete_material,
    edit_claim,
    edit_material,
    extract_material,
    list_claims,
    list_materials,
    router as material_router,
)
from .backend import active_backend, active_runtime as _active_runtime, active_store as _active_store


router = APIRouter()
logger = logging.getLogger(__name__)

# Keep the legacy ``career_agent.main`` route imports explicit without leaking
# FastAPI, Pydantic, and other implementation imports into that module.
__all__ = (
    "router",
    "active_store",
    "active_runtime",
    "active_chat_use_cases",
    "active_interview_use_cases",
    "active_material_use_cases",
    "active_memory_use_cases",
    "http_use_case_error",
    "http_interview_use_case_error",
    "run_chat_session",
    "run_interview_session",
    "wants_sse",
    "stream_response",
    "complete_chat_request",
    "complete_interview_request",
    "TextInput",
    "InterviewInput",
    "ChatCreateInput",
    "ToolGrantsInput",
    "MaterialInput",
    "ClaimInput",
    "MaterialEdit",
    "ClaimEdit",
    "ChatRunInput",
    "ChatMemoryInput",
    "LongMemoryInput",
    "MemoryCandidateInput",
    "MemoryCandidateEdit",
    "SessionTitleInput",
    "InterviewAnswerInput",
    "PracticeInput",
    "Credentials",
    "require_session",
    "config",
    "list_models",
    "list_memories",
    "add_memory",
    "delete_memory",
    "list_memory_candidates",
    "get_memory_candidate",
    "create_memory_candidate",
    "update_memory_candidate",
    "confirm_memory_candidate",
    "delete_memory_candidate",
    "list_mcp_tools",
    "list_mcp_servers",
    "list_tools",
    "get_session_tools",
    "set_session_tools",
    "backup_data",
    "set_login_cookie",
    "register",
    "login",
    "current_user",
    "logout",
    "list_materials",
    "create_material",
    "extract_material",
    "edit_material",
    "delete_material",
    "list_claims",
    "create_claim",
    "edit_claim",
    "delete_claim",
    "confirm_claim",
    "list_sessions",
    "get_session",
    "delete_session",
    "update_session_title",
    "export_session",
    "create_chat",
    "update_chat_memory",
    "chat_message",
    "claim_chat_message",
    "run_chat_message",
    "retry_chat_message",
    "interrupt_chat",
    "create_interview",
    "interview_report",
    "create_practice",
    "start_interview",
    "answer_interview",
    "claim_interview_answer",
    "run_interview_answer",
    "skip_interview_question",
    "pause_interview",
    "cancel_interview",
    "resume_interview",
)


def active_store():
    return _active_store()


def active_runtime():
    return _active_runtime()


def active_chat_use_cases():
    backend = active_backend()
    return ChatUseCases(active_store(), backend.model_catalog(), run_chat_session,
                        backend.cancel_session_runs)


def active_interview_use_cases():
    backend = active_backend()
    return InterviewUseCases(active_store(), backend.model_catalog(), run_interview_session,
                             backend.cancel_session_runs)


def active_memory_use_cases():
    return MemoryUseCases(active_store())


def http_use_case_error(exc: UseCaseError) -> HTTPException:
    status = {"not_found": 404, "conflict": 409, "invalid": 422}[exc.kind]
    return HTTPException(status, str(exc))


def http_interview_use_case_error(exc: UseCaseError) -> HTTPException:
    if exc.kind == "not_found" and str(exc) == "面试会话不存在":
        return HTTPException(404, "会话不存在")
    if exc.kind == "invalid" and str(exc) == "回答不能为空":
        return HTTPException(422, "内容不能为空")
    return http_use_case_error(exc)


async def run_chat_session(session: dict, on_event: RunEventSink | None = None) -> dict:
    backend = active_backend()
    selected = session.get("model_id", "default")
    run = active_runtime() if selected == "default" else backend.make_runtime(
        active_store(), backend.model_catalog().get(selected),
    )
    return await run.run(session, on_event=on_event)


async def run_interview_session(session: dict, result_handler,
                                on_event: RunEventSink | None = None) -> dict:
    backend = active_backend()
    selected = session.get("model_id", "default")
    run = active_runtime() if selected == "default" else backend.make_runtime(
        active_store(), backend.model_catalog().get(selected),
    )
    return await run.run(session, result_handler=result_handler, on_event=on_event)


def wants_sse(request: Request) -> bool:
    return "text/event-stream" in request.headers.get("accept", "").lower()


def stream_response(operation: Callable[[RunEventSink], Awaitable[dict]]) -> StreamingResponse:
    async def events():
        queue = asyncio.Queue()
        finished = object()

        async def run():
            try:
                await operation(queue.put_nowait)
                queue.put_nowait({"type": "done"})
            except UseCaseError as exc:
                status = {"not_found": 404, "conflict": 409, "invalid": 422}[exc.kind]
                queue.put_nowait({"type": "error", "detail": str(exc), "code": "request_rejected",
                                  "status": status})
            except ModelError as exc:
                logger.warning("Model run failed: code=%s", exc.code)
                queue.put_nowait({"type": "error", "detail": str(exc), "code": exc.code,
                                  "status": exc.status_code})
            except ValueError:
                logger.warning("Model run failed: code=model_invalid_output")
                queue.put_nowait({"type": "error", "detail": "模型输出或上下文不符合要求",
                                  "code": "model_invalid_output", "status": 502})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("Model run failed: code=model_run_failed")
                queue.put_nowait({"type": "error", "detail": "模型运行失败，请稍后重试",
                                  "code": "model_run_failed", "status": 502})
            finally:
                queue.put_nowait(finished)

        task = asyncio.create_task(run())
        try:
            yield "data: {\"type\":\"started\"}\n\n"
            while True:
                event = await queue.get()
                if event is finished:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"
        finally:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def complete_chat_request(result) -> dict | JSONResponse:
    try:
        return await result
    except UseCaseError:
        raise
    except ModelError as exc:
        logger.warning("Model run failed: code=%s", exc.code)
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)
    except ValueError:
        logger.warning("Model run failed: code=model_invalid_output")
        return JSONResponse({"detail": "模型输出或上下文不符合要求", "code": "model_invalid_output"}, status_code=502)
    except Exception:
        logger.error("Model run failed: code=model_run_failed")
        return JSONResponse({"detail": "模型运行失败，请稍后重试", "code": "model_run_failed"}, status_code=502)


async def complete_interview_request(result) -> dict | JSONResponse:
    try:
        return await complete_chat_request(result)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc

class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class InterviewInput(BaseModel):
    goal: str = Field(min_length=1, max_length=500)
    max_questions: int = Field(default=3, ge=1, le=10)
    material_ids: list[str] = Field(default_factory=list, max_length=5)
    claim_ids: list[str] = Field(default_factory=list, max_length=10)
    model_id: str = Field(default="default", min_length=1, max_length=80)


class ChatCreateInput(BaseModel):
    model_id: str = Field(default="default", min_length=1, max_length=80)


class ToolGrantsInput(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=20)


class ChatRunInput(BaseModel):
    version: int = Field(ge=1)


class ChatMemoryInput(BaseModel):
    text: str = Field(max_length=2000)


class LongMemoryInput(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class MemoryCandidateInput(LongMemoryInput):
    source_message_ids: list[int] = Field(min_length=1, max_length=16)


class MemoryCandidateEdit(LongMemoryInput):
    pass


class SessionTitleInput(BaseModel):
    title: str = Field(max_length=80)


class InterviewAnswerInput(TextInput):
    question_number: int = Field(ge=1)


class PracticeInput(BaseModel):
    question_number: int = Field(ge=1)


def require_session(session_id: str, kind: str) -> dict:
    session = active_store().get(session_id, kind)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


@router.get("/api/config")
def config():
    return {"provider": active_backend().provider().mode, "model": active_backend().provider().model, "auth_required": active_backend().auth_required(),
            "config_error": active_backend().provider().configuration_error()}


@router.get("/api/models")
def list_models():
    return active_backend().model_catalog().list()


@router.get("/api/memories")
def list_memories(q: str = Query(default="", max_length=100)):
    return active_memory_use_cases().search(q)


@router.post("/api/memories")
def add_memory(body: LongMemoryInput):
    try:
        return active_memory_use_cases().add(body.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    try:
        active_memory_use_cases().delete(memory_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    return {"deleted": True}


@router.get("/api/memory-candidates")
def list_memory_candidates(
    session_id: str | None = Query(default=None),
    status: Literal["pending", "confirmed"] | None = Query(default=None),
):
    if session_id is not None:
        require_session(session_id, "chat")
    try:
        return active_memory_use_cases().list_candidates(session_id, status)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.get("/api/memory-candidates/{candidate_id}")
def get_memory_candidate(candidate_id: str):
    try:
        return active_memory_use_cases().get_candidate(candidate_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/memory-candidates")
def create_memory_candidate(session_id: str, body: MemoryCandidateInput):
    require_session(session_id, "chat")
    try:
        return active_memory_use_cases().create_candidate(
            session_id, body.content, body.source_message_ids
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/memory-candidates/{candidate_id}")
def update_memory_candidate(candidate_id: str, body: MemoryCandidateEdit):
    try:
        return active_memory_use_cases().edit_candidate(candidate_id, body.content)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/memory-candidates/{candidate_id}/confirm")
def confirm_memory_candidate(candidate_id: str):
    try:
        return active_memory_use_cases().confirm_candidate(candidate_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.delete("/api/memory-candidates/{candidate_id}")
def delete_memory_candidate(candidate_id: str):
    try:
        active_memory_use_cases().delete_candidate(candidate_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    return {"deleted": True}


@router.get("/api/mcp/tools")
async def list_mcp_tools():
    try:
        return await active_backend().mcp_servers().discover()
    except Exception as exc:
        raise HTTPException(502, "MCP 工具发现失败，请检查本地服务器配置") from exc


@router.get("/api/mcp/servers")
async def list_mcp_servers():
    """Report each configured server so operators can see why a tool is missing."""
    return {"servers": await active_backend().mcp_servers().report()}


@router.get("/api/tools")
async def list_tools():
    try:
        return active_backend().local_plugins().discover() + await active_backend().mcp_servers().discover()
    except Exception as exc:
        raise HTTPException(502, "工具发现失败，请检查本地插件或 MCP 配置") from exc


@router.get("/api/{kind}/sessions/{session_id}/tools")
def get_session_tools(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    require_session(session_id, kind)
    return {"names": ToolGrantUseCases(active_store()).list(session_id)}


@router.put("/api/{kind}/sessions/{session_id}/tools")
async def set_session_tools(kind: str, session_id: str, body: ToolGrantsInput):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = require_session(session_id, kind)
    if session["status"] == "running":
        raise HTTPException(409, "运行中不能修改工具授权")
    try:
        available = {item["name"]: item
                     for item in active_backend().local_plugins().discover() + await active_backend().mcp_servers().discover()}
    except Exception as exc:
        raise HTTPException(502, "MCP 工具发现失败，请检查本地服务器配置") from exc
    try:
        names = ToolGrantUseCases(active_store()).replace(
            session_id, body.names, available.values(),
        )
    except ToolGrantError as exc:
        message = "包含未配置的外部工具" if exc.kind == "unavailable" else str(exc)
        raise HTTPException(422, message) from exc
    return {"names": names}


@router.get("/api/data/backup")
def backup_data():
    return Response(active_store().backup(), media_type="application/vnd.sqlite3",
                    headers={"Content-Disposition": 'attachment; filename="nico-agent-backup.sqlite3"'})


router.include_router(auth_router)
router.include_router(material_router)


@router.get("/api/{kind}/sessions")
def list_sessions(kind: str, q: str = Query(default="", max_length=100)):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    return active_store().list_sessions(kind, q)


@router.get("/api/{kind}/sessions/{session_id}")
def get_session(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = require_session(session_id, kind)
    result = {"session": session, "messages": active_store().messages(session_id, kind), "events": active_store().events(session_id, kind)}
    if kind == "interview":
        result["resources"] = active_store().interview_resources(session_id)
        result["practice_source"] = active_store().practice_source(session_id)
    return result


@router.delete("/api/{kind}/sessions/{session_id}")
def delete_session(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    try:
        delete_session_and_cancel_runs(active_store(), session_id, kind,
                                       active_backend().cancel_session_runs)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    return {"deleted": True}


@router.put("/api/{kind}/sessions/{session_id}/title")
def update_session_title(kind: str, session_id: str, body: SessionTitleInput):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = active_store().set_session_title(session_id, kind, " ".join(body.title.split()))
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


@router.get("/api/{kind}/sessions/{session_id}/export.md")
def export_session(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = require_session(session_id, kind)
    messages = active_store().messages(session_id, kind)
    report = active_store().interview_report(session_id) if kind == "interview" else None
    content = render_session_markdown(session, messages, report)
    return Response(content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{kind}-{session_id}.md"'})


@router.post("/api/chat/sessions")
def create_chat(body: ChatCreateInput | None = None):
    model_id = body.model_id if body else "default"
    try:
        return active_chat_use_cases().create(model_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/chat/sessions/{session_id}/memory")
def update_chat_memory(session_id: str, body: ChatMemoryInput):
    try:
        return {"text": active_chat_use_cases().set_memory(session_id, body.text)}
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: TextInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_chat_use_cases().send_message(
            session_id, body.text, on_event,
        ))
    try:
        return await complete_chat_request(
            active_chat_use_cases().send_message(session_id, body.text)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/chat/sessions/{session_id}/messages/claim")
def claim_chat_message(session_id: str, body: TextInput):
    try:
        return active_chat_use_cases().claim_message(session_id, body.text)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/chat/sessions/{session_id}/messages/run")
async def run_chat_message(session_id: str, body: ChatRunInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_chat_use_cases().run_claimed_message(
            session_id, body.version, on_event,
        ))
    try:
        return await complete_chat_request(
            active_chat_use_cases().run_claimed_message(session_id, body.version)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/retry")
async def retry_chat_message(session_id: str, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_chat_use_cases().retry_message(
            session_id, on_event,
        ))
    try:
        return await complete_chat_request(
            active_chat_use_cases().retry_message(session_id)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/interrupt")
def interrupt_chat(session_id: str):
    try:
        return active_chat_use_cases().interrupt(session_id)
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/interview/sessions")
def create_interview(body: InterviewInput):
    try:
        return active_interview_use_cases().create(
            body.goal, tuple(body.material_ids), tuple(body.claim_ids),
            body.max_questions, body.model_id,
        )
    except ValueError as exc:
        message = "内容不能为空" if str(exc) == "岗位目标不能为空" else str(exc)
        raise HTTPException(422, message) from exc


@router.get("/api/interview/sessions/{session_id}/report")
def interview_report(session_id: str):
    try:
        return active_interview_use_cases().report(session_id)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/practice")
def create_practice(session_id: str, body: PracticeInput):
    try:
        return active_interview_use_cases().create_practice(session_id, body.question_number)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("/api/interview/sessions/{session_id}/start")
async def start_interview(session_id: str, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().start(
            session_id, on_event,
        ))
    return await complete_interview_request(active_interview_use_cases().start(session_id))


@router.post("/api/interview/sessions/{session_id}/answers")
async def answer_interview(session_id: str, body: TextInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().answer(
            session_id, body.text, on_event,
        ))
    return await complete_interview_request(active_interview_use_cases().answer(session_id, body.text))


@router.post("/api/interview/sessions/{session_id}/answers/claim")
def claim_interview_answer(session_id: str, body: InterviewAnswerInput):
    try:
        return active_interview_use_cases().claim_answer(
            session_id, body.text, body.question_number,
        )
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/answers/run")
async def run_interview_answer(session_id: str, body: ChatRunInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().run_claimed_answer(
            session_id, body.version, on_event,
        ))
    return await complete_interview_request(
        active_interview_use_cases().run_claimed_answer(session_id, body.version)
    )


@router.post("/api/interview/sessions/{session_id}/skip")
async def skip_interview_question(session_id: str, body: PracticeInput, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().skip(
            session_id, body.question_number, on_event,
        ))
    return await complete_interview_request(
        active_interview_use_cases().skip(session_id, body.question_number)
    )


@router.post("/api/interview/sessions/{session_id}/pause")
def pause_interview(session_id: str):
    try:
        return active_interview_use_cases().pause(session_id)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/cancel")
def cancel_interview(session_id: str):
    try:
        return active_interview_use_cases().cancel(session_id)
    except UseCaseError as exc:
        raise http_interview_use_case_error(exc) from exc


@router.post("/api/interview/sessions/{session_id}/resume")
async def resume_interview(session_id: str, request: Request):
    if wants_sse(request):
        return stream_response(lambda on_event: active_interview_use_cases().resume(
            session_id, on_event,
        ))
    return await complete_interview_request(active_interview_use_cases().resume(session_id))
