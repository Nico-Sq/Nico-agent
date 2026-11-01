import json
import unittest

from nico_domain import (
    can_answer_interview,
    can_cancel_interview,
    can_pause_interview,
    can_practice_interview,
    can_resume_interview,
    can_skip_interview,
    can_start_interview,
    is_current_interview_run,
    parse_interview_result,
)


class InterviewRuleTests(unittest.TestCase):
    def test_state_transitions(self):
        cases = (
            (can_start_interview, ("new",), True),
            (can_start_interview, ("paused",), False),
            (can_answer_interview, ("awaiting_answer", 1), True),
            (can_answer_interview, ("running", 1), False),
            (can_answer_interview, ("running", 1, 1), True),
            (can_answer_interview, ("running", 1, 2), False),
            (can_skip_interview, ("awaiting_answer", 2, 2), True),
            (can_skip_interview, ("awaiting_answer", 2, 1), False),
            (can_skip_interview, ("running", 2, 2), False),
            (can_pause_interview, ("awaiting_answer",), True),
            (can_resume_interview, ("paused",), True),
            (can_cancel_interview, ("finished",), False),
            (can_cancel_interview, ("running",), True),
            (can_practice_interview, ("finished", 1, True), True),
            (can_practice_interview, ("finished", 1, False), False),
            (is_current_interview_run, ("running", 4, 4), True),
            (is_current_interview_run, ("paused", 4, 4), False),
        )
        for rule, args, expected in cases:
            with self.subTest(rule=rule.__name__, args=args):
                self.assertEqual(rule(*args), expected)

    def test_first_question_and_fenced_json(self):
        result = parse_interview_result(
            {"question_count": 0, "max_questions": 1},
            '```json\n{"feedback":"","question":"请介绍项目","finished":false,"evidence":[]}\n```',
            "", False,
        )
        self.assertEqual(result.content, "第 1 题：请介绍项目")
        self.assertEqual(result.status, "awaiting_answer")
        self.assertEqual(result.question_count, 1)

    def test_final_answer_requires_exact_original_evidence(self):
        result = parse_interview_result(
            {"question_count": 1, "max_questions": 1},
            json.dumps({"feedback": "回答清楚", "question": "", "finished": True,
                        "evidence": ["延迟下降 20%"], "summary": "整体表现良好"}),
            "接口延迟下降 20%", False,
        )
        self.assertEqual(result.content, "整体表现良好")
        self.assertEqual(result.status, "finished")
        self.assertEqual(result.evidence, ("延迟下降 20%",))

    def test_skipped_question_cannot_gain_feedback_or_evidence(self):
        with self.assertRaisesRegex(ValueError, "跳过的题目"):
            parse_interview_result(
                {"question_count": 1, "max_questions": 2},
                json.dumps({"feedback": "虚构反馈", "question": "下一题", "finished": False,
                            "evidence": []}),
                "", True,
            )

    def test_fabricated_evidence_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "原文短句"):
            parse_interview_result(
                {"question_count": 1, "max_questions": 1},
                json.dumps({"feedback": "结论", "question": "", "finished": True,
                            "evidence": ["未曾回答的内容"], "summary": "总结"}),
                "我的实际回答", False,
            )


if __name__ == "__main__":
    unittest.main()
