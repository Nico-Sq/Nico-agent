"""HTTP routes and request models for Nico-agent."""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from nico_application import (
    UseCaseError,
    delete_session_and_cancel_runs,
)
from nico_application.export import render_session_markdown

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
from .memory_routes import (
    LongMemoryInput,
    MemoryCandidateEdit,
    MemoryCandidateInput,
    active_memory_use_cases,
    add_memory,
    confirm_memory_candidate,
    create_memory_candidate,
    delete_memory,
    delete_memory_candidate,
    get_memory_candidate,
    list_memories,
    list_memory_candidates,
    router as memory_router,
    update_memory_candidate,
)
from .chat_routes import (
    ChatCreateInput,
    ChatMemoryInput,
    claim_chat_message,
    chat_message,
    create_chat,
    interrupt_chat,
    router as chat_router,
    retry_chat_message,
    run_chat_message,
    update_chat_memory,
)
from .interview_routes import (
    InterviewAnswerInput,
    InterviewInput,
    PracticeInput,
    answer_interview,
    cancel_interview,
    claim_interview_answer,
    create_interview,
    create_practice,
    interview_report,
    pause_interview,
    resume_interview,
    router as interview_router,
    run_interview_answer,
    skip_interview_question,
    start_interview,
)
from .route_support import http_interview_use_case_error, http_use_case_error, require_session
from .tool_routes import (
    ToolGrantsInput,
    get_session_tools,
    list_mcp_servers,
    list_mcp_tools,
    list_tools,
    router as tool_router,
    set_session_tools,
)
from .workflow_support import (
    ChatRunInput,
    TextInput,
    active_chat_use_cases,
    active_interview_use_cases,
    active_runtime,
    active_store,
    complete_chat_request,
    complete_interview_request,
    run_chat_session,
    run_interview_session,
    stream_response,
    wants_sse,
)
from .backend import active_backend


router = APIRouter()

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


class SessionTitleInput(BaseModel):
    title: str = Field(max_length=80)


@router.get("/api/config")
def config():
    return {"provider": active_backend().provider().mode, "model": active_backend().provider().model, "auth_required": active_backend().auth_required(),
            "config_error": active_backend().provider().configuration_error()}


@router.get("/api/models")
def list_models():
    return active_backend().model_catalog().list()


router.include_router(memory_router)
router.include_router(tool_router)


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


router.include_router(chat_router)
router.include_router(interview_router)
