"""Argument parsing and command interaction over application use cases."""

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Callable

from nico_application import (
    ChannelRouter,
    ChatUseCases,
    InterviewUseCases,
    MemoryUseCases,
    ToolDiscoveryUseCases,
    ToolGrantUseCases,
    UseCaseError,
)
from nico_contracts.channels import TextChannel, TextMessage
from nico_contracts.errors import ChannelDeliveryError, ModelError

from .interview_commands import (
    add_interview_commands,
    format_interview_report,
    question_count,
    run_interview_action,
)


@dataclass(frozen=True)
class CLIServices:
    chat: ChatUseCases
    interviews: InterviewUseCases
    memory: MemoryUseCases
    tool_grants: ToolGrantUseCases
    tool_discovery: ToolDiscoveryUseCases
    channel: TextChannel
    channel_router: ChannelRouter
    list_models: Callable[[], list[dict]]


def cli_error_text(exc: Exception) -> str:
    if isinstance(exc, ModelError):
        return f"{exc.code}: {exc}"
    return str(exc)


def session_banner(session: dict, tool_grants: ToolGrantUseCases) -> str:
    grants = tool_grants.list(session["id"])
    return (f"会话 {session['id']}｜状态 {session['status']}｜模型 {session['model_id']}"
            f"｜工具授权 {', '.join(grants) if grants else '无'}")


async def run_channel(channel: TextChannel, router: ChannelRouter, chat: ChatUseCases,
                      tool_grants: ToolGrantUseCases, model_id: str = "default",
                      session_id: str | None = None) -> str:
    sender_id = getattr(channel, "sender_id", "local")
    conversation_id = getattr(channel, "conversation_id", "console")
    if session_id:
        session = router.bind(sender_id, conversation_id, session_id)
    else:
        router.model_id = model_id
        session = router.ensure_session(sender_id, conversation_id)
    chat.validate_model(session["model_id"])
    banner = f"{session_banner(session, tool_grants)}，输入 /retry 重试失败轮次，/exit 结束。"
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
                    result = await chat.send_message(current_session["id"], message.text.strip())
                return result.get("message", "本轮已中断")
            except UseCaseError as exc:
                if exc.kind == "conflict":
                    return "当前没有可重试消息，或会话正在运行。"
                return str(exc)
            except (ModelError, ValueError) as exc:
                return f"运行失败：{cli_error_text(exc)}"

        try:
            result = await router.process(incoming, handle, channel.send)
            if not result.delivered:
                print("渠道消息已认领，处理状态不确定；为避免重复运行模型，本次未自动重试。")
        except ChannelDeliveryError as exc:
            print(f"渠道投递失败：message_id={exc.message_id} delivery_id={exc.delivery_id}")


def run_memory_candidate_action(args, memory: MemoryUseCases) -> dict:
    if args.action == "list":
        return {"candidates": memory.list_candidates(args.session_id, args.status)}
    if args.action == "show":
        return {"candidate": memory.get_candidate(args.candidate_id)}
    if args.action == "create":
        return {"candidate": memory.create_candidate(args.session_id, args.content, args.source)}
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


def print_result(result: dict, tool_grants: ToolGrantUseCases) -> None:
    session = result.get("session")
    if session:
        print(session_banner(session, tool_grants))
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

    add_interview_commands(sub)

    tools = sub.add_parser("tools", help="发现工具或授权会话")
    tools.add_argument("action", choices=("list", "grant", "revoke"))
    tools.add_argument("session_id", nargs="?")
    tools.add_argument("tool_name", nargs="?")
    tools.add_argument("--kind", choices=("chat", "interview"), default="chat")
    return parser


def run(args: argparse.Namespace, services: CLIServices) -> None:
    if args.command == "models":
        for item in services.list_models():
            print(f"{item['id']}: {item['provider']} {item['model']} "
                  f"输出上限 {item['max_output_tokens'] or '—'} {item['config_error'] or ''}")
    elif args.command == "chat":
        asyncio.run(run_channel(
            services.channel, services.channel_router, services.chat,
            services.tool_grants, args.model, args.session,
        ))
    elif args.command == "memory":
        if args.action == "add":
            if not args.value or not args.value.strip() or len(args.value) > 2000:
                raise ValueError("记忆内容需为 1–2000 字")
            saved = services.memory.add(args.value)
            print(saved["id"] + ("" if saved["created"] else "（已存在）"))
        elif args.action == "delete":
            if not args.value:
                raise ValueError("记忆不存在")
            services.memory.delete(args.value)
            print("已删除")
        else:
            for item in services.memory.search(args.value or ""):
                print(f"{item['id']}  {item['content']}")
    elif args.command == "candidate":
        args.action = args.candidate_action
        print_result(run_memory_candidate_action(args, services.memory), services.tool_grants)
    elif args.command == "interview":
        print_result(
            asyncio.run(run_interview_action(args, services.interviews)), services.tool_grants,
        )
    else:
        if args.action == "list":
            available, statuses = asyncio.run(services.tool_discovery.list())
            for item in available:
                permission = "只读" if item.get("read_only") is True else "非只读（不可授权）"
                print(f"{item['name']} [{permission}]: {item['description']}")
            for name, error in statuses.items():
                print(f"{name}: {error}")
        else:
            if not args.session_id:
                raise ValueError(f"需要现有{args.kind}会话 ID")
            services.tool_grants.require_session(args.session_id, args.kind)
            if args.action == "grant":
                if not args.tool_name:
                    raise ValueError("需要工具名称")
                available, _ = asyncio.run(services.tool_discovery.list())
                names = services.tool_grants.grant(
                    args.session_id, args.tool_name, available,
                )
            else:
                if not args.tool_name:
                    raise ValueError("需要工具名称")
                names = services.tool_grants.revoke(args.session_id, args.tool_name)
            print(names)
