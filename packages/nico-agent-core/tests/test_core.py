import asyncio
import copy
import unittest
from threading import Event, Thread
from unittest.mock import patch

import nico_agent_core
from nico_agent_core import AgentCore, AgentRunRequest, RunSuperseded, classify_run_error
from nico_contracts.errors import ModelError


class FakeModel:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    async def complete(self, messages, tools):
        self.calls += 1
        return self.replies.pop(0)


class EmptyTools:
    definitions = ()

    async def execute(self, name, args):
        raise AssertionError("no tool was offered")

    def can_execute_in_parallel(self, name):
        return True


class WaitingModel:
    def __init__(self):
        self.started = asyncio.Event()

    async def complete(self, messages, tools):
        self.started.set()
        await asyncio.Event().wait()


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
        engine = AgentCore()
        first_key = nico_agent_core.RunKey("chat", "session", 1)
        second_key = nico_agent_core.RunKey("chat", "session", 2)

        class GatedRuns(dict):
            def items(self):
                iterator = iter(super().items())
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
        active_runs = GatedRuns({first_key: first_task, second_key: second_task})

        def capture_error(action):
            try:
                action()
            except Exception as exc:
                errors.append(exc)

        def finish():
            finish_started.set()
            engine.finish_session_run(second_key, second_task)
            finish_completed.set()

        cancel_thread = Thread(target=lambda: capture_error(
            lambda: engine.cancel_session_runs("session"),
        ))
        finish_thread = Thread(target=finish)
        finish_thread_started = False
        with patch.object(engine, "_active_runs", active_runs):
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
            engine = AgentCore()
            release = asyncio.Event()

            async def wait_for_release():
                await release.wait()

            older = asyncio.create_task(wait_for_release())
            newer = asyncio.create_task(wait_for_release())
            older_key = nico_agent_core.RunKey("chat", "version-boundary", 1)
            newer_key = nico_agent_core.RunKey("chat", "version-boundary", 2)
            engine.track_session_run(older_key, older)
            engine.track_session_run(newer_key, newer)
            try:
                self.assertEqual(
                    engine.cancel_session_runs("version-boundary", before_version=2), 1,
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
                engine.finish_session_run(older_key, older)
                engine.finish_session_run(newer_key, newer)

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

    def test_invalid_tool_arguments_never_reach_executor(self):
        for arguments in ('{"query":', '[1, 2]', 'null'):
            with self.subTest(arguments=arguments):
                reply = FakeModel({"content": None, "tool_calls": [{
                    "id": "call-1", "function": {"name": "lookup", "arguments": arguments},
                }], "finish_reason": "tool_calls"})
                executed = []

                async def execute(call):
                    executed.append(call)
                    return {"role": "tool"}

                with self.assertRaises(ValueError):
                    asyncio.run(AgentCore().run(self.request(reply, execute_tool=execute)))
                self.assertEqual(executed, [])

    def test_unrecognized_finish_reason_never_executes_tools(self):
        reply = FakeModel({"content": None, "tool_calls": [{
            "id": "call-1", "function": {"name": "lookup", "arguments": "{}"},
        }], "finish_reason": "content_filter"})
        executed = []

        async def execute(call):
            executed.append(call)
            return {"role": "tool"}

        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().run(self.request(reply, execute_tool=execute)))
        self.assertEqual(caught.exception.code, "model_incomplete")
        self.assertEqual(executed, [])

    def test_complete_only_model_emits_one_delta_when_observed(self):
        model = FakeModel({"content": "ready", "finish_reason": "stop"})
        events = []
        request = self.request(model)
        request.on_event = events.append
        self.assertEqual(asyncio.run(AgentCore().run(request)), "ready")
        self.assertEqual(events, [{"type": "text_delta", "text": "ready"}])

    def test_error_classification_keeps_stable_codes(self):
        self.assertEqual(classify_run_error(ModelError("model_busy", "busy")), "model_busy")
        self.assertEqual(classify_run_error(ValueError("bad")), "model_invalid_output")
        self.assertEqual(classify_run_error(RuntimeError("failed")), "model_run_failed")

    def test_timeout_is_mapped_and_recorded(self):
        class SlowModel:
            async def complete(self, messages, tools):
                await asyncio.sleep(1)

        request = self.request(SlowModel())
        request.policy = nico_agent_core.RunPolicy(timeout_seconds=0.01)
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().run(request))
        self.assertEqual(caught.exception.code, "run_timeout")
        self.assertEqual(request.trace.outcome, "failed")
        self.assertEqual(request.trace.error_code, "run_timeout")


