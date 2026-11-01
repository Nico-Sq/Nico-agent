"""Framework-neutral callbacks supplied by the application composition root."""

from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class WebBackend:
    data_dir: Callable[[], Path]
    auth_required: Callable[[], bool]
    auth_store: Callable[[], Any]
    store: Callable[[], Any]
    runtime: Callable[[], Any]
    provider: Callable[[], Any]
    model_catalog: Callable[[], Any]
    mcp_servers: Callable[[], Any]
    local_plugins: Callable[[], Any]
    make_store: Callable[[Path], Any]
    make_runtime: Callable[[Any, Any], Any]
    cancel_session_runs: Callable[[str], int]
    max_file_bytes: int
    extract_text: Callable[[str, bytes], str]
    login_rate_limited_error: type[Exception]


_backend: ContextVar[WebBackend | None] = ContextVar("nico_web_backend", default=None)
_default_backend: WebBackend | None = None
_request_resources: ContextVar[tuple[Any, Any] | None] = ContextVar(
    "nico_web_request_resources", default=None,
)


def set_default_backend(backend: WebBackend) -> None:
    global _default_backend
    _default_backend = backend


def bind_backend(backend: WebBackend):
    return _backend.set(backend)


def reset_backend(token) -> None:
    _backend.reset(token)


def bind_request_resources(store: Any, runtime: Any):
    return _request_resources.set((store, runtime))


def reset_request_resources(token) -> None:
    _request_resources.reset(token)


def active_backend() -> WebBackend:
    backend = _backend.get() or _default_backend
    if backend is None:
        raise RuntimeError("nico-web has no application backend")
    return backend


def active_store() -> Any:
    resources = _request_resources.get()
    return resources[0] if resources else active_backend().store()


def active_runtime() -> Any:
    resources = _request_resources.get()
    return resources[1] if resources else active_backend().runtime()
