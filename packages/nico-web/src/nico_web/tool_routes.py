"""Tool discovery and per-session grant HTTP routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from nico_application import ToolGrantError, ToolGrantUseCases

from .backend import active_backend, active_store
from .route_support import require_session


router = APIRouter()


class ToolGrantsInput(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=20)


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
