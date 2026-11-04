"""Generic non-streaming model and tool loop."""

import asyncio
import copy
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from threading import Lock

from nico_contracts.errors import ModelError
from nico_contracts.events import RunEventSink, Usage, run_event
from nico_contracts.messages import AgentMessage
from nico_contracts.tools import ToolDefinition

from .ports import ModelPort, StreamingModelPort
from .events import EngineEvent, RunObserver
from .run import (
    MAX_MODEL_INPUT_CHARS,
    MAX_MODEL_OUTPUT_CHARS,
    MAX_MODEL_TURNS,
    MAX_RUN_SECONDS,
    MAX_TOOL_CALLS,
    PreparedRun,
    RunKey,
    RunOutcome,
    RunPolicy,
)

class RunSuperseded(Exception):
    """The session version changed while this run was in flight."""


@dataclass
class AgentRunRequest:
    model: ModelPort
    messages: list[AgentMessage]
    tools: list[ToolDefinition]
    is_current: Callable[[], bool]
    execute_tool: Callable[[dict], Awaitable[dict]]
    can_execute_in_parallel: Callable[[dict], bool]
    prepare_tools: Callable[[], Awaitable[None]]
    trace: "AgentRunTrace" = field(default_factory=lambda: AgentRunTrace())
    on_event: RunEventSink | None = None
    observer: RunObserver | None = None
    policy: RunPolicy = field(default_factory=RunPolicy)
    key: RunKey | None = None
    parsed_arguments: dict[str, dict] = field(default_factory=dict)


@dataclass
class AgentRunTrace:
    version: int = 0
    key: RunKey | None = None
    provider: str = "custom"
    model: str = ""
    prompt_version: str = ""
    context_chars: int = 0
    context_dropped: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    tool_names: list[str] = field(default_factory=list)
    outcome: str = "failed"
    error_code: str | None = None
    started_at: float = field(default_factory=time.perf_counter)
    sequence: int = 0
    streamed_chars: int = 0

    def event(self) -> dict:
        return run_event(
            version=self.version, status=self.outcome, error_code=self.error_code,
            provider=self.provider, model=self.model, prompt_version=self.prompt_version,
            context_chars=self.context_chars, context_dropped=self.context_dropped,
            tool_names=self.tool_names, usage=self.usage,
            elapsed_ms=round((time.perf_counter() - self.started_at) * 1000),
        )


def classify_run_error(error: Exception) -> str:
    if isinstance(error, ModelError):
        return error.code
    if isinstance(error, ValueError):
        return "model_invalid_output"
    return "model_run_failed"


def _message_chars(messages: list[AgentMessage]) -> int:
    return sum(len(message.get("content") or "")
               + len(json.dumps(message.get("tool_calls", []), ensure_ascii=False))
               for message in messages)