class PreparedRunContractTests(unittest.TestCase):
    def test_policy_rejects_invalid_limits_at_construction(self):
        for changes in ({"max_turns": 0}, {"max_tool_calls": True},
                        {"timeout_seconds": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                nico_agent_core.RunPolicy(**changes)

    def test_namespaces_and_attempts_keep_distinct_run_identity(self):
        keys = (
            nico_agent_core.RunKey("chat", "same", 1, 0),
            nico_agent_core.RunKey("interview", "same", 1, 0),
            nico_agent_core.RunKey("chat", "same", 1, 1),
        )

        async def scenario():
            return await asyncio.gather(*(AgentCore().execute(
                nico_agent_core.PreparedRun(
                    key=key, model=FakeModel({"content": str(index), "finish_reason": "stop"}),
                    messages=({"role": "user", "content": "start"},),
                    tools=EmptyTools(), is_current=lambda: True,
                )
            ) for index, key in enumerate(keys)))

        outcomes = asyncio.run(scenario())
        self.assertEqual([outcome.content for outcome in outcomes], ["0", "1", "2"])
        self.assertEqual([getattr(outcome.trace, "key", None) for outcome in outcomes], list(keys))

    def test_independent_host_can_run_with_neutral_contract(self):
        self.assertTrue(all(hasattr(nico_agent_core, name) for name in (
            "RunKey", "RunPolicy", "PreparedRun", "RunOutcome", "ToolSnapshot",
        )))

        prepared = nico_agent_core.PreparedRun(
            key=nico_agent_core.RunKey("example", "session", 1),
            model=FakeModel({"content": "ready", "finish_reason": "stop"}),
            messages=({"role": "user", "content": "start"},),
            tools=EmptyTools(),
            policy=nico_agent_core.RunPolicy(),
            is_current=lambda: True,
        )
        outcome = asyncio.run(AgentCore().execute(prepared))
        self.assertEqual(outcome.content, "ready")
        self.assertEqual(outcome.trace.usage.request_count, 1)

    def test_legacy_run_does_not_change_host_messages(self):
        call = {"id": "lookup-1", "function": {"name": "lookup", "arguments": "{}"}}
        model = FakeModel(
            {"content": None, "tool_calls": [call], "finish_reason": "tool_calls"},
            {"content": "done", "finish_reason": "stop"},
        )
        request = AgentCoreTests().request(model)
        original = copy.deepcopy(request.messages)
        self.assertEqual(asyncio.run(AgentCore().run(request)), "done")
        self.assertEqual(request.messages, original)

    def test_invalid_initial_message_stops_before_model(self):
        model = FakeModel({"content": "should not run"})
        request = AgentCoreTests().request(model, messages=[
            {"role": "admin", "content": "invalid role"},
        ])
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().run(request))
        self.assertEqual(caught.exception.code, "model_invalid_input")
        self.assertEqual(model.calls, 0)

    def test_missing_user_content_stops_before_model(self):
        model = FakeModel({"content": "should not run"})
        request = AgentCoreTests().request(model, messages=[{"role": "user"}])
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().run(request))
        self.assertEqual(caught.exception.code, "model_invalid_input")
        self.assertEqual(model.calls, 0)

    def test_initial_input_budget_stops_before_model(self):
        model = FakeModel({"content": "should not run"})
        request = AgentCoreTests().request(model, messages=[
            {"role": "user", "content": "too long"},
        ])
        request.policy = nico_agent_core.RunPolicy(max_input_chars=5)
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().run(request))
        self.assertEqual(caught.exception.code, "context_budget_exceeded")
        self.assertEqual(model.calls, 0)

    def test_custom_token_policy_stops_tool_but_default_allows_it(self):
        call = {"id": "lookup-1", "function": {"name": "lookup", "arguments": "{}"}}

        class LookupTools:
            definitions = ({"type": "function", "function": {
                "name": "lookup", "description": "Read one value",
                "parameters": {"type": "object", "properties": {}},
            }},)

            def __init__(self):
                self.calls = 0

            async def execute(self, name, args):
                self.calls += 1
                return {"value": "found"}

            def can_execute_in_parallel(self, name):
                return True

        def prepared(limit, reported_tokens=3):
            tools = LookupTools()
            model = FakeModel(
                {"content": None, "tool_calls": [call], "finish_reason": "tool_calls",
                 "usage": {"total_tokens": reported_tokens}},
                {"content": "done", "finish_reason": "stop"},
            )
            return nico_agent_core.PreparedRun(
                key=nico_agent_core.RunKey("example", "session", 1),
                model=model, messages=({"role": "user", "content": "start"},),
                tools=tools, policy=nico_agent_core.RunPolicy(max_reported_tokens=limit),
                is_current=lambda: True,
            ), tools

        limited, limited_tools = prepared(3)
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().execute(limited))
        self.assertEqual(caught.exception.code, "run_token_limit")
        self.assertEqual(limited_tools.calls, 0)

        normal, normal_tools = prepared(16_000)
        self.assertEqual(asyncio.run(AgentCore().execute(normal)).content, "done")
        self.assertEqual(normal_tools.calls, 1)

        default_limited, default_tools = prepared(16_000, 16_000)
        with self.assertRaises(ModelError) as caught:
            asyncio.run(AgentCore().execute(default_limited))
        self.assertEqual(caught.exception.code, "run_token_limit")
        self.assertEqual(default_tools.calls, 0)


