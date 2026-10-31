"""Local text channel and commands for the same Agent runtime as the Web API."""

import argparse
import asyncio
import os
from pathlib import Path
from typing import Protocol

from .mcp import MCPServers
from .models import ModelCatalog
from .plugins import LocalPlugins
from .providers import ModelError
from .runtime import AgentRuntime
from .store import Store


class TextChannel(Protocol):
    async def receive(self) -> str | None: ...
    async def send(self, text: str) -> None: ...


class ConsoleChannel:
    async def receive(self) -> str | None:
        try:
            value = await asyncio.to_thread(input, "你> ")
        except (EOFError, KeyboardInterrupt):
            return None
        return None if value.strip() in ("/exit", "/quit") else value

    async def send(self, text: str) -> None:
        print(text)


async def run_channel(channel: TextChannel, store: Store, model_id: str = "default",
                      session_id: str | None = None, catalog: ModelCatalog | None = None,
                      mcp: MCPServers | None = None, plugins: LocalPlugins | None = None) -> str:
    catalog = catalog or ModelCatalog()
    if session_id:
        session = store.get(session_id, "chat")
        if not session:
            raise ValueError("聊天会话不存在")
        model_id = session["model_id"]
    else:
        catalog.get(model_id)
        session = store.create("chat", model_id=model_id)
        session_id = session["id"]
    provider = catalog.get(model_id)
    if provider.configuration_error():
        raise ValueError(provider.configuration_error())
    runtime = AgentRuntime(store, provider, mcp, plugins)
    await channel.send(f"会话 {session_id}，输入 /exit 结束。")
    while True:
        message = await channel.receive()
        if message is None:
            return session_id
        if not message.strip():
            continue
        running = store.claim_run(session_id, "chat", "active", message.strip())
        if running is None:
            await channel.send("当前会话正在运行，请稍后重试。")
            continue
        try:
            result = await runtime.run(running)
            await channel.send(result.get("message", "本轮已中断"))
        except (ModelError, ValueError) as exc:
            await channel.send(f"运行失败：{exc}")


async def run_chat(args, store: Store, catalog: ModelCatalog, mcp: MCPServers,
                    plugins: LocalPlugins) -> None:
    try:
        await run_channel(ConsoleChannel(), store, args.model, args.session, catalog, mcp, plugins)
    finally:
        await mcp.close()


async def discover_mcp(mcp: MCPServers) -> list[dict]:
    """Discover MCP tools, then drop the server processes the CLI no longer needs."""
    try:
        return await mcp.discover()
    finally:
        await mcp.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="nico-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="交互式本地文字聊天")
    chat.add_argument("--model", default="default")
    chat.add_argument("--session")
    sub.add_parser("models", help="列出模型")
    memory = sub.add_parser("memory", help="管理长期记忆")
    memory.add_argument("action", choices=("list", "add", "delete"))
    memory.add_argument("value", nargs="?")
    tools = sub.add_parser("tools", help="发现工具或授权会话")
    tools.add_argument("action", choices=("list", "grant", "revoke"))
    tools.add_argument("session_id", nargs="?")
    tools.add_argument("tool_name", nargs="?")
    args = parser.parse_args()
    if os.getenv("AUTH_REQUIRED", "false").lower() == "true":
        parser.error("启用账户登录时请通过 Web API 管理各账户数据")
    root = Path(__file__).resolve().parent.parent
    store = Store(Path(os.getenv("DATA_DIR", root / "data")) / "career_agent.sqlite3")
    catalog = ModelCatalog()
    mcp = MCPServers()
    plugins = LocalPlugins()
    if args.command == "models":
        for item in catalog.list():
            print(f"{item['id']}: {item['provider']} {item['model']} "
                  f"输出上限 {item['max_output_tokens'] or '—'} {item['config_error'] or ''}")
    elif args.command == "chat":
        asyncio.run(run_chat(args, store, catalog, mcp, plugins))
    elif args.command == "memory":
        if args.action == "add":
            if not args.value or not args.value.strip() or len(args.value) > 2000:
                parser.error("记忆内容需为 1–2000 字")
            print(store.add_memory(args.value.strip())["id"])
        elif args.action == "delete":
            if not args.value or not store.delete_memory(args.value):
                parser.error("记忆不存在")
        else:
            for item in store.search_memories(args.value or ""):
                print(f"{item['id']}  {item['content']}")
    else:
        if args.action == "list":
            for item in plugins.discover() + asyncio.run(discover_mcp(mcp)):
                print(f"{item['name']}: {item['description']}")
            for name, error in mcp.status().items():
                print(f"{name}: {error}")
        else:
            if not args.session_id or not store.get(args.session_id, "chat"):
                parser.error("需要现有聊天会话 ID")
            names = set(store.tool_grants(args.session_id))
            if args.action == "grant":
                available = {item["name"] for item in plugins.discover() + asyncio.run(discover_mcp(mcp))}
                if args.tool_name not in available:
                    parser.error("工具不存在或未启用")
                names.add(args.tool_name)
            else:
                names.discard(args.tool_name)
            print(store.set_tool_grants(args.session_id, sorted(names)))


if __name__ == "__main__":
    main()
