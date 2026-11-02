import argparse
import asyncio
from contextlib import redirect_stdout
from io import StringIO
import unittest
from types import SimpleNamespace

from nico_cli import build_parser, question_count, run, run_channel
from nico_contracts.channels import ChannelResult, TextMessage


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

    def test_memory_add_command_keeps_validation_and_output(self):
        class Memory:
            def add(self, content):
                return {"id": "memory-1", "created": True}

        args = build_parser().parse_args(["memory", "add", "偏好后端"])
        output = StringIO()
        with redirect_stdout(output):
            run(args, SimpleNamespace(memory=Memory()))
        self.assertEqual(output.getvalue(), "memory-1\n")

        args = build_parser().parse_args(["memory", "add", " "])
        with self.assertRaisesRegex(ValueError, "记忆内容需为 1–2000 字"):
            run(args, SimpleNamespace(memory=Memory()))

    def test_retry_delivery_command_reuses_saved_message_without_chat_turn(self):
        class Channel:
            sender_id = "local"
            conversation_id = "console"

            def __init__(self):
                self.incoming = iter([
                    TextMessage("local", "console", "command-1", "/retry-delivery"),
                    TextMessage("local", "console", "command-2", "/retry-delivery failed-1"),
                    None,
                ])
                self.sent = []

            async def receive(self):
                return next(self.incoming)

            async def send(self, conversation_id, text, delivery_id):
                self.sent.append((conversation_id, text, delivery_id))

        class Router:
            channel_name = "console"

            def __init__(self):
                self.retry_args = None

            def ensure_session(self, sender_id, conversation_id):
                return {"id": "session-1", "status": "active", "model_id": "demo"}

            def session_id(self, sender_id, conversation_id):
                return "session-1"

            async def retry_delivery(self, message_id, conversation_id, channel_send,
                                     sender_id=None):
                self.retry_args = (message_id, conversation_id, sender_id)
                await channel_send(conversation_id, "saved response", "stable-delivery-id")
                return ChannelResult("session-1", "saved response", duplicate=True)

        class Chat:
            def validate_model(self, model_id):
                pass

            async def send_message(self, *args):
                raise AssertionError("delivery retry must not start a chat turn")

            async def retry_message(self, *args):
                raise AssertionError("delivery retry must not retry model generation")

        channel = Channel()
        router = Router()
        grants = SimpleNamespace(list=lambda session_id: [])
        output = StringIO()
        with redirect_stdout(output):
            asyncio.run(run_channel(channel, router, Chat(), grants))

        self.assertEqual(router.retry_args, ("failed-1", "console", "local"))
        self.assertIn("用法：/retry-delivery <message_id>", output.getvalue())
        self.assertEqual(channel.sent[-1], ("console", "saved response", "stable-delivery-id"))


if __name__ == "__main__":
    unittest.main()
