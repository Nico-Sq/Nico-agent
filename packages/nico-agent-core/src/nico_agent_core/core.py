"""Generic non-streaming model and tool loop."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from nico_contracts.errors import ModelError
from nico_contracts.events import RunEventSink, Usage, run_event
from nico_contracts.messages import AgentMessage
from nico_contracts.tools import ToolDefinition

from .ports import ModelPort

MAX_MODEL_INPUT_CHARS = 64_000
MAX_TOOL_CALLS = 8
MAX_RUN_SECONDS = 120
MAX_MODEL_OUTPUT_CHARS = 12_000
MAX_MODEL_TURNS = 4

_ACTIVE_RUNS: dict[str, dict[int, asyncio.Task]] = {}


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


@dataclass
class AgentRunTrace:
    version: int = 0
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


class AgentCore:
    @classmethod
    def track_session_run(cls, session_id: str, version: int, task: asyncio.Task | None) -> None:
        if task is not None:
            _ACTIVE_RUNS.setdefault(session_id, {})[version] = task

    @classmethod
    def finish_session_run(cls, session_id: str, version: int, task: asyncio.Task | None) -> None:
        runs = _ACTIVE_RUNS.get(session_id)
        if runs and runs.get(version) is task:
            runs.pop(version, None)
            if not runs:
                _ACTIVE_RUNS.pop(session_id, None)

    @classmethod
    def cancel_session_runs(cls, session_id: str) -> int:
        """Cancel tasks still working on a session after its version changed."""
        runs = _ACTIVE_RUNS.get(session_id, {})
        tasks = [task for task in runs.values() if not task.done()]
        if runs and not tasks:
            _ACTIVE_RUNS.pop(session_id, None)
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
        try:
            try:
                async with asyncio.timeout(MAX_RUN_SECONDS):
                    await request.prepare_tools()
                    content = await self._model_loop(request)
            except TimeoutError as exc:
                error = ModelError("run_timeout", "本次模型运行超时", 504)
                request.trace.outcome = "failed"
                request.trace.error_code = error.code
                raise error from exc
            if len(content) > MAX_MODEL_OUTPUT_CHARS:
                raise ValueError("模型输出超过长度上限")
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

    async def _model_loop(self, request: AgentRunRequest) -> str:
        for _ in range(MAX_MODEL_TURNS):
            if not request.is_current():
                raise RunSuperseded()
            request.trace.usage.request_count += 1
            stream = getattr(request.model, "stream", None)
            streaming = request.on_event is not None and callable(stream)
            if streaming:
                reply = await stream(
                    request.messages, request.tools,
                    lambda text: request.on_event({"type": "text_delta", "text": text}),
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
                or not isinstance(call.get("function"), dict)
                or not isinstance(call["function"].get("name"), str)
                for call in calls
            )):
                raise ValueError("模型工具调用格式错误")
            if not calls:
                if finish_reason == "tool_calls":
                    raise ModelError("model_invalid_response", "模型服务返回了无效的工具调用结果")
                content = reply.get("content")
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("模型未返回有效内容")
                if request.on_event is not None and not streaming:
                    request.on_event({"type": "text_delta", "text": content})
                return content.strip()
            if request.trace.usage.exceeded:
                raise ModelError("run_token_limit", "本次模型运行已达到用量上限")
            if len(request.trace.tool_names) + len(calls) > MAX_TOOL_CALLS:
                raise ValueError("本次运行的工具调用超过上限")
            request.trace.tool_names.extend(call["function"]["name"] for call in calls)
            if request.on_event is not None:
                for call in calls:
                    request.on_event({"type": "tool_start", "name": call["function"]["name"]})
            request.messages.append({"role": "assistant", "content": reply.get("content"),
                                     "tool_calls": calls})
            request.messages.extend(await self.execute_tools(
                calls, request.execute_tool, request.can_execute_in_parallel,
            ))
            if sum(len(message.get("content") or "")
                   + len(json.dumps(message.get("tool_calls", []), ensure_ascii=False))
                   for message in request.messages) > MAX_MODEL_INPUT_CHARS:
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