class AgentCore:
    def __init__(self) -> None:
        self._active_runs: dict[RunKey, asyncio.Task] = {}
        # Sync handlers may cancel work owned by another event-loop thread.
        self._active_runs_lock = Lock()

    async def execute(self, prepared: PreparedRun) -> RunOutcome:
        async def execute_tool(call: dict) -> dict:
            name = call["function"]["name"]
            args = parsed_arguments[call["id"]]
            result = await prepared.tools.execute(name, args)
            return {"role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps(result, ensure_ascii=False)}

        async def prepare_tools() -> None:
            return None

        parsed_arguments: dict[str, dict] = {}
        request = AgentRunRequest(
            model=prepared.model,
            messages=list(prepared.messages),
            tools=list(prepared.tools.definitions),
            is_current=prepared.is_current,
            execute_tool=execute_tool,
            can_execute_in_parallel=lambda call: prepared.tools.can_execute_in_parallel(
                call["function"]["name"]),
            prepare_tools=prepare_tools,
            trace=AgentRunTrace(version=prepared.key.version, key=prepared.key),
            on_event=prepared.on_event,
            observer=prepared.observer,
            policy=prepared.policy,
            key=prepared.key,
            parsed_arguments=parsed_arguments,
        )
        return RunOutcome(await self.run(request), request.trace)

    def track_session_run(self, key: RunKey, task: asyncio.Task | None) -> None:
        if task is not None:
            with self._active_runs_lock:
                previous = self._active_runs.get(key)
                if previous is not None and previous is not task and not previous.done():
                    raise ModelError("run_already_active", "该会话版本已有模型运行", 409)
                self._active_runs[key] = task

    def finish_session_run(self, key: RunKey, task: asyncio.Task | None) -> None:
        with self._active_runs_lock:
            if self._active_runs.get(key) is task:
                self._active_runs.pop(key, None)

    def cancel_session_runs(self, session_id: str, before_version: int | None = None,
                            namespace: str | None = None) -> int:
        """Cancel older runs, optionally keeping runs at or after a state version."""
        with self._active_runs_lock:
            tasks = [
                task for key, task in self._active_runs.items()
                if key.session_id == session_id
                and (namespace is None or key.namespace == namespace)
                and not task.done() and (before_version is None or key.version < before_version)
            ]
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        cancelled = 0
        for task in tasks:
            loop = task.get_loop()
            if loop.is_closed():
                continue
            if loop is current_loop:
                cancelled += int(task.cancel())
            else:
                loop.call_soon_threadsafe(task.cancel)
                cancelled += 1
        return cancelled

    async def run(self, request: AgentRunRequest) -> str:
        task = asyncio.current_task()
        request.trace.sequence = 0
        request.trace.streamed_chars = 0
        if request.key is not None:
            self.track_session_run(request.key, task)
        try:
            if any(not isinstance(message, dict)
                   or message.get("role") not in ("system", "user", "assistant", "tool")
                   or not isinstance(message.get("content"), (str, type(None)))
                   or (message.get("role") != "assistant"
                       and not isinstance(message.get("content"), str))
                   for message in request.messages):
                raise ModelError("model_invalid_input", "模型输入消息格式错误")
            if _message_chars(request.messages) > request.policy.max_input_chars:
                raise ModelError("context_budget_exceeded", "模型输入超过上下文预算", 413)
            working_request = replace(request, messages=copy.deepcopy(request.messages))
            try:
                async with asyncio.timeout(request.policy.timeout_seconds):
                    if not request.is_current():
                        raise RunSuperseded()
                    await request.prepare_tools()
                    content = await self._model_loop(working_request)
            except TimeoutError as exc:
                error = ModelError("run_timeout", "本次模型运行超时", 504)
                request.trace.outcome = "failed"
                request.trace.error_code = error.code
                raise error from exc
            if len(content) > request.policy.max_output_chars:
                raise ValueError("模型输出超过长度上限")
            request.trace.outcome = "completed"
            return content
        except RunSuperseded:
            request.trace.outcome = "interrupted"
            raise
        except asyncio.CancelledError:
            request.trace.outcome = "cancelled"
            request.trace.error_code = "cancelled"
            raise
        except Exception as exc:
            request.trace.outcome = "failed"
            request.trace.error_code = classify_run_error(exc)
            raise
        finally:
            self._emit(request, request.trace.outcome, error_code=request.trace.error_code)
            if request.key is not None:
                self.finish_session_run(request.key, task)

    def _emit(self, request: AgentRunRequest, event_type: str, *, text: str = "",
              name: str = "", error_code: str | None = None) -> None:
        if event_type == "text_delta":
            request.trace.streamed_chars += len(text)
            if request.trace.streamed_chars > request.policy.max_output_chars:
                raise ValueError("模型输出超过长度上限")
        request.trace.sequence += 1
        event = EngineEvent(request.key, request.trace.sequence, event_type,
                            text=text, name=name, error_code=error_code)
        if request.observer is not None:
            try:
                request.observer(event)
            except Exception:
                logging.getLogger(__name__).warning("Agent run observer failed")
        if request.on_event is not None and event_type in ("text_delta", "tool_start"):
            update = ({"type": "text_delta", "text": text} if event_type == "text_delta"
                      else {"type": "tool_start", "name": name})
            try:
                request.on_event(update)
            except Exception:
                logging.getLogger(__name__).warning("Agent run update subscriber failed")

    async def _model_loop(self, request: AgentRunRequest) -> str:
        for _ in range(request.policy.max_turns):
            if not request.is_current():
                raise RunSuperseded()
            request.trace.usage.request_count += 1
            streaming = (request.on_event is not None or request.observer is not None) and isinstance(
                request.model, StreamingModelPort)
            if streaming:
                reply = await request.model.stream(
                    request.messages, request.tools,
                    lambda text: self._emit(request, "text_delta", text=text),
                )
            else:
                reply = await request.model.complete(request.messages, request.tools)
            request.trace.usage.add(reply.get("usage"))
            if not request.is_current():
                raise RunSuperseded()
            finish_reason = reply.get("finish_reason")
            if finish_reason == "length":
                raise ModelError("model_output_truncated", "模型输出达到上限，请缩短本轮材料或请求后重试")
            if finish_reason not in (None, "stop", "tool_calls"):
                raise ModelError("model_incomplete", "模型未正常完成本轮回复，请检查服务状态后重试")
            calls = reply.get("tool_calls") or []
            if (not isinstance(calls, list) or any(
                not isinstance(call, dict) or not isinstance(call.get("id"), str)
                or not call["id"]
                or not isinstance(call.get("function"), dict)
                or not isinstance(call["function"].get("name"), str)
                or not call["function"]["name"]
                for call in calls
            )):
                raise ValueError("模型工具调用格式错误")
            if len({call["id"] for call in calls}) != len(calls):
                raise ValueError("模型工具调用 ID 重复")
            for call in calls:
                arguments = call["function"].get("arguments", "{}")
                if not isinstance(arguments, str):
                    raise ValueError("模型工具调用参数格式错误")
                try:
                    parsed = json.loads(arguments or "{}")
                except ValueError as exc:
                    raise ValueError("模型工具调用参数格式错误") from exc
                if not isinstance(parsed, dict):
                    raise ValueError("模型工具调用参数格式错误")
                request.parsed_arguments[call["id"]] = parsed
            if not calls:
                if finish_reason == "tool_calls":
                    raise ModelError("model_invalid_response", "模型服务返回了无效的工具调用结果")
                content = reply.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("模型未返回有效内容")
                if not streaming:
                    self._emit(request, "text_delta", text=content)
                return content.strip()
            if request.trace.usage.total_tokens >= request.policy.max_reported_tokens:
                raise ModelError("run_token_limit", "本次模型运行已达到用量上限")
            if len(request.trace.tool_names) + len(calls) > request.policy.max_tool_calls:
                raise ValueError("本次运行的工具调用超过上限")
            request.trace.tool_names.extend(call["function"]["name"] for call in calls)
            for call in calls:
                self._emit(request, "tool_start", name=call["function"]["name"])
            request.messages.append({"role": "assistant", "content": reply.get("content"),
                                     "tool_calls": calls})
            request.messages.extend(await self.execute_tools(
                calls, request.execute_tool, request.can_execute_in_parallel,
            ))
            if _message_chars(request.messages) > request.policy.max_input_chars:
                raise ModelError(
                    "context_budget_exceeded",
                    "工具结果超过模型上下文预算，请减少本次读取的材料或经历", 413,
                )
        raise ValueError("模型工具调用超过 4 轮")

    async def execute_tools(
        self,
        calls: list[dict],
        execute_tool: Callable[[dict], Awaitable[dict]],
        can_execute_in_parallel: Callable[[dict], bool],
    ) -> list[dict]:
        async def run(batch: list[dict]) -> list[dict]:
            if not batch:
                return []
            return list(await asyncio.gather(*(execute_tool(call) for call in batch)))

        results, batch = [], []
        for call in calls:
            if can_execute_in_parallel(call):
                batch.append(call)
                continue
            results.extend(await run(batch))
            batch = []
            results.append(await execute_tool(call))
        results.extend(await run(batch))
        return results
