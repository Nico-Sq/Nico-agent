"""HTTP routes and request models for Nico-agent."""

from typing import Literal

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from nico_application import (
    ChatUseCases,
    InterviewUseCases,
    MaterialUseCases,
    MemoryUseCases,
    ToolGrantError,
    ToolGrantUseCases,
    UseCaseError,
)
from nico_contracts.errors import ModelError

from .backend import active_backend, active_runtime as _active_runtime, active_store as _active_store


router = APIRouter()


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


def active_material_use_cases():
    return MaterialUseCases(active_store())


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


async def run_chat_session(session: dict) -> dict:
    backend = active_backend()
    selected = session.get("model_id", "default")
    run = active_runtime() if selected == "default" else backend.make_runtime(
        active_store(), backend.model_catalog().get(selected),
    )
    return await run.run(session)


async def run_interview_session(session: dict, result_handler) -> dict:
    backend = active_backend()
    selected = session.get("model_id", "default")
    run = active_runtime() if selected == "default" else backend.make_runtime(
        active_store(), backend.model_catalog().get(selected),
    )
    return await run.run(session, result_handler=result_handler)


async def complete_chat_request(result) -> dict | JSONResponse:
    try:
        return await result
    except UseCaseError:
        raise
    except ModelError as exc:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)
    except ValueError:
        return JSONResponse({"detail": "模型输出或上下文不符合要求", "code": "model_invalid_output"}, status_code=502)
    except Exception:
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


class MaterialInput(BaseModel):
    kind: Literal["resume", "job_description", "project"]
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)


class ClaimInput(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    material_id: str | None = None


class MaterialEdit(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)


class ClaimEdit(BaseModel):
    text: str = Field(min_length=1, max_length=500)


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


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_-]{3,32}$")
    password: str = Field(min_length=8, max_length=128)


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


def set_login_cookie(response: Response, request: Request, user_id: str):
    response.set_cookie("nico_session", active_backend().auth_store().issue_token(user_id), max_age=7 * 24 * 3600,
                        httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/")


@router.post("/api/auth/register")
def register(body: Credentials, request: Request, response: Response):
    if not active_backend().auth_required():
        raise HTTPException(404)
    user_id = active_backend().auth_store().register(body.username, body.password)
    if user_id is None:
        raise HTTPException(409, "用户名已存在")
    set_login_cookie(response, request, user_id)
    return {"username": body.username.lower()}


@router.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response):
    if not active_backend().auth_required():
        raise HTTPException(404)
    try:
        user_id = active_backend().auth_store().login(body.username, body.password)
    except active_backend().login_rate_limited_error as exc:
        raise HTTPException(429, "登录尝试过多，请 15 分钟后重试") from exc
    if user_id is None:
        raise HTTPException(401, "用户名或密码错误")
    set_login_cookie(response, request, user_id)
    return {"username": body.username.lower()}


@router.get("/api/auth/me")
def current_user(request: Request):
    if not active_backend().auth_required():
        raise HTTPException(404)
    user_id = active_backend().auth_store().resolve_token(request.cookies.get("nico_session"))
    if user_id is None:
        raise HTTPException(401, "请先登录")
    return {"username": active_backend().auth_store().username(user_id)}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    if not active_backend().auth_required():
        raise HTTPException(404)
    active_backend().auth_store().revoke_token(request.cookies.get("nico_session"))
    response.delete_cookie("nico_session", path="/")
    return {"logged_out": True}


@router.get("/api/materials")
def list_materials():
    return active_material_use_cases().list_materials()


@router.post("/api/materials")
def create_material(body: MaterialInput):
    try:
        return active_material_use_cases().create_material(body.kind, body.title, body.content)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/api/materials/extract")
