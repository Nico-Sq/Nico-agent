"""Interview command arguments and interactions."""

import argparse

from nico_application import InterviewUseCases, UseCaseError


def add_interview_commands(sub) -> None:
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


async def run_interview_action(args, interviews: InterviewUseCases) -> dict:
    if args.action == "create":
        return {"session": interviews.create(
            args.goal, tuple(args.material), tuple(args.claim), args.questions, args.model,
        )}
    if args.action == "list":
        return {"sessions": interviews.list(args.query)}

    try:
        if args.action == "start":
            return await interviews.start(args.session_id)
        if args.action == "answer":
            return await interviews.answer_current(args.session_id, args.text)
        if args.action == "skip":
            return await interviews.skip(args.session_id, args.question_number)
        if args.action == "pause":
            return {"session": interviews.pause(args.session_id)}
        if args.action == "cancel":
            return {"session": interviews.cancel(args.session_id)}
        if args.action == "resume":
            return await interviews.resume(args.session_id)
        if args.action == "report":
            return {"report": interviews.report(args.session_id)}
        if args.action == "practice":
            return await interviews.practice(args.session_id, args.question_number)
        raise ValueError("未知面试命令")
    except UseCaseError as exc:
        if exc.kind == "invalid":
            message = str(exc)
        elif exc.kind == "not_found":
            message = "面试会话不存在"
        else:
            message = {
                "start": "面试已经开始或状态不允许开始",
                "answer": ("面试尚未生成题目" if str(exc) == "面试尚未生成题目"
                           else "当前题目已变化或状态不允许回答"),
                "skip": "当前题目已变化，无法跳过",
                "pause": "当前不能暂停",
                "cancel": "当前不能结束面试",
                "resume": "当前不能恢复",
                "report": "面试不存在",
                "practice": "只能复练已完成面试中有反馈的题目",
            }.get(args.action, str(exc))
        raise ValueError(message) from exc


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