class EngineCancellationTests(unittest.TestCase):
    def test_duplicate_attempt_cannot_replace_active_registration(self):
        async def scenario():
            engine = AgentCore()
            key = nico_agent_core.RunKey("example", "duplicate", 1)
            waiting = WaitingModel()
            first = asyncio.create_task(engine.execute(nico_agent_core.PreparedRun(
                key=key, model=waiting,
                messages=({"role": "user", "content": "wait"},),
                tools=EmptyTools(), is_current=lambda: True,
            )))
            try:
                await waiting.started.wait()
                second_model = FakeModel({"content": "late"})
                with self.assertRaises(ModelError) as caught:
                    await engine.execute(nico_agent_core.PreparedRun(
                        key=key, model=second_model,
                        messages=({"role": "user", "content": "again"},),
                        tools=EmptyTools(), is_current=lambda: True,
                    ))
                self.assertEqual(caught.exception.code, "run_already_active")
                self.assertEqual(second_model.calls, 0)
                self.assertEqual(engine.cancel_session_runs("duplicate"), 1)
            finally:
                first.cancel()
                await asyncio.gather(first, return_exceptions=True)

        asyncio.run(scenario())

    def test_namespaced_cancel_keeps_other_host_running(self):
        async def scenario():
            engine = AgentCore()
            chat, interview = WaitingModel(), WaitingModel()
            tasks = [asyncio.create_task(engine.execute(nico_agent_core.PreparedRun(
                key=nico_agent_core.RunKey(namespace, "shared", 1),
                model=model, messages=({"role": "user", "content": "wait"},),
                tools=EmptyTools(), is_current=lambda: True,
            ))) for namespace, model in (("chat", chat), ("interview", interview))]
            try:
                await asyncio.gather(chat.started.wait(), interview.started.wait())
                self.assertEqual(engine.cancel_session_runs("shared", namespace="chat"), 1)
                await asyncio.sleep(0)
                self.assertTrue(tasks[0].cancelled())
                self.assertFalse(tasks[1].cancelled())
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(scenario())

    def test_cross_thread_cancel_releases_only_its_engine_registration(self):
        async def scenario():
            engine = AgentCore()
            model = WaitingModel()
            run = nico_agent_core.PreparedRun(
                key=nico_agent_core.RunKey("example", "threaded", 1),
                model=model, messages=({"role": "user", "content": "wait"},),
                tools=EmptyTools(), is_current=lambda: True,
            )
            task = asyncio.create_task(engine.execute(run))
            await model.started.wait()
            self.assertEqual(await asyncio.to_thread(engine.cancel_session_runs, "threaded"), 1)
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(engine.cancel_session_runs("threaded"), 0)

        asyncio.run(scenario())

    def test_identical_run_keys_in_separate_engines_do_not_cancel_each_other(self):
        async def scenario():
            key = nico_agent_core.RunKey("chat", "same", 1)
            engines = [AgentCore(), AgentCore()]
            models = [WaitingModel(), WaitingModel()]
            tasks = [asyncio.create_task(engine.execute(nico_agent_core.PreparedRun(
                key=key, model=model, messages=({"role": "user", "content": "wait"},),
                tools=EmptyTools(), is_current=lambda: True,
            ))) for engine, model in zip(engines, models)]
            try:
                await asyncio.gather(*(model.started.wait() for model in models))
                cancelled = engines[0].cancel_session_runs("same")
                await asyncio.sleep(0)
                self.assertEqual(cancelled, 1)
                self.assertTrue(tasks[0].cancelled())
                self.assertFalse(tasks[1].cancelled())
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
