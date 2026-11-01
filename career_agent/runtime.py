import asyncio
import json
from collections.abc import Callable

import jsonschema
from nico_agent_core import (
    MAX_MODEL_INPUT_CHARS,
    MAX_MODEL_OUTPUT_CHARS,
    MAX_RUN_SECONDS,
    MAX_TOOL_CALLS,
    AgentCore,
    AgentRunRequest,
    AgentRunTrace,
    ModelPort,
    RunSuperseded,
    classify_run_error,
)
from nico_contracts.errors import ModelError
from nico_contracts.messages import AgentMessage
from nico_contracts.tools import ToolDefinition

from .context import (
    ContextBudgetError,
    assemble as assemble_context,
    build_context as context_messages,
    prompt_version as prompt_fingerprint,
)
from .mcp import MCPServers
from .plugins import LocalPlugins, PluginTimeout
from .store import Store
from .tools import READ_ONLY_TOOLS, call as run_internal, record, schemas, tool_message


class AgentRuntime:
    def __init__(self, store: Store, provider: ModelPort, mcp: MCPServers | None = None,
                 plugins: LocalPlugins | None = None):
        self.store = store
        self.provider = provider
        self.mcp = mcp
        self.plugins = plugins
        self.core = AgentCore()

    @classmethod
    def cancel_session_runs(cls, session_id: str) -> int:
        """Cancel tasks still working on a session after its version changed."""
        return AgentCore.cancel_session_runs(session_id)

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
        return await self.core.execute_tools(
            calls,
            lambda call: self._execute_tool(session, call, external),
            lambda call: self._concurrency_safe(call, external or {}),
        )

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

    async def _prepare_tools(self, session: dict, tools: list[ToolDefinition],
                             external: dict[str, dict]) -> None:
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

    async def run(self, session: dict,
                  result_handler: Callable[[dict, str], dict] | None = None) -> dict:
        kind = session["kind"]
        if not self.store.begin_run(session["id"], session["version"]):
            current = self.store.get(session["id"], kind)
            if current and current["status"] == "running" and current["version"] == session["version"]:
                return {"in_progress": True, "session": current}
            return {"interrupted": True, "session": current}
        task = asyncio.current_task()
        AgentCore.track_session_run(session["id"], session["version"], task)
        trace = AgentRunTrace(
            version=session["version"],
            provider=getattr(self.provider, "mode", "custom"),
            model=getattr(self.provider, "model", ""),
        )
        try:
            if not self.store.run_current(session["id"], session["version"]):
                raise RunSuperseded()
            trace.prompt_version = prompt_fingerprint(kind)
            try:
                context, trace.context_dropped = assemble_context(self.store, session)
            except ContextBudgetError as exc:
                raise ModelError(
                    "context_budget_exceeded",
                    "最新消息超出本轮上下文预算，请缩短内容或清空会话固定备注后重试", 413,
                ) from exc
            trace.context_chars = sum(len(item["content"]) for item in context)
            tools = self._tools(kind)
            external: dict[str, dict] = {}

            async def prepare_tools() -> None:
                await self._prepare_tools(session, tools, external)

            request = AgentRunRequest(
                model=self.provider,
                messages=context,
                tools=tools,
                is_current=lambda: self.store.run_current(session["id"], session["version"]),
                execute_tool=lambda call: self._execute_tool(session, call, external),
                can_execute_in_parallel=lambda call: self._concurrency_safe(call, external),
                prepare_tools=prepare_tools,
                trace=trace,
            )
            content = await self.core.run(request)
            result = self._finalize_run_result(session, content, result_handler)
            trace.outcome = "interrupted" if result.get("interrupted") else "completed"
            return result
        except RunSuperseded:
            trace.outcome = "interrupted"
            return {"interrupted": True, "session": self.store.get(session["id"], kind)}
        except asyncio.CancelledError:
            trace.outcome = "cancelled"
            trace.error_code = "cancelled"
            self.store.fail_run(session["id"], session["version"], kind, trace.error_code)
            raise
        except Exception as exc:
            trace.error_code = classify_run_error(exc)
            self.store.fail_run(session["id"], session["version"], kind, trace.error_code)
            raise
        finally:
            AgentCore.finish_session_run(session["id"], session["version"], task)
            self.store.event(session["id"], "model_run", trace.event())

    def _finalize_run_result(self, session: dict, raw: str,
                             result_handler: Callable[[dict, str], dict] | None) -> dict:
        kind = session["kind"]
        if kind != "chat":
            if result_handler is None:
                # Preserve direct AgentRuntime callers while keeping interview decisions in application.
                from nico_application import commit_interview_result

                result_handler = lambda current, output: commit_interview_result(
                    self.store, current, output,
                )
            return result_handler(session, raw)
        committed = self.store.finish_run(session["id"], session["version"], raw, "active", 0)
        if not committed:
            return {"interrupted": True, "session": self.store.get(session["id"], kind)}
        return {"message": raw, "session": self.store.get(session["id"], kind)}
