import argparse
import unittest
from types import SimpleNamespace

from nico_cli import build_parser, question_count, run


class CLITests(unittest.TestCase):
    def test_commands_and_interview_options_remain_stable(self):
        parser = build_parser()
        subparsers = next(
            action for action in parser._actions
            if getattr(action, "dest", None) == "command"
        )
        self.assertEqual(
            set(subparsers.choices),
            {"chat", "models", "memory", "candidate", "interview", "tools"},
        )
        parsed = parser.parse_args(
            ["interview", "create", "后端开发", "--questions", "2", "--material", "resume"],
        )
        self.assertEqual(
            (parsed.command, parsed.action, parsed.goal, parsed.questions, parsed.material),
            ("interview", "create", "后端开发", 2, ["resume"]),
        )

    def test_question_count_keeps_bounds(self):
        self.assertEqual(question_count("10"), 10)
        for value in ("0", "11", "many"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                question_count(value)

    def test_tool_commands_keep_missing_session_error(self):
        class ToolGrants:
            def require_session(self, session_id, kind):
                raise ValueError(f"需要现有{kind}会话 ID")

        args = build_parser().parse_args(["tools", "grant", "missing", "read"])
        with self.assertRaisesRegex(ValueError, "需要现有chat会话 ID"):
            run(args, SimpleNamespace(tool_grants=ToolGrants()))


if __name__ == "__main__":
    unittest.main()
