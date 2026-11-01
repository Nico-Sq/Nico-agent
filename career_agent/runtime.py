import asyncio
import json
import time
from collections.abc import Callable

import jsonschema
from nico_contracts.messages import AgentMessage
from nico_contracts.tools import ToolDefinition

from .context import (
    ContextBudgetError,
    assemble as assemble_context,
    build_context as context_messages,
    prompt_version as prompt_fingerprint,
)
from .events import Usage, run_event
from .mcp import MCPServers
from .plugins import LocalPlugins, PluginTimeout
from .provider import Provider
from .providers import ModelError
from .store import Store
from .tools import READ_ONLY_TOOLS, call as run_internal, record, schemas, tool_message


MAX_MODEL_INPUT_CHARS = 64_000
MAX_TOOL_CALLS = 8
MAX_RUN_SECONDS = 120
MAX_MODEL_OUTPUT_CHARS = 12_000

_ACTIVE_RUNS: dict[str, dict[int, asyncio.Task]] = {}


class RunSuperseded(Exception):
    pass


class AgentRuntime:
    def __init__(self, store: Store, provider: Provider, mcp: MCPServers | None = None,
                 plugins: LocalPlugins | None = None):
        self.store = store
        self.provider = provider
        self.mcp = mcp
        self.plugins = plugins

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

    def build_context(self, session: dict) -> list[AgentMessage]:
        """Delegate prompt assembly to the context module."""
        return context_messages(self.store, session)

    def _tools(self, kind: str) -> list[ToolDefinition]:
        return list(schemas(kind))

    def _concurrency_safe(self, call: dict, external: dict[str, dict]) -> bool:
        name = call["function"]["name"]
        if external and name in external:
            return bool(external[name].get("read_only"))
        return name in READ_ONLY_TOOLS

    async def _execute_tools(self, session: dict, calls: list[dict],
                             external: dict[str, dict] | None = None) -> list[dict]:
        """只读工具按连续批次并行，有副作用的工具单独串行执行。"""
        async def run(batch: list[dict]) -> list[dict]:
            if not batch:
                return []
            return list(await asyncio.gather(*(self._execute_tool(session, call, external) for call in batch)))

        results, batch = [], []
        for call in calls:
            if self._concurrency_safe(call, external or {}):
                batch.append(call)
                continue
            results.extend(await run(batch))
            batch = []
            results.append(await self._execute_tool(session, call, external))
        results.extend(await run(batch))
        return results

    async def _execute_tool(self, session: dict, call: dict, external: dict[str, dict] | None = None) -> dict:
        name = call["function"]["name"]
        tool = external.get(name) if external else None
        if tool is not None:
            failure = "plugin_tool_failed" if tool.get("source") == "plugin" else "mcp_tool_failed"
            try:
                if name not in self.store.tool_grants(session["id"]):
                    result = {"error": "tool_not_authorized"}
                elif not self.store.run_current(session["id"], session["version"]):
                    raise RunSuperseded()
                elif tool.get("read_only") is not True:
                    result = {"error": "tool_write_disabled"}
                else:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        jsonschema.validate(args, tool["parameters"])
                    except (ValueError, TypeError, KeyError, jsonschema.ValidationError):
                        result = {"error": "invalid_arguments"}
                    else:
                        result = (await self.plugins.call(tool, args) if tool.get("source") == "plugin"
                                  else await self.mcp.call(tool, args))
            except RunSuperseded:
                raise
            except PluginTimeout:
                result = {"error": "plugin_timeout", "may_still_be_running": True}
            except Exception:
                result = {"error": failure}
            record(self.store, session, name, result)
            return tool_message(call, result)
        if not self.store.run_current(session["id"], session["version"]):
            raise RunSuperseded()
        return await asyncio.to_thread(run_internal, self.store, session, call)

    def _execute_tool_sync(self, session: dict, call: dict) -> dict:
        return run_internal(self.store, session, call)

    async def _model_loop(self, session: dict, messages: list[AgentMessage], tool_names: list[str],
                          usage: Usage) -> str:
        tools = self._tools(session["kind"])
        external = {}
        grants = set(self.store.tool_grants(session["id"]))
        if grants and self.mcp:
            for item in await self.mcp.discover():
                if item["name"] in grants:
                    external[item["name"]] = item
                    if item.get("read_only") is True:
                        tools.append({"type": "function", "function": {
                            "name": item["name"], "description": item["description"],
                            "parameters": item["parameters"],
                        }})
        if grants and self.plugins:
            for item in self.plugins.discover():
                if item["name"] in grants:
                    external[item["name"]] = item
                    if item.get("read_only") is True:
                        tools.append({"type": "function", "function": {
                            "name": item["name"], "description": item["description"],
                            "parameters": item["parameters"],
                        }})
        for _ in range(4):
            if not self.store.run_current(session["id"], session["version"]):
                raise RunSuperseded()
            usage.request_count += 1
            reply = await self.provider.complete(messages, tools)
            usage.add(reply.get("usage"))
            if not self.store.run_current(session["id"], session["version"]):
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
                return content.strip()
            if usage.exceeded:
                raise ModelError("run_token_limit", "本次模型运行已达到用量上限")
            if len(tool_names) + len(calls) > MAX_TOOL_CALLS:
                raise ValueError("本次运行的工具调用超过上限")
            tool_names.extend(call["function"]["name"] for call in calls)
            messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
            messages.extend(await self._execute_tools(session, calls, external))
            if sum(len(message.get("content") or "") + len(json.dumps(message.get("tool_calls", []), ensure_ascii=False))
                   for message in messages) > MAX_MODEL_INPUT_CHARS:
                raise ModelError(
                    "context_budget_exceeded",
                    "工具结果超过模型上下文预算，请减少本次读取的材料或经历", 413,
                )
        raise ValueError("模型工具调用超过 4 轮")

    async def run(self, session: dict,
                  result_handler: Callable[[dict, str], dict] | None = None) -> dict:
        kind = session["kind"]
        if not self.store.begin_run(session["id"], session["version"]):
            current = self.store.get(session["id"], kind)
            if current and current["status"] == "running" and current["version"] == session["version"]:
                return {"in_progress": True, "session": current}
            return {"interrupted": True, "session": current}
        task = asyncio.current_task()
        if task is not None:
            _ACTIVE_RUNS.setdefault(session["id"], {})[session["version"]] = task
        started = time.perf_counter()
        outcome = "failed"
        context_chars = 0
        tool_names = []
        dropped: list[str] = []
        usage = Usage()
        prompt_version = ""
        error_code = None
        try:
            if not self.store.run_current(session["id"], session["version"]):
                raise RunSuperseded()
            prompt_version = prompt_fingerprint(kind)
            try:
                context, dropped = assemble_context(self.store, session)
            except ContextBudgetError as exc:
                raise ModelError(
                    "context_budget_exceeded",
                    "最新消息超出本轮上下文预算，请缩短内容或清空会话固定备注后重试", 413,
                ) from exc
            context_chars = sum(len(item["content"]) for item in context)
            try:
                async with asyncio.timeout(MAX_RUN_SECONDS):
                    raw = await self._model_loop(session, context, tool_names, usage)
            except TimeoutError as exc:
                raise ModelError("run_timeout", "本次模型运行超时", 504) from exc
            if len(raw) > MAX_MODEL_OUTPUT_CHARS:
                raise ValueError("模型输出超过长度上限")
            if kind != "chat":
                if result_handler is None:
                    # Preserve direct AgentRuntime callers while keeping interview decisions in application.
                    from nico_application import commit_interview_result

                    result_handler = lambda current, output: commit_interview_result(
                        self.store, current, output,
                    )
                result = result_handler(session, raw)
                outcome = "interrupted" if result.get("interrupted") else "completed"
                return result

            committed = self.store.finish_run(
                session["id"], session["version"], raw, "active", 0,
            )
            if not committed:
                outcome = "interrupted"
                return {"interrupted": True, "session": self.store.get(session["id"], kind)}
            outcome = "completed"
            return {"message": raw, "session": self.store.get(session["id"], kind)}
        except RunSuperseded:
            outcome = "interrupted"
            return {"interrupted": True, "session": self.store.get(session["id"], kind)}
        except asyncio.CancelledError:
            outcome = "cancelled"
            error_code = "cancelled"
            self.store.fail_run(session["id"], session["version"], kind, error_code)
            raise
        except Exception as exc:
            if isinstance(exc, ModelError):
                error_code = exc.code
            elif isinstance(exc, ValueError):
                error_code = "model_invalid_output"
            else:
                error_code = "model_run_failed"
            self.store.fail_run(session["id"], session["version"], kind, error_code)
            raise
        finally:
            runs = _ACTIVE_RUNS.get(session["id"])
            if runs and runs.get(session["version"]) is task:
                runs.pop(session["version"], None)
                if not runs:
                    _ACTIVE_RUNS.pop(session["id"], None)
            self.store.event(session["id"], "model_run", run_event(
                version=session["version"], status=outcome, error_code=error_code,
                provider=getattr(self.provider, "mode", "custom"),
                model=getattr(self.provider, "model", ""),
                prompt_version=prompt_version, context_chars=context_chars,
                context_dropped=dropped, tool_names=tool_names, usage=usage,
                elapsed_ms=round((time.perf_counter() - started) * 1000),
            ))
