"""Long-term memory and memory-candidate CLI commands."""

from nico_application import MemoryUseCases, UseCaseError


def add_memory_commands(sub) -> None:
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


def run_memory_action(args, memory: MemoryUseCases) -> None:
    if args.action == "add":
        if not args.value or not args.value.strip() or len(args.value) > 2000:
            raise ValueError("记忆内容需为 1–2000 字")
        saved = memory.add(args.value)
        print(saved["id"] + ("" if saved["created"] else "（已存在）"))
    elif args.action == "delete":
        if not args.value:
            raise ValueError("记忆不存在")
        memory.delete(args.value)
        print("已删除")
    else:
        for item in memory.search(args.value or ""):
            print(f"{item['id']}  {item['content']}")


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
