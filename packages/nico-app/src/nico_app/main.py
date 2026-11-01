"""Default application composition for the Web API and compatibility entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress

from nico_adapters.models.catalog import ModelCatalog
from nico_adapters.models.provider import Provider
from nico_adapters.extract import MAX_FILE_BYTES, extract_text
from nico_adapters.registry import load_tool_adapters
from nico_storage import AuthStore, Store
from nico_storage.auth import LoginRateLimited
from nico_storage.backup import backup_all
from nico_web import create_app
from nico_web.backend import WebBackend, set_default_backend

from .config import data_dir as configured_data_dir
from .runtime import AgentRuntime
from nico_web.routes import *


DATA_DIR = configured_data_dir()
AUTH_REQUIRED = os.getenv("AUTH_REQUIRED", "false").lower() == "true"
auth_store = AuthStore(DATA_DIR / "auth.sqlite3") if AUTH_REQUIRED else None
store = Store(DATA_DIR / "career_agent.sqlite3")
provider = Provider()
model_catalog = ModelCatalog()
_TOOL_ADAPTER_TYPES = load_tool_adapters(("stdio_mcp", "trusted_local_plugins"))
MCPServers = _TOOL_ADAPTER_TYPES["stdio_mcp"]
LocalPlugins = _TOOL_ADAPTER_TYPES["trusted_local_plugins"]
mcp_servers = MCPServers()
local_plugins = LocalPlugins()
runtime = AgentRuntime(store, provider, mcp_servers, local_plugins)


def make_runtime(target_store, selected_provider):
    return AgentRuntime(target_store, selected_provider, mcp_servers, local_plugins)


backend = WebBackend(
    data_dir=lambda: DATA_DIR,
    auth_required=lambda: AUTH_REQUIRED,
    auth_store=lambda: auth_store,
    store=lambda: store,
    runtime=lambda: runtime,
    provider=lambda: provider,
    model_catalog=lambda: model_catalog,
    mcp_servers=lambda: mcp_servers,
    local_plugins=lambda: local_plugins,
    make_store=Store,
    make_runtime=make_runtime,
    cancel_session_runs=AgentRuntime.cancel_session_runs,
    max_file_bytes=MAX_FILE_BYTES,
    extract_text=extract_text,
    login_rate_limited_error=LoginRateLimited,
)


@asynccontextmanager
async def lifespan(_app):
    local_plugins.activate()

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
        with suppress(Exception):
            await mcp_servers.close()
        with suppress(Exception):
            await local_plugins.close()
        await backup_once()


set_default_backend(backend)
app = create_app(backend, lifespan=lifespan)
