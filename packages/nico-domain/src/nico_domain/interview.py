"""Interview state and model-result rules without storage or delivery dependencies."""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class InterviewResult:
    content: str
    status: str
    question_count: int
    question: str | None
    feedback: str | None
    evidence: tuple[str, ...]


def can_start_interview(status: str) -> bool:
    return status == "new"


def can_answer_interview(status: str, question_count: int, expected_question: int | None = None) -> bool:
    if question_count < 1:
        return False
    if expected_question is None:
        return status == "awaiting_answer"
    return status in {"awaiting_answer", "running"} and expected_question == question_count


def can_skip_interview(status: str, question_count: int, question_number: int) -> bool:
    return status == "awaiting_answer" and question_count > 0 and question_number == question_count


def can_pause_interview(status: str) -> bool:
    return status in {"running", "awaiting_answer"}


def can_resume_interview(status: str) -> bool:
    return status == "paused"


def can_cancel_interview(status: str) -> bool:
    return status in {"new", "running", "awaiting_answer", "paused"}


def can_practice_interview(status: str, question_number: int, has_feedback: bool) -> bool:
    return status in {"finished", "cancelled"} and question_number > 0 and has_feedback


def is_current_interview_run(status: str, current_version: int, expected_version: int) -> bool:
    return status == "running" and current_version == expected_version


def parse_interview_result(
    session: dict,
    raw: str,
    latest_answer: str,
    skipped: bool,
) -> InterviewResult:
    payload = raw.strip()
    if payload.startswith("```") and payload.endswith("```"):
        language, separator, fenced = payload.partition("\n")
        if separator and language.lower() in ("```", "```json"):
            payload = fenced[:-3].strip()
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("面试模型必须返回 JSON 对象")
    if not {"feedback", "question", "finished", "evidence"}.issubset(data):
        raise ValueError("面试模型缺少必要字段")
    feedback = data["feedback"]
    question = data["question"]
    evidence = data["evidence"]
    summary = data.get("summary", "")
    if (not isinstance(feedback, str) or not isinstance(question, str)
            or not isinstance(data["finished"], bool) or not isinstance(evidence, list)
            or not isinstance(summary, str)):
        raise ValueError("面试模型输出字段格式错误")
    if (len(question) > 1000 or len(feedback) > 4000 or len(summary) > 4000
            or len(evidence) > 5):
        raise ValueError("面试模型输出超过长度上限")

    old_count = session["question_count"]
    max_questions = session["max_questions"]
    if data["finished"] != (old_count >= max_questions):
        raise ValueError("面试模型完成状态与题数不符")
    if not old_count and (feedback.strip() or summary.strip()):
        raise ValueError("首题不能包含反馈或总结")
    if old_count < max_questions and summary.strip():
        raise ValueError("未结束时不能生成全场总结")
    if old_count >= max_questions and question.strip():
        raise ValueError("结束时不能生成下一题")
    if skipped and (feedback.strip() or evidence):
        raise ValueError("跳过的题目不能生成反馈或回答证据")
    if old_count and not skipped and not feedback.strip():
        raise ValueError("面试模型未返回反馈")
    if old_count and not skipped:
        if not evidence or any(
            not isinstance(quote, str) or not quote.strip() or len(quote) > 200 or quote not in latest_answer
            for quote in evidence
        ):
            raise ValueError("反馈证据必须是本题原始回答中的原文短句")
    elif evidence:
        raise ValueError("首题不能引用尚未提交的回答")
    if old_count < max_questions and not question.strip():
        raise ValueError("面试模型未返回下一题")
    if old_count >= max_questions and not summary.strip():
        raise ValueError("面试模型未返回总结")

    if old_count >= max_questions:
        return InterviewResult(
            summary.strip(), "finished", old_count, None, feedback.strip() or None,
            tuple(dict.fromkeys(evidence)),
        )
    content = (f"反馈：{feedback.strip()}\n\n" if feedback.strip() else "") + (
        f"第 {old_count + 1} 题：{question.strip()}"
    )
    return InterviewResult(
        content, "awaiting_answer", old_count + 1, question.strip(),
        feedback.strip() or None, tuple(dict.fromkeys(evidence)),
    )
