import asyncio
import inspect
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
from nico_application import (
    ToolAdapterPort,
    ToolCatalog,
    ToolCatalogPort,
    ToolCatalogSnapshot,
)
from nico_domain import tool_is_read_only

from nico_application.context import (
    ContextBudgetError,
    assemble as assemble_context,
    build_context as context_messages,
    prompt_version as prompt_fingerprint,
)
from nico_adapters.mcp import MCPServers
from nico_adapters.plugins import LocalPlugins, PluginTimeout
from nico_application.tools import READ_ONLY_TOOLS, call as run_internal, record, schemas, tool_message
from nico_storage import Store


class AgentRuntime:
    def __init__(self, store: Store, provider: ModelPort, mcp: MCPServers | None = None,
                 plugins: LocalPlugins | None = None,
                 tool_adapters: list[ToolAdapterPort] | None = None,
                 tool_catalog: ToolCatalogPort | None = None):
        self.store = store
        self.provider = provider
        self.mcp = mcp
        self.plugins = plugins
        self.tool_adapters = (list(tool_adapters) if tool_adapters is not None else
                              [adapter for adapter in (mcp, plugins) if adapter is not None])
        self.tool_catalog = tool_catalog or ToolCatalog()
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

    def _concurrency_safe(self, call: dict, external) -> bool:
        name = call["function"]["name"]
        if isinstance(external, ToolCatalogSnapshot):
            return external.is_read_only(name)
        if not isinstance(external, dict):
            external = {}
        if external and name in external:
            return tool_is_read_only(external[name].get("read_only"))
        return name in READ_ONLY_TOOLS

    async def _execute_tools(self, session: dict, calls: list[dict],
                             external: dict[str, dict] | None = None) -> list[dict]:
        return await self.core.execute_tools(
            calls,
            lambda call: self._execute_tool(session, call, external),
            lambda call: self._concurrency_safe(call, external or {}),
        )

    async def _execute_tool(
        self, session: dict, call: dict,
        external: dict[str, dict] | ToolCatalogSnapshot | None = None,
    ) -> dict:
        name = call["function"]["name"]
        snapshot = external if isinstance(external, ToolCatalogSnapshot) else None
        descriptors = external if isinstance(external, dict) else None
        tool = snapshot.external_tool(name) if snapshot is not None else (
            descriptors.get(name) if descriptors else None
        )
        if tool is not None:
            failure = "plugin_tool_failed" if tool.get("source") == "plugin" else "mcp_tool_failed"
            try:
                if snapshot is None and name not in self.store.tool_grants(session["id"]):
                    result = {"error": "tool_not_authorized"}
                elif not self.store.run_current(session["id"], session["version"]):
                    raise RunSuperseded()
                elif not tool_is_read_only(tool.get("read_only")):
                    result = {"error": "tool_write_disabled"}
                else:
                    try:
                        args = json.loads(call["function"].get("arguments") or "{}")
                        jsonschema.validate(args, tool["parameters"])
                    except (ValueError, TypeError, KeyError, jsonschema.ValidationError):
                        result = {"error": "invalid_arguments"}
                    else:
                        adapter = snapshot.external_adapter(name) if snapshot is not None else None
                        if adapter is not None:
                            result = await adapter.call(tool, args)
                        else:
                            result = (await self.plugins.call(tool, args)
                                      if tool.get("source") == "plugin"
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

    async def _prepare_tools(self, session: dict,
                             tools: list[ToolDefinition]) -> ToolCatalogSnapshot:
        grants = set(self.store.tool_grants(session["id"]))
        candidates = []
        candidate_handlers = {}
        if grants:
            for adapter in self.tool_adapters:
                try:
                    discovered = adapter.discover()
                    if inspect.isawaitable(discovered):
                        discovered = await discovered
                    for item in discovered:
                        if isinstance(item, dict) and isinstance(item.get("name"), str):
                            candidates.append(item)
                            if item["name"] in grants:
                                candidate_handlers[item["name"]] = adapter
                except Exception:
                    continue
        snapshot = self.tool_catalog.snapshot(
            tools, candidates, grants, READ_ONLY_TOOLS, candidate_handlers,
        )
        tools[:] = snapshot.definitions()
        return snapshot

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
            snapshot = None

            async def prepare_tools() -> None:
                nonlocal snapshot
                snapshot = await self._prepare_tools(session, tools)

            request = AgentRunRequest(
                model=self.provider,
                messages=context,
                tools=tools,
                is_current=lambda: self.store.run_current(session["id"], session["version"]),
                execute_tool=lambda call: self._execute_tool(session, call, snapshot),
                can_execute_in_parallel=lambda call: self._concurrency_safe(call, snapshot),
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
