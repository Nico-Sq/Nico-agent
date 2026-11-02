"""FastAPI factory; service construction stays in the caller."""

from importlib.resources import files
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .backend import (
    WebBackend,
    bind_backend,
    bind_request_resources,
    reset_backend,
    reset_request_resources,
)
from .routes import router


_MATERIAL_UPLOAD_OVERHEAD_BYTES = 64 * 1024


class _RequestBodyTooLarge(Exception):
    pass


class _MaterialUploadBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["method"] != "POST"
                or scope["path"] != "/api/materials/extract"):
            await self.app(scope, receive, send)
            return

        content_length = next(
            (value for name, value in scope["headers"] if name == b"content-length"), None,
        )
        declared_length = None
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                pass
        if declared_length is not None and declared_length > self.max_bytes:
            await JSONResponse({"detail": "上传请求超过大小限制"}, status_code=413)(
                scope, receive, send,
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await JSONResponse({"detail": "上传请求超过大小限制"}, status_code=413)(
                scope, receive, send,
            )


def create_app(backend: WebBackend, *, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="双线求职 Agent", lifespan=lifespan)
    app.add_middleware(
        _MaterialUploadBodyLimitMiddleware,
        max_bytes=backend.max_file_bytes + _MATERIAL_UPLOAD_OVERHEAD_BYTES,
    )
    app.include_router(router)
    static_root = files("nico_web").joinpath("resources", "static")
    app.mount("/static", StaticFiles(directory=str(static_root)), name="static")

    @app.get("/")
    def index():
        content = files("nico_web").joinpath("resources", "static", "index.html").read_bytes()
        return Response(content, media_type="text/html; charset=utf-8")

    @app.middleware("http")
    async def bind_account(request: Request, call_next):
        backend_token = bind_backend(backend)
        resources_token = None
        try:
            if not backend.auth_required() or request.url.path in (
                "/", "/api/config", "/api/auth/register", "/api/auth/login", "/api/auth/logout",
            ) or request.url.path.startswith("/static/"):
                return await call_next(request)
            user_id = backend.auth_store().resolve_token(request.cookies.get("nico_session"))
            if user_id is None:
                return JSONResponse({"detail": "请先登录"}, status_code=401)
            data_dir = backend.data_dir()
            store = backend.make_store(data_dir / "users" / user_id / "career_agent.sqlite3")
            runtime = backend.make_runtime(store, backend.provider())
            resources_token = bind_request_resources(store, runtime)
            return await call_next(request)
        finally:
            if resources_token is not None:
                reset_request_resources(resources_token)
            reset_backend(backend_token)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    return app
