"""Authentication HTTP routes and request model."""

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .backend import active_backend


router = APIRouter()


class Credentials(BaseModel):
    username: str = Field(pattern=r"^[A-Za-z0-9_-]{3,32}$")
    password: str = Field(min_length=8, max_length=128)


def set_login_cookie(response: Response, request: Request, user_id: str):
    response.set_cookie(
        "nico_session",
        active_backend().auth_store().issue_token(user_id),
        max_age=7 * 24 * 3600,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )


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
