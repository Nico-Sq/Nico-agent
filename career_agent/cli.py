"""Compatibility entry point and composition root for the local CLI."""

import asyncio
import os
from pathlib import Path

from nico_adapters.registry import load_channels, load_tool_adapters
from nico_application import (
    ChatUseCases,
    InterviewUseCases,
    MemoryUseCases,
    ToolDiscoveryUseCases,
    ToolGrantUseCases,
)
from nico_cli import (
    CLIServices,
    build_parser,
    cli_error_text,
    format_interview_report,
    print_result as _print_result,
    question_count,
    run as run_cli,
    run_channel as _run_channel,
    run_interview_action as _run_interview_action,
    run_memory_candidate_action as _run_memory_candidate_action,
    session_banner as _session_banner,
)

from .models import ModelCatalog
from .providers import ModelError
from .runtime import AgentRuntime
from .store import Store


def session_banner(store: Store, session: dict) -> str:
    return _session_banner(session, ToolGrantUseCases(store))


async def complete_session(session: dict, store: Store, catalog: ModelCatalog,
                           mcp, plugins, result_handler=None) -> dict:
    provider = catalog.get(session.get("model_id", "default"))
    error = provider.configuration_error()
    if error:
        raise ValueError(error)
    return await AgentRuntime(store, provider, mcp, plugins).run(
        session, result_handler=result_handler,
    )


async def run_channel(channel, store: Store, model_id: str = "default",
                      session_id: str | None = None, catalog: ModelCatalog | None = None,
                      mcp=None, plugins=None) -> str:
    catalog = catalog or ModelCatalog()
    adapter_types = load_tool_adapters(("stdio_mcp", "trusted_local_plugins"))
    mcp = mcp or adapter_types["stdio_mcp"]()
    plugins = plugins or adapter_types["trusted_local_plugins"]()

    async def run_chat_session(session: dict) -> dict:
        return await complete_session(session, store, catalog, mcp, plugins)

    chat = ChatUseCases(store, catalog, run_chat_session, AgentRuntime.cancel_session_runs)
    grants = ToolGrantUseCases(store)
    router = _make_router(channel, store, chat, model_id)
    return await _run_channel(channel, router, chat, grants, model_id, session_id)


async def run_chat(args, store: Store, catalog: ModelCatalog, mcp, plugins) -> None:
    try:
        await run_channel(
            load_channels(("console",))["console"](),
            store, args.model, args.session, catalog, mcp, plugins,
        )
    finally:
        await mcp.close()


async def discover_mcp(mcp) -> list[dict]:
    try:
        return await mcp.discover()
    finally:
        await mcp.close()


def _make_router(channel, store: Store, chat: ChatUseCases, model_id: str):
    from .channels import ChannelRouter

    sender_id = getattr(channel, "sender_id", "local")
    return ChannelRouter(
        store,
        getattr(channel, "name", "local"),
        set(getattr(channel, "allowed_senders", {sender_id})),
        model_id,
        chat.create,
    )


async def run_interview_action(args, store: Store, catalog: ModelCatalog, mcp, plugins) -> dict:
    interviews = InterviewUseCases(
        store, catalog,
        lambda session, result_handler: complete_session(
            session, store, catalog, mcp, plugins, result_handler,
        ),
        AgentRuntime.cancel_session_runs,
    )
    return await _run_interview_action(args, interviews)


async def run_interview(args, store: Store, catalog: ModelCatalog, mcp, plugins) -> dict:
    try:
        return await run_interview_action(args, store, catalog, mcp, plugins)
    finally:
        await mcp.close()


def run_memory_candidate_action(args, store: Store) -> dict:
    return _run_memory_candidate_action(args, MemoryUseCases(store))


def print_result(result: dict, store: Store) -> None:
    _print_result(result, ToolGrantUseCases(store))


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if os.getenv("AUTH_REQUIRED", "false").lower() == "true":
        parser.error("启用账户登录时请通过 Web API 管理各账户数据")

    root = Path(__file__).resolve().parent.parent
    store = Store(Path(os.getenv("DATA_DIR", root / "data")) / "career_agent.sqlite3")
    catalog = ModelCatalog()
    adapter_types = load_tool_adapters(("stdio_mcp", "trusted_local_plugins"))
    mcp = adapter_types["stdio_mcp"]()
    plugins = adapter_types["trusted_local_plugins"]()
    channel_type = load_channels(("console",))["console"]
    channel = channel_type()

    async def run_chat_session(session: dict) -> dict:
        return await complete_session(session, store, catalog, mcp, plugins)

    async def run_interview_session(session: dict, result_handler) -> dict:
        return await complete_session(
            session, store, catalog, mcp, plugins, result_handler,
        )

    chat = ChatUseCases(store, catalog, run_chat_session, AgentRuntime.cancel_session_runs)
    interviews = InterviewUseCases(
        store, catalog, run_interview_session, AgentRuntime.cancel_session_runs,
    )
    grants = ToolGrantUseCases(store)
    services = CLIServices(
        chat=chat,
        interviews=interviews,
        memory=MemoryUseCases(store),
        tool_grants=grants,
        tool_discovery=ToolDiscoveryUseCases((plugins, mcp)),
        channel=channel,
        channel_router=_make_router(channel, store, chat, args.model),
        list_models=catalog.list,
    )
    try:
        run_cli(args, services)
    except (ModelError, ValueError) as exc:
        parser.exit(1, f"操作失败：{cli_error_text(exc)}\n")
    finally:
        asyncio.run(mcp.close())
        asyncio.run(plugins.close())


if __name__ == "__main__":
    main()
