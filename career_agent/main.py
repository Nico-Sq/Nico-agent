import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .auth import AuthStore, LoginRateLimited
from .auto_backup import backup_all
from .extract import MAX_FILE_BYTES, extract_text
from .mcp import MCPServers
from .models import ModelCatalog
from .plugins import LocalPlugins
from .provider import ModelError, Provider
from .runtime import AgentRuntime
from .store import Store


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() == "true"
auth_store = AuthStore(DATA_DIR / "auth.sqlite3") if AUTH_REQUIRED else None
store = Store(DATA_DIR / "career_agent.sqlite3")
provider = Provider()
model_catalog = ModelCatalog()
mcp_servers = MCPServers()
local_plugins = LocalPlugins()
runtime = AgentRuntime(store, provider, mcp_servers, local_plugins)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    async def backup_once():
        try:
            await asyncio.to_thread(backup_all, DATA_DIR)
        except Exception:
            logging.exception("本地业务数据自动备份失败")

    async def backup_loop():
        while True:
            await asyncio.sleep(3600)
            await backup_once()

    await backup_once()
    task = asyncio.create_task(backup_loop())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await backup_once()


app = FastAPI(title="双线求职 Agent", lifespan=lifespan)
request_store: ContextVar[Store | None] = ContextVar("request_store", default=None)
request_runtime: ContextVar[AgentRuntime | None] = ContextVar("request_runtime", default=None)


def active_store() -> Store:
    return request_store.get() or store


def active_runtime() -> AgentRuntime:
    return request_runtime.get() or runtime


@app.middleware("http")
async def bind_account(request: Request, call_next):
    if not AUTH_REQUIRED or request.url.path in (
        "/", "/api/config", "/api/auth/register", "/api/auth/login", "/api/auth/logout",
    ) or request.url.path.startswith("/static/"):
        return await call_next(request)
    user_id = auth_store.resolve_token(request.cookies.get("nico_session"))
    if user_id is None:
        return JSONResponse({"detail": "请先登录"}, status_code=401)
    scoped_store = Store(DATA_DIR / "users" / user_id / "career_agent.sqlite3")
    store_token = request_store.set(scoped_store)
    runtime_token = request_runtime.set(AgentRuntime(scoped_store, provider, mcp_servers, local_plugins))
    try:
        return await call_next(request)
    finally:
        request_store.reset(store_token)
        request_runtime.reset(runtime_token)


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


class SessionTitleInput(BaseModel):
    title: str = Field(max_length=80)


class InterviewAnswerInput(TextInput):
    question_number: int = Field(ge=1)


class PracticeInput(BaseModel):
    question_number: int = Field(ge=1)


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_-]{3,32}$")
    password: str = Field(min_length=8, max_length=128)


def nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(422, "内容不能为空")
    return value


def require_session(session_id: str, kind: str) -> dict:
    session = active_store().get(session_id, kind)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


async def complete_run(session: dict) -> dict | JSONResponse:
    try:
        selected = session.get("model_id", "default")
        run = active_runtime() if selected == "default" else AgentRuntime(
            active_store(), model_catalog.get(selected), mcp_servers, local_plugins)
        return await run.run(session)
    except ModelError as exc:
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)
    except ValueError:
        return JSONResponse({"detail": "模型输出或上下文不符合要求", "code": "model_invalid_output"}, status_code=502)
    except Exception:
        return JSONResponse({"detail": "模型运行失败，请稍后重试", "code": "model_run_failed"}, status_code=502)


@app.get("/api/config")
def config():
    return {"provider": provider.mode, "model": provider.model, "auth_required": AUTH_REQUIRED,
            "config_error": provider.configuration_error()}


@app.get("/api/models")
def list_models():
    return model_catalog.list()


@app.get("/api/memories")
def list_memories(q: str = Query(default="", max_length=100)):
    return active_store().search_memories(q)


