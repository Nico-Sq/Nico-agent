import asyncio
import unittest
from threading import Event, Thread
from unittest.mock import patch

from nico_agent_core import AgentCore, AgentRunRequest, RunSuperseded, classify_run_error
import nico_agent_core.core as agent_core_module
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

    def test_superseded_run_skips_tool_preparation(self):
        request = self.request(FakeModel())
        prepared = []

        async def prepare_tools():
            prepared.append(True)

        request.is_current = lambda: False
        request.prepare_tools = prepare_tools

        with self.assertRaises(RunSuperseded):
            asyncio.run(AgentCore().run(request))

        self.assertEqual(prepared, [])

    def test_session_run_registry_allows_cross_thread_finish_during_cancel(self):
        values_started = Event()
        continue_values = Event()
        finish_started = Event()
        finish_completed = Event()
        errors = []

        class GatedRuns(dict):
            def values(self):
                iterator = iter(super().values())
                first = next(iterator)
                values_started.set()
                if not continue_values.wait(1):
                    raise TimeoutError("cancel snapshot was not released")
                yield first
                yield from iterator

        class ClosedLoop:
            @staticmethod
            def is_closed():
                return True

        class FinishedTask:
            @staticmethod
            def done():
                return False

            @staticmethod
            def get_loop():
                return ClosedLoop()

        first_task, second_task = FinishedTask(), FinishedTask()
        active_runs = GatedRuns({1: first_task, 2: second_task})

        def capture_error(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)

        def finish():
            finish_started.set()
            AgentCore.finish_session_run("session", 2, second_task)
            finish_completed.set()

        cancel_thread = Thread(target=lambda: capture_error(
            lambda: AgentCore.cancel_session_runs("session"),
        ))
        finish_thread = Thread(target=finish)
        finish_thread_started = False
        with patch.object(agent_core_module, "_ACTIVE_RUNS", {"session": active_runs}):
            cancel_thread.start()
            try:
                self.assertTrue(values_started.wait(1), repr(errors))
                finish_thread.start()
                finish_thread_started = True
                self.assertTrue(finish_started.wait(1))
                self.assertFalse(finish_completed.wait(0.05))
            finally:
                continue_values.set()
                cancel_thread.join(1)
                if finish_thread_started:
                    finish_thread.join(1)

        self.assertFalse(cancel_thread.is_alive())
        self.assertFalse(finish_thread.is_alive())
        self.assertTrue(finish_completed.is_set())
        self.assertEqual(errors, [])

    def test_session_cancel_can_preserve_the_newer_run_version(self):
        async def scenario():
            release = asyncio.Event()

            async def wait_for_release():
                await release.wait()

            older = asyncio.create_task(wait_for_release())
            newer = asyncio.create_task(wait_for_release())
            AgentCore.track_session_run("version-boundary", 1, older)
            AgentCore.track_session_run("version-boundary", 2, newer)
            try:
                self.assertEqual(
                    AgentCore.cancel_session_runs("version-boundary", before_version=2), 1,
                )
                await asyncio.sleep(0)
                self.assertTrue(older.cancelled())
                self.assertFalse(newer.cancelled())
                try:
                    await older
                except asyncio.CancelledError:
                    pass
            finally:
                newer.cancel()
                try:
                    await newer
                except asyncio.CancelledError:
                    pass
                AgentCore.finish_session_run("version-boundary", 1, older)
                AgentCore.finish_session_run("version-boundary", 2, newer)

        asyncio.run(scenario())

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
