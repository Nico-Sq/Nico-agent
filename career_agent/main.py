import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .provider import Provider
from .runtime import AgentRuntime
from .store import Store


ROOT = Path(__file__).resolve().parent.parent
store = Store(Path(os.getenv("DATA_DIR", ROOT / "data")) / "career_agent.sqlite3")
provider = Provider()
runtime = AgentRuntime(store, provider)
app = FastAPI(title="双线求职 Agent")


class TextInput(BaseModel):
    text: str = Field(min_length=1, max_length=12000)


class InterviewInput(BaseModel):
    goal: str = Field(min_length=1, max_length=500)


def require_session(session_id: str, kind: str) -> dict:
    session = store.get(session_id, kind)
    if session is None:
        raise HTTPException(404, "会话不存在")
    return session


async def complete_run(session: dict) -> dict:
    try:
        return await runtime.run(session)
    except Exception as exc:
        raise HTTPException(502, f"模型运行失败：{exc}") from exc


@app.get("/api/config")
def config():
    return {"provider": provider.mode, "model": provider.model}


@app.get("/api/{kind}/sessions")
def list_sessions(kind: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    return store.list_sessions(kind)


@app.get("/api/{kind}/sessions/{session_id}")
def get_session(kind: str, session_id: str):
    if kind not in ("chat", "interview"):
        raise HTTPException(404)
    session = require_session(session_id, kind)
    return {"session": session, "messages": store.messages(session_id, kind), "events": store.events(session_id, kind)}


@app.post("/api/chat/sessions")
def create_chat():
    return store.create("chat")


@app.post("/api/chat/sessions/{session_id}/messages")
async def chat_message(session_id: str, body: TextInput):
    require_session(session_id, "chat")
    session = store.claim_run(session_id, "chat", "active", body.text.strip())
    if session is None:
        raise HTTPException(409, "会话正在运行")
    return await complete_run(session)


@app.post("/api/chat/sessions/{session_id}/interrupt")
def interrupt_chat(session_id: str):
    require_session(session_id, "chat")
    result = store.interrupt(session_id, "chat")
    if result is None:
        raise HTTPException(409, "当前没有可中断的运行")
    return result


@app.post("/api/interview/sessions")
def create_interview(body: InterviewInput):
    return store.create("interview", body.goal.strip())


@app.post("/api/interview/sessions/{session_id}/start")
async def start_interview(session_id: str):
    require_session(session_id, "interview")
    session = store.claim_run(session_id, "interview", "new")
    if session is None:
        raise HTTPException(409, "面试已开始")
    return await complete_run(session)


@app.post("/api/interview/sessions/{session_id}/answers")
async def answer_interview(session_id: str, body: TextInput):
    require_session(session_id, "interview")
    session = store.claim_run(session_id, "interview", "awaiting_answer", body.text.strip())
    if session is None:
        raise HTTPException(409, "当前不能提交回答")
    return await complete_run(session)


@app.post("/api/interview/sessions/{session_id}/pause")
def pause_interview(session_id: str):
    require_session(session_id, "interview")
    result = store.interrupt(session_id, "interview")
    if result is None:
        raise HTTPException(409, "当前不能暂停")
    return result


@app.post("/api/interview/sessions/{session_id}/resume")
async def resume_interview(session_id: str):
    require_session(session_id, "interview")
    result = store.resume(session_id)
    if result is None:
        raise HTTPException(409, "当前不能恢复")
    session, needs_run = result
    return await complete_run(session) if needs_run else {"session": session}


STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