@app.post("/api/memories")
def add_memory(body: LongMemoryInput):
    return active_store().add_memory(nonempty(body.content))


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: str):
    if not active_store().delete_memory(memory_id):
        raise HTTPException(404, "记忆不存在")
    return {"deleted": True}


@app.get("/api/mcp/tools")
async def list_mcp_tools():
    try:
        return await mcp_servers.discover()
    except Exception as exc:
        raise HTTPException(502, "MCP 工具发现失败，请检查本地服务器配置") from exc


@app.get("/api/tools")
async def list_tools():
    try:
        return local_plugins.discover() + await mcp_servers.discover()
    except Exception as exc:
        raise HTTPException(502, "工具发现失败，请检查本地插件或 MCP 配置") from exc


@app.get("/api/{kind}/sessions/{session_id}/tools")
def get_session_tools(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    require_session(session_id, kind)
    return {"names": active_store().tool_grants(session_id)}


@app.put("/api/{kind}/sessions/{session_id}/tools")
async def set_session_tools(kind: str, session_id: str, body: ToolGrantsInput):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = require_session(session_id, kind)
    if session["status"] == "running":
        raise HTTPException(409, "运行中不能修改工具授权")
    try:
        available = {item["name"] for item in local_plugins.discover() + await mcp_servers.discover()}
    except Exception as exc:
        raise HTTPException(502, "MCP 工具发现失败，请检查本地服务器配置") from exc
    if not set(body.names) <= available:
        raise HTTPException(422, "包含未配置的 MCP 工具")
    return {"names": active_store().set_tool_grants(session_id, body.names)}


@app.get("/api/data/backup")
def backup_data():
    return Response(active_store().backup(), media_type="application/vnd.sqlite3",
                    headers={"Content-Disposition": 'attachment; filename="nico-agent-backup.sqlite3"'})


def set_login_cookie(response: Response, request: Request, user_id: str):
    response.set_cookie("nico_session", auth_store.issue_token(user_id), max_age=7 * 24 * 3600,
                        httponly=True, secure=request.url.scheme == "https", samesite="strict", path="/")


@app.post("/api/auth/register")
def register(body: Credentials, request: Request, response: Response):
    if not AUTH_REQUIRED:
        raise HTTPException(404)
    user_id = auth_store.register(body.username, body.password)
    if user_id is None:
        raise HTTPException(409, "用户名已存在")
    set_login_cookie(response, request, user_id)
    return {"username": body.username.lower()}


@app.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response):
    if not AUTH_REQUIRED:
        raise HTTPException(404)
    try:
        user_id = auth_store.login(body.username, body.password)
    except LoginRateLimited as exc:
        raise HTTPException(429, "登录尝试过多，请 15 分钟后重试") from exc
    if user_id is None:
        raise HTTPException(401, "用户名或密码错误")
    set_login_cookie(response, request, user_id)
    return {"username": body.username.lower()}


@app.get("/api/auth/me")
def current_user(request: Request):
    if not AUTH_REQUIRED:
        raise HTTPException(404)
    user_id = auth_store.resolve_token(request.cookies.get("nico_session"))
    if user_id is None:
        raise HTTPException(401, "请先登录")
    return {"username": auth_store.username(user_id)}


@app.post("/api/auth/logout")
def logout(request: Request, response: Response):
    if not AUTH_REQUIRED:
        raise HTTPException(404)
    auth_store.revoke_token(request.cookies.get("nico_session"))
    response.delete_cookie("nico_session", path="/")
    return {"logged_out": True}


@app.get("/api/materials")
def list_materials():
    return active_store().list_materials()


@app.post("/api/materials")
def create_material(body: MaterialInput):
    return active_store().create_material(body.kind, nonempty(body.title), nonempty(body.content))


