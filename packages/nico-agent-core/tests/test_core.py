import asyncio
import unittest
from unittest.mock import patch

from nico_agent_core import AgentCore, AgentRunRequest, classify_run_error
from nico_contracts.errors import ModelError


class FakeModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return self.replies.pop(0)


class AgentCoreTests(unittest.TestCase):
    def request(self, model, messages=None, execute_tool=None):
        return AgentRunRequest(
            model=model,
            messages=messages or [{"role": "user", "content": "question"}],
            tools=[],
            is_current=lambda: True,
            execute_tool=execute_tool or (lambda _: asyncio.sleep(0, result={"role": "tool"})),
            can_execute_in_parallel=lambda _: True,
            prepare_tools=lambda: asyncio.sleep(0),
        )

    def test_no_provider_dependency_for_plain_reply(self):
        reply = FakeModel({"content": " answer ", "finish_reason": "stop"})
        result = asyncio.run(AgentCore().run(self.request(reply)))
        self.assertEqual(result, "answer")
        self.assertEqual(reply.calls, 1)

    def test_tool_cycle_uses_generic_executor_and_usage(self):
        tool_call = {"id": "call-1", "function": {"name": "lookup", "arguments": "{}"}}
        reply = FakeModel(
            {"content": None, "tool_calls": [tool_call], "finish_reason": "tool_calls",
             "usage": {"total_tokens": 3}},
            {"content": "done", "finish_reason": "stop", "usage": {"total_tokens": 2}},
        )
        executed = []

        async def execute(call):
            executed.append(call["function"]["name"])
            return {"role": "tool", "tool_call_id": call["id"], "content": "found"}

        request = self.request(reply, execute_tool=execute)
        result = asyncio.run(AgentCore().run(request))
        self.assertEqual(result, "done")
        self.assertEqual(executed, ["lookup"])
        self.assertEqual(request.trace.tool_names, ["lookup"])
        self.assertEqual(request.trace.usage.request_count, 2)

    def test_stream_events_include_text_and_tool_names_without_arguments(self):
        call = {"id": "call-1", "function": {"name": "lookup", "arguments": "{\"secret\":\"value\"}"}}
        replies = [
            {"content": None, "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "complete", "finish_reason": "stop"},
        ]

        class StreamingModel:
            async def complete(self, messages, tools):
                raise AssertionError("streaming path should be used")

            async def stream(self, messages, tools, on_delta):
                reply = replies.pop(0)
                if reply["content"]:
                    on_delta(reply["content"])
                return reply

        events = []
        request = self.request(StreamingModel())
        request.on_event = events.append
        result = asyncio.run(AgentCore().run(request))
        self.assertEqual(result, "complete")
        self.assertEqual(events, [
            {"type": "tool_start", "name": "lookup"},
            {"type": "text_delta", "text": "complete"},
        ])
        self.assertNotIn("secret", repr(events))

    def test_streaming_run_cancellation_is_recorded(self):
        started = asyncio.Event()

        class WaitingModel:
            async def complete(self, messages, tools):
                raise AssertionError("streaming path should be used")

            async def stream(self, messages, tools, on_delta):
                started.set()
                await asyncio.Event().wait()

        async def cancel_run():
            request = self.request(WaitingModel())
            request.on_event = lambda _: None
            task = asyncio.create_task(AgentCore().run(request))
            await started.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return request.trace

        trace = asyncio.run(cancel_run())
        self.assertEqual((trace.outcome, trace.error_code), ("cancelled", "cancelled"))

    def test_truncated_reply_never_executes_tools(self):
        reply = FakeModel({"content": None, "tool_calls": [{
            "id": "call-1", "function": {"name": "lookup", "arguments": "{}"},
        }], "finish_reason": "length"})
        executed = []

        async def execute(call):
            executed.append(call)
            return {"role": "tool"}

        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().run(self.request(reply, execute_tool=execute)))
        self.assertEqual(caught.exception.code, "model_output_truncated")
        self.assertEqual(executed, [])

    def test_error_classification_keeps_stable_codes(self):
        self.assertEqual(classify_run_error(ModelError("model_busy", "busy")), "model_busy")
        self.assertEqual(classify_run_error(ValueError("bad")), "model_invalid_output")
        self.assertEqual(classify_run_error(RuntimeError("failed")), "model_run_failed")

    def test_timeout_is_mapped_and_recorded(self):
        class SlowModel:
            async def complete(self, messages, tools):
                await asyncio.sleep(1)

        request = self.request(SlowModel())
        with patch("nico_agent_core.core.MAX_RUN_SECONDS", 0.01):
            with self.assertRaises(ModelError) as caught:
                asyncio.run(AgentCore().run(request))
        self.assertEqual(caught.exception.code, "run_timeout")
        self.assertEqual(request.trace.outcome, "failed")
        self.assertEqual(request.trace.error_code, "run_timeout")


if __name__ == "__main__":
    unittest.main()
