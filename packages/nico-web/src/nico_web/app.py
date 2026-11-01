"""FastAPI factory; service construction stays in the caller."""

from importlib.resources import files
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .backend import (
    WebBackend,
    bind_backend,
    bind_request_resources,
    reset_backend,
    reset_request_resources,
)
from .routes import router


def create_app(backend: WebBackend, *, lifespan: Any = None) -> FastAPI:
    app = FastAPI(title="双线求职 Agent", lifespan=lifespan)
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