@app.post("/api/materials/extract")
async def extract_material(file: UploadFile = File(...)):
    data = await file.read(MAX_FILE_BYTES + 1)
    try:
        return {"filename": file.filename, "content": extract_text(file.filename or "", data)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/materials/{material_id}")
def edit_material(material_id: str, body: MaterialEdit):
    material = active_store().replace_material(material_id, nonempty(body.title), nonempty(body.content))
    if material is None:
        raise HTTPException(404, "材料不存在或已撤回")
    return material


@app.delete("/api/materials/{material_id}")
def delete_material(material_id: str):
    if not active_store().revoke_material(material_id):
        raise HTTPException(404, "材料不存在或已撤回")
    return {"revoked": True}


@app.get("/api/claims")
def list_claims():
    return active_store().list_claims()


@app.post("/api/claims")
def create_claim(body: ClaimInput):
    try:
        return active_store().create_claim(nonempty(body.text), body.material_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/claims/{claim_id}")
def edit_claim(claim_id: str, body: ClaimEdit):
    claim = active_store().replace_claim(claim_id, nonempty(body.text))
    if claim is None:
        raise HTTPException(404, "经历不存在或已撤回")
    return claim


@app.delete("/api/claims/{claim_id}")
def delete_claim(claim_id: str):
    if not active_store().revoke_claim(claim_id):
        raise HTTPException(404, "经历不存在或已撤回")
    return {"revoked": True}


@app.post("/api/claims/{claim_id}/confirm")
def confirm_claim(claim_id: str):
    claim = active_store().confirm_claim(claim_id)
    if claim is None:
        raise HTTPException(404, "待确认经历不存在")
    return claim


@app.get("/api/{kind}/sessions")
def list_sessions(kind: str, q: str = Query(default="", max_length=100)):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    return active_store().list_sessions(kind, q)


@app.get("/api/{kind}/sessions/{session_id}")
def get_session(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = require_session(session_id, kind)
    result = {"session": session, "messages": active_store().messages(session_id, kind), "events": active_store().events(session_id, kind)}
    if kind == "interview":
        result["resources"] = active_store().interview_resources(session_id)
        result["practice_source"] = active_store().practice_source(session_id)
    return result


@app.delete("/api/{kind}/sessions/{session_id}")
def delete_session(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    if not active_store().delete_session(session_id, kind):
        raise HTTPException(404, "会话不存在")
    return {"deleted": True}


@app.put("/api/{kind}/sessions/{session_id}/title")
def update_session_title(kind: str, session_id: str, body: SessionTitleInput):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = active_store().set_session_title(session_id, kind, " ".join(body.title.split()))
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


@app.get("/api/{kind}/sessions/{session_id}/export.md")
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


@app.post("/api/chat/sessions")
def create_chat(body: ChatCreateInput | None = None):
    model_id = body.model_id if body else "default"
    try:
        model = model_catalog.get(model_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if model.configuration_error():
        raise HTTPException(422, model.configuration_error())
    return active_store().create("chat", model_id=model_id)


@app.put("/api/chat/sessions/{session_id}/memory")
def update_chat_memory(session_id: str, body: ChatMemoryInput):
    if active_store().set_chat_memory(session_id, body.text.strip()) is None:
        raise HTTPException(404, "聊天会话不存在")
    return {"text": body.text.strip()}


@app.post("/api/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: TextInput):
    require_session(session_id, "chat")
    session = active_store().claim_run(session_id, "chat", "active", nonempty(body.text))
    if session is None:
        raise HTTPException(409, "当前不能发送消息")
    return await complete_run(session)


@app.post("/api/chat/sessions/{session_id}/messages/claim")
def claim_chat_message(session_id: str, body: TextInput):
    require_session(session_id, "chat")
    session = active_store().claim_run(session_id, "chat", "active", nonempty(body.text))
    if session is None:
        raise HTTPException(409, "当前不能发送消息")
    return session


@app.post("/api/chat/sessions/{session_id}/messages/run")
async def run_chat_message(session_id: str, body: ChatRunInput):
    session = require_session(session_id, "chat")
    if session["status"] != "running" or session["version"] != body.version:
        return {"interrupted": True, "session": session}
    return await complete_run(session)


@app.post("/api/chat/sessions/{session_id}/retry")
async def retry_chat_message(session_id: str):
    require_session(session_id, "chat")
    session = active_store().retry_chat(session_id)
    if session is None:
        raise HTTPException(409, "当前没有待重试的消息")
    return await complete_run(session)


@app.post("/api/chat/sessions/{session_id}/interrupt")
def interrupt_chat(session_id: str):
    require_session(session_id, "chat")
    result = active_store().interrupt(session_id, "chat")
    if result is None:
        raise HTTPException(409, "当前没有可中断的运行")
    return result


@app.post("/api/interview/sessions")
def create_interview(body: InterviewInput):
    try:
        model = model_catalog.get(body.model_id)
        if model.configuration_error():
            raise ValueError(model.configuration_error())
        return active_store().create("interview", nonempty(body.goal), tuple(body.material_ids),
                            tuple(body.claim_ids), body.max_questions, body.model_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/interview/sessions/{session_id}/report")
def interview_report(session_id: str):
    report = active_store().interview_report(session_id)
    if report is None:
        raise HTTPException(404, "面试不存在")
    return report


@app.post("/api/interview/sessions/{session_id}/practice")
def create_practice(session_id: str, body: PracticeInput):
    require_session(session_id, "interview")
    try:
        return active_store().create_practice(session_id, body.question_number)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/interview/sessions/{session_id}/start")
async def start_interview(session_id: str):
    require_session(session_id, "interview")
    session = active_store().claim_run(session_id, "interview", "new")
    if session is None:
        raise HTTPException(409, "面试已开始")
    return await complete_run(session)


@app.post("/api/interview/sessions/{session_id}/answers")
async def answer_interview(session_id: str, body: TextInput):
    require_session(session_id, "interview")
    session = active_store().claim_run(session_id, "interview", "awaiting_answer", nonempty(body.text))
    if session is None:
        raise HTTPException(409, "当前不能提交回答")
    return await complete_run(session)


@app.post("/api/interview/sessions/{session_id}/answers/claim")
def claim_interview_answer(session_id: str, body: InterviewAnswerInput):
    require_session(session_id, "interview")
    session = active_store().claim_run(session_id, "interview", "awaiting_answer", nonempty(body.text),
                              expected_question=body.question_number)
    if session is None:
        raise HTTPException(409, "当前题目已变化，无法提交回答")
    return session


@app.post("/api/interview/sessions/{session_id}/answers/run")
async def run_interview_answer(session_id: str, body: ChatRunInput):
    session = require_session(session_id, "interview")
    if session["status"] != "running" or session["version"] != body.version:
        return {"interrupted": True, "session": session}
    return await complete_run(session)


@app.post("/api/interview/sessions/{session_id}/skip")
async def skip_interview_question(session_id: str, body: PracticeInput):
    require_session(session_id, "interview")
    session = active_store().skip_interview_question(session_id, body.question_number)
    if session is None:
        raise HTTPException(409, "当前题目已变化，无法跳过")
    return await complete_run(session)


@app.post("/api/interview/sessions/{session_id}/pause")
def pause_interview(session_id: str):
    require_session(session_id, "interview")
    result = active_store().interrupt(session_id, "interview")
    if result is None:
        raise HTTPException(409, "当前不能暂停")
    return result


@app.post("/api/interview/sessions/{session_id}/cancel")
def cancel_interview(session_id: str):
    require_session(session_id, "interview")
    result = active_store().cancel_interview(session_id)
    if result is None:
        raise HTTPException(409, "当前不能结束面试")
    return result


@app.post("/api/interview/sessions/{session_id}/resume")
async def resume_interview(session_id: str):
    require_session(session_id, "interview")
    result = active_store().resume(session_id)
    if result is None:
        raise HTTPException(409, "当前不能恢复")
    session, needs_run = result
    return await complete_run(session) if needs_run else {"session": session}


STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
