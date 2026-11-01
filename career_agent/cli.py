"""Local text channel and commands for the same Agent runtime as the Web API."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from nico_application import ChatUseCases, MemoryUseCases, UseCaseError

from .channels import (
    ChannelDeliveryError,
    ChannelRouter,
    ConsoleChannel,
    TextChannel,
    TextMessage,
)
from .mcp import MCPServers
from .models import ModelCatalog
from .plugins import LocalPlugins
from .providers import ModelError
from .runtime import AgentRuntime
from .store import Store


def cli_error_text(exc: Exception) -> str:
    if isinstance(exc, ModelError):
        return f"{exc.code}: {exc}"
    return str(exc)


def session_banner(store: Store, session: dict) -> str:
    grants = store.tool_grants(session["id"])
    return (f"会话 {session['id']}｜状态 {session['status']}｜模型 {session['model_id']}"
            f"｜工具授权 {', '.join(grants) if grants else '无'}")


async def complete_session(session: dict, store: Store, catalog: ModelCatalog,
                           mcp: MCPServers, plugins: LocalPlugins) -> dict:
    provider = catalog.get(session.get("model_id", "default"))
    if provider.configuration_error():
        raise ValueError(provider.configuration_error())
    return await AgentRuntime(store, provider, mcp, plugins).run(session)


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
    chat = ChatUseCases(store, catalog, runtime.run, AgentRuntime.cancel_session_runs)
    if session_id is None:
        session = chat.create(model_id)
        session_id = session["id"]
    sender_id = getattr(channel, "sender_id", "local")
    conversation_id = getattr(channel, "conversation_id", "console")
    allowed_senders = set(getattr(channel, "allowed_senders", {sender_id}))
    router = ChannelRouter(
        store, getattr(channel, "name", "local"), allowed_senders, model_id
    )
    session = (router.bind(sender_id, conversation_id, session_id) if session_id
               else router.ensure_session(sender_id, conversation_id))
    banner = f"{session_banner(store, session)}，输入 /retry 重试失败轮次，/exit 结束。"
    await channel.send(
        conversation_id, banner, f"{router.channel_name}:banner:{session['id']}"
    )
    while True:
        incoming = await channel.receive()
        if incoming is None:
            return router.session_id(sender_id, conversation_id)
        if not incoming.text.strip():
            continue

        async def handle(current_session: dict, message: TextMessage) -> str:
            try:
                if message.text.strip() == "/retry":
                    result = await chat.retry_message(current_session["id"])
                else:
                    result = await chat.send_message(
                        current_session["id"], message.text.strip(),
                    )
                return result.get("message", "本轮已中断")
            except UseCaseError as exc:
                if exc.kind == "conflict":
                    return "当前没有可重试消息，或会话正在运行。"
                return str(exc)
            except (ModelError, ValueError) as exc:
                return f"运行失败：{cli_error_text(exc)}"

        try:
            await router.process(incoming, handle, channel.send)
        except ChannelDeliveryError as exc:
            print(f"渠道投递失败：message_id={exc.message_id} delivery_id={exc.delivery_id}")


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


async def run_interview_action(args, store: Store, catalog: ModelCatalog,
                               mcp: MCPServers, plugins: LocalPlugins) -> dict:
    if args.action == "create":
        goal = args.goal.strip()
        if not goal:
            raise ValueError("岗位目标不能为空")
        if len(goal) > 500 or len(args.material) > 5 or len(args.claim) > 10:
            raise ValueError("岗位目标或关联资源数量超出限制")
        provider = catalog.get(args.model)
        if provider.configuration_error():
            raise ValueError(provider.configuration_error())
        session = store.create(
            "interview", goal, tuple(args.material), tuple(args.claim),
            args.questions, args.model,
        )
        return {"session": session}
    if args.action == "list":
        return {"sessions": store.list_sessions("interview", args.query)}

    session = store.get(args.session_id, "interview")
    if session is None:
        raise ValueError("面试会话不存在")
    if args.action == "start":
        running = store.claim_run(session["id"], "interview", "new")
        if running is None:
            raise ValueError("面试已经开始或状态不允许开始")
        return await complete_session(running, store, catalog, mcp, plugins)
    if args.action == "answer":
        text = args.text.strip()
        if not text:
            raise ValueError("回答不能为空")
        if len(text) > 12000:
            raise ValueError("回答不能超过 12000 字")
        if not session["question_count"]:
            raise ValueError("面试尚未生成题目")
        running = store.claim_run(
            session["id"], "interview", "awaiting_answer", text,
            expected_question=session["question_count"],
        )
        if running is None:
            raise ValueError("当前题目已变化或状态不允许回答")
        return await complete_session(running, store, catalog, mcp, plugins)
    if args.action == "skip":
        if args.question_number < 1:
            raise ValueError("题号必须大于 0")
        running = store.skip_interview_question(session["id"], args.question_number)
        if running is None:
            raise ValueError("当前题目已变化，无法跳过")
        return await complete_session(running, store, catalog, mcp, plugins)
    if args.action == "pause":
        paused = store.interrupt(session["id"], "interview")
        if paused is None:
            raise ValueError("当前不能暂停")
        return {"session": paused}
    if args.action == "cancel":
        cancelled = store.cancel_interview(session["id"])
        if cancelled is None:
            raise ValueError("当前不能结束面试")
        return {"session": cancelled}
    if args.action == "resume":
        resumed = store.resume(session["id"])
        if resumed is None:
            raise ValueError("当前不能恢复")
        resumed_session, needs_run = resumed
        return (await complete_session(resumed_session, store, catalog, mcp, plugins)
                if needs_run else {"session": resumed_session})
    if args.action == "report":
        report = store.interview_report(session["id"])
        if report is None:
            raise ValueError("面试不存在")
        return {"report": report}
    if args.action == "practice":
        if args.question_number < 1:
            raise ValueError("题号必须大于 0")
        practice = store.create_practice(session["id"], args.question_number)
        running = store.claim_run(practice["id"], "interview", "new")
        return await complete_session(running, store, catalog, mcp, plugins)
    raise ValueError("未知面试命令")


async def run_interview(args, store: Store, catalog: ModelCatalog,
                        mcp: MCPServers, plugins: LocalPlugins) -> dict:
    try:
        return await run_interview_action(args, store, catalog, mcp, plugins)
    finally:
        await mcp.close()


def run_memory_candidate_action(args, store: Store) -> dict:
    memory = MemoryUseCases(store)
    if args.action == "list":
        return {"candidates": memory.list_candidates(args.session_id, args.status)}
    if args.action == "show":
        return {"candidate": memory.get_candidate(args.candidate_id)}
    if args.action == "create":
        return {"candidate": memory.create_candidate(
            args.session_id, args.content, args.source,
        )}
    if args.action == "edit":
        try:
            return {"candidate": memory.edit_candidate(args.candidate_id, args.content)}
        except UseCaseError as exc:
            if exc.kind in {"not_found", "conflict"}:
                raise ValueError("候选不存在或已经确认") from exc
            raise
    if args.action == "confirm":
        return {"candidate": memory.confirm_candidate(args.candidate_id)}
    if args.action == "delete":
        memory.delete_candidate(args.candidate_id)
        return {"deleted": True, "candidate_id": args.candidate_id}
    raise ValueError("未知候选记忆命令")


def format_interview_report(report: dict) -> str:
    lines = [f"面试状态：{report['status']}", f"岗位目标：{report['goal']}", ""]
    for item in report["items"]:
        lines.append(f"第 {item['number']} 题：{item['question']}")
        lines.append("已跳过" if item["skipped"] else f"回答：{item['answer'] or '无'}")
        if item["feedback"]:
            lines.append(f"反馈：{item['feedback']}")
        if item["evidence"]:
            lines.append("证据：" + "；".join(item["evidence"]))
        lines.append("")
    if report["summary"]:
        lines.extend(["总结：", report["summary"]])
    return "\n".join(lines).rstrip()


def question_count(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("题数必须是整数") from exc
    if not 1 <= number <= 10:
        raise argparse.ArgumentTypeError("题数必须在 1 到 10 之间")
    return number


def print_result(result: dict, store: Store) -> None:
    session = result.get("session")
    if session:
        print(session_banner(store, session))
    if "message" in result:
        print(result["message"])
    elif "report" in result:
        print(format_interview_report(result["report"]))
    elif "sessions" in result:
        for item in result["sessions"]:
            label = item.get("title") or item.get("goal") or "未命名"
            print(f"{item['id']}  {item['status']}  {item['model_id']}  {label}")
    elif "candidate" in result:
        print(json.dumps(result["candidate"], ensure_ascii=False))
    elif "candidates" in result:
        print(json.dumps(result["candidates"], ensure_ascii=False))
    elif "deleted" in result:
        print("已删除")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nico-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    chat = sub.add_parser("chat", help="交互式本地文字聊天")
    chat.add_argument("--model", default="default")
    chat.add_argument("--session")
    sub.add_parser("models", help="列出模型")

    memory = sub.add_parser("memory", help="管理长期记忆")
    memory.add_argument("action", choices=("list", "add", "delete"))
    memory.add_argument("value", nargs="?")

    candidate = sub.add_parser("candidate", help="查看和处理记忆候选")
    candidate_sub = candidate.add_subparsers(dest="candidate_action", required=True)
    candidate_list = candidate_sub.add_parser("list")
    candidate_list.add_argument("--session")
    candidate_list.add_argument("--status", choices=("pending", "confirmed"))
    candidate_show = candidate_sub.add_parser("show")
    candidate_show.add_argument("candidate_id")
    candidate_create = candidate_sub.add_parser("create")
    candidate_create.add_argument("session_id")
    candidate_create.add_argument("content")
    candidate_create.add_argument("--source", type=int, action="append", required=True)
    candidate_edit = candidate_sub.add_parser("edit")
    candidate_edit.add_argument("candidate_id")
    candidate_edit.add_argument("content")
    for action in ("confirm", "delete"):
        item = candidate_sub.add_parser(action)
        item.add_argument("candidate_id")
    candidate.set_defaults(action="list", session_id=None, status=None)

    interview = sub.add_parser("interview", help="本机文字模拟面试")
    interview_sub = interview.add_subparsers(dest="action", required=True)
    interview_create = interview_sub.add_parser("create")
    interview_create.add_argument("goal")
    interview_create.add_argument("--questions", type=question_count, default=3)
    interview_create.add_argument("--model", default="default")
    interview_create.add_argument("--material", action="append", default=[])
    interview_create.add_argument("--claim", action="append", default=[])
    interview_list = interview_sub.add_parser("list")
    interview_list.add_argument("--query", default="")
    for action in ("start", "pause", "cancel", "resume", "report"):
        item = interview_sub.add_parser(action)
        item.add_argument("session_id")
    interview_answer = interview_sub.add_parser("answer")
    interview_answer.add_argument("session_id")
    interview_answer.add_argument("text")
    interview_skip = interview_sub.add_parser("skip")
    interview_skip.add_argument("session_id")
    interview_skip.add_argument("question_number", type=int)
    interview_practice = interview_sub.add_parser("practice")
    interview_practice.add_argument("session_id")
    interview_practice.add_argument("question_number", type=int)

    tools = sub.add_parser("tools", help="发现工具或授权会话")
    tools.add_argument("action", choices=("list", "grant", "revoke"))
    tools.add_argument("session_id", nargs="?")
    tools.add_argument("tool_name", nargs="?")
    tools.add_argument("--kind", choices=("chat", "interview"), default="chat")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if os.getenv("AUTH_REQUIRED", "false").lower() == "true":
        parser.error("启用账户登录时请通过 Web API 管理各账户数据")
    root = Path(__file__).resolve().parent.parent
    store = Store(Path(os.getenv("DATA_DIR", root / "data")) / "career_agent.sqlite3")
    memory_use_cases = MemoryUseCases(store)
    catalog = ModelCatalog()
    mcp = MCPServers()
    plugins = LocalPlugins()
    try:
        if args.command == "models":
            for item in catalog.list():
                print(f"{item['id']}: {item['provider']} {item['model']} "
                      f"输出上限 {item['max_output_tokens'] or '—'} {item['config_error'] or ''}")
        elif args.command == "chat":
            asyncio.run(run_chat(args, store, catalog, mcp, plugins))
        elif args.command == "memory":
            if args.action == "add":
                if not args.value or not args.value.strip() or len(args.value) > 2000:
                    raise ValueError("记忆内容需为 1–2000 字")
                saved = memory_use_cases.add(args.value)
                print(saved["id"] + ("" if saved["created"] else "（已存在）"))
            elif args.action == "delete":
                if not args.value:
                    raise ValueError("记忆不存在")
                memory_use_cases.delete(args.value)
                print("已删除")
            else:
                for item in memory_use_cases.search(args.value or ""):
                    print(f"{item['id']}  {item['content']}")
        elif args.command == "candidate":
            args.action = args.candidate_action
            print_result(run_memory_candidate_action(args, store), store)
        elif args.command == "interview":
            print_result(asyncio.run(run_interview(args, store, catalog, mcp, plugins)), store)
        else:
            if args.action == "list":
                for item in plugins.discover() + asyncio.run(discover_mcp(mcp)):
                    permission = "只读" if item.get("read_only") is True else "非只读（不可授权）"
                    print(f"{item['name']} [{permission}]: {item['description']}")
                for name, error in mcp.status().items():
                    print(f"{name}: {error}")
            else:
                if not args.session_id or not store.get(args.session_id, args.kind):
                    raise ValueError(f"需要现有{args.kind}会话 ID")
                if not args.tool_name:
                    raise ValueError("需要工具名称")
                names = set(store.tool_grants(args.session_id))
                if args.action == "grant":
                    available = {item["name"]: item
                                 for item in plugins.discover() + asyncio.run(discover_mcp(mcp))}
                    if args.tool_name not in available:
                        raise ValueError("工具不存在或未启用")
                    if available[args.tool_name].get("read_only") is not True:
                        raise ValueError("非只读工具尚未开放")
                    names.add(args.tool_name)
                else:
                    names.discard(args.tool_name)
                print(store.set_tool_grants(args.session_id, sorted(names)))
    except (ModelError, ValueError) as exc:
        parser.exit(1, f"操作失败：{cli_error_text(exc)}\n")


if __name__ == "__main__":
    main()