async def extract_material(file: UploadFile = File(...)):
    data = await file.read(active_backend().max_file_bytes + 1)
    try:
        return {"filename": file.filename, "content": active_backend().extract_text(file.filename or "", data)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/materials/{material_id}")
def edit_material(material_id: str, body: MaterialEdit):
    try:
        material = active_material_use_cases().edit_material(
            material_id, body.title, body.content,
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if material is None:
        raise HTTPException(404, "材料不存在或已撤回")
    return material


@router.delete("/api/materials/{material_id}")
def delete_material(material_id: str):
    if not active_material_use_cases().delete_material(material_id):
        raise HTTPException(404, "材料不存在或已撤回")
    return {"revoked": True}


@router.get("/api/claims")
def list_claims():
    return active_material_use_cases().list_claims()


@router.post("/api/claims")
def create_claim(body: ClaimInput):
    try:
        return active_material_use_cases().create_claim(body.text, body.material_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.put("/api/claims/{claim_id}")
def edit_claim(claim_id: str, body: ClaimEdit):
    try:
        claim = active_material_use_cases().edit_claim(claim_id, body.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if claim is None:
        raise HTTPException(404, "经历不存在或已撤回")
    return claim


@router.delete("/api/claims/{claim_id}")
def delete_claim(claim_id: str):
    if not active_material_use_cases().delete_claim(claim_id):
        raise HTTPException(404, "经历不存在或已撤回")
    return {"revoked": True}


@router.post("/api/claims/{claim_id}/confirm")
def confirm_claim(claim_id: str):
    claim = active_material_use_cases().confirm_claim(claim_id)
    if claim is None:
        raise HTTPException(404, "待确认经历不存在")
    return claim


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
    if not active_store().delete_session(session_id, kind):
        raise HTTPException(404, "会话不存在")
    active_backend().cancel_session_runs(session_id)
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
    lines = ["# 聊天记录" if kind == "chat" else "# 模拟面试记录", ""]
    if session["title"]:
        lines.extend([f"会话名称：{session['title']}", ""])
    if kind == "chat":
        if session["chat_memory"]:
            lines.extend(["## 固定备注", "", session["chat_memory"], ""])
        for message in active_store().messages(session_id, kind):
            lines.extend([f"## {'用户' if message['role'] == 'user' else '聊天助手'}", "",
                          message["content"], ""])
    else:
        report = active_store().interview_report(session_id)
        lines.extend([f"岗位目标：{session['goal']}", f"状态：{session['status']}", ""])
        if report["practice_source"]:
            source = report["practice_source"]
            lines.extend(["## 复练来源", "", f"原题：{source['question']}", "",
                          f"原回答：{source['answer'] or '无'}", "",
                          f"原反馈：{source['feedback'] or '无'}", ""])
        for item in report["items"]:
            lines.extend([f"## 第 {item['number']} 题", "", item["question"], "",
                          "### 回答", "", "已跳过" if item["skipped"] else item["answer"] or "未回答", "",
                          "### 反馈", "", item["feedback"] or "尚无反馈", ""])
            if item["evidence"]:
                lines.extend(["回答依据：" + "；".join(item["evidence"]), ""])
        if report["summary"]:
            lines.extend(["## 全场总结", "", report["summary"], ""])
        lines.extend(["## 完整对话", ""])
        for message in active_store().messages(session_id, kind):
            lines.extend([f"### {'用户' if message['role'] == 'user' else '面试官'}", "",
                          "已跳过本题" if message["content"] == "[SKIPPED_QUESTION]" else message["content"], ""])
    return Response("\n".join(lines), media_type="text/markdown; charset=utf-8",
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
async def chat_message(session_id: str, body: TextInput):
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
async def run_chat_message(session_id: str, body: ChatRunInput):
    try:
        return await complete_chat_request(
            active_chat_use_cases().run_claimed_message(session_id, body.version)
        )
    except UseCaseError as exc:
        raise http_use_case_error(exc) from exc


@router.post("/api/chat/sessions/{session_id}/retry")
async def retry_chat_message(session_id: str):
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
async def start_interview(session_id: str):
    return await complete_interview_request(active_interview_use_cases().start(session_id))


@router.post("/api/interview/sessions/{session_id}/answers")
async def answer_interview(session_id: str, body: TextInput):
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
async def run_interview_answer(session_id: str, body: ChatRunInput):
    return await complete_interview_request(
        active_interview_use_cases().run_claimed_answer(session_id, body.version)
    )


@router.post("/api/interview/sessions/{session_id}/skip")
async def skip_interview_question(session_id: str, body: PracticeInput):
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
async def resume_interview(session_id: str):
    return await complete_interview_request(active_interview_use_cases().resume(session_id))
