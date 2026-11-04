"""An independent host using only the distributable engine contracts."""

import asyncio
import unittest

from nico_agent_core import (AgentCore, FrozenToolSnapshot, PreparedRun, RunKey,
                             RunPolicy, ToolBinding)
from nico_contracts.errors import ModelError


class LogbookHost:
    def __init__(self, *, include_plugin=False):
        self.engine = AgentCore()
        self.events = []
        self.updates = []
        self.calls = []
        self.include_plugin = include_plugin

    def snapshot(self):
        async def read(args):
            self.calls.append(args["entry"])
            return {"entry": args["entry"], "value": "ready"}

        bindings = [ToolBinding({"type": "function", "function": {
            "name": "read_entry", "description": "Read a log entry",
            "parameters": {"type": "object", "properties": {"entry": {"type": "string"}},
                           "required": ["entry"], "additionalProperties": False},
        }}, read, parallel=True)]
        if self.include_plugin:
            async def plugin(args):
                return {"source": "plugin", "value": args["entry"]}

            bindings.append(ToolBinding({"type": "function", "function": {
                "name": "plugin_entry", "description": "Optional log lookup",
                "parameters": {"type": "object", "properties": {"entry": {"type": "string"}},
                               "required": ["entry"]},
            }}, plugin, parallel=True))
        return FrozenToolSnapshot(tuple(bindings))

    async def run(self, model, *, policy=None, session_id="log-1"):
        outcome = await self.engine.execute(PreparedRun(
            key=RunKey("logbook", session_id, 1), model=model,
            messages=({"role": "system", "content": "Summarize the requested log entry."},
                      {"role": "user", "content": "Summarize entry one."}),
            tools=self.snapshot(), is_current=lambda: True,
            policy=policy or RunPolicy(), on_event=self.updates.append,
            observer=self.events.append,
        ))
        return {"summary": outcome.content.upper(), "requests": outcome.trace.usage.request_count}


class LogbookModel:
    def __init__(self, tool_name="read_entry"):
        self.calls = 0
        self.tool_name = tool_name

    async def complete(self, messages, tools):
        return await self.stream(messages, tools, lambda _: None)

    async def stream(self, messages, tools, on_delta):
        self.calls += 1
        if self.calls == 1:
            assert self.tool_name in {item["function"]["name"] for item in tools}
            return {"content": None, "finish_reason": "tool_calls", "tool_calls": [{
                "id": "entry-1", "function": {"name": self.tool_name,
                                            "arguments": '{"entry":"one"}'},
            }]}
        on_delta("entry ready")
        return {"content": "entry ready", "finish_reason": "stop"}


class ThirdHostTests(unittest.TestCase):
    def test_host_runs_tool_cycle_and_ordered_stream_events(self):
        host = LogbookHost()
        result = asyncio.run(host.run(LogbookModel()))
        self.assertEqual(result, {"summary": "ENTRY READY", "requests": 2})
        self.assertEqual(host.calls, ["one"])
        self.assertEqual([event.type for event in host.events],
                         ["tool_start", "text_delta", "completed"])
        self.assertEqual([event.sequence for event in host.events], [1, 2, 3])
        self.assertEqual([event["type"] for event in host.updates], ["tool_start", "text_delta"])

    def test_optional_plugin_binding_needs_no_engine_change(self):
        host = LogbookHost(include_plugin=True)
        result = asyncio.run(host.run(LogbookModel("plugin_entry")))
        self.assertEqual(result["summary"], "ENTRY READY")
        self.assertEqual(host.calls, [])

    def test_host_can_cancel_its_own_run(self):
        class WaitingModel:
            def __init__(self):
                self.started = asyncio.Event()

            async def complete(self, messages, tools):
                self.started.set()
                await asyncio.Event().wait()

        async def scenario():
            host, model = LogbookHost(), WaitingModel()
            task = asyncio.create_task(host.run(model))
            await model.started.wait()
            self.assertEqual(host.engine.cancel_session_runs("log-1", namespace="logbook"), 1)
            await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(host.engine.cancel_session_runs("log-1"), 0)
            return host.events

        self.assertEqual([event.type for event in asyncio.run(scenario())], ["cancelled"])

    def test_host_budget_rejects_before_model_call(self):
        host, model = LogbookHost(), LogbookModel()
        with self.assertRaises(ModelError) as caught:
            asyncio.run(host.run(model, policy=RunPolicy(max_input_chars=5)))
        self.assertEqual(caught.exception.code, "context_budget_exceeded")
        self.assertEqual(model.calls, 0)


if __name__ == "__main__":
    unittest.main()
