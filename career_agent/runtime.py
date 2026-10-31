import asyncio
import json
import time

import jsonschema

from .context import (
    ContextBudgetError,
    assemble as assemble_context,
    build_context as context_messages,
    prompt_version as prompt_fingerprint,
)
from .events import Usage, run_event
from .mcp import MCPServers
from .plugins import LocalPlugins
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

    def build_context(self, session: dict) -> list[dict]:
        """Delegate prompt assembly to the context module."""
        return context_messages(self.store, session)

    def _tools(self, kind: str) -> list[dict]:
        return schemas(kind)

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
        if external and name in external:
            try:
                if name not in self.store.tool_grants(session["id"]):
                    result = {"error": "tool_not_authorized"}
                elif not self.store.run_current(session["id"], session["version"]):
                    raise RunSuperseded()
                else:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    tool = external[name]
                    result = (await self.plugins.call(tool, args) if tool.get("source") == "plugin"
                              else await self.mcp.call(tool, args))
            except RunSuperseded:
                raise
            except (ValueError, TypeError, KeyError, jsonschema.ValidationError):
                result = {"error": "invalid_arguments"}
            except Exception:
                result = {"error": "mcp_tool_failed"}
            record(self.store, session, name, result)
            return tool_message(call, result)
        return await asyncio.to_thread(run_internal, self.store, session, call)

    def _execute_tool_sync(self, session: dict, call: dict) -> dict:
        return run_internal(self.store, session, call)

    async def _model_loop(self, session: dict, messages: list[dict], tool_names: list[str],
                          usage: Usage) -> str:
        tools = self._tools(session["kind"])
        external = {}
        grants = set(self.store.tool_grants(session["id"]))
        if grants and self.mcp:
            for item in await self.mcp.discover():
                if item["name"] in grants:
                    external[item["name"]] = item
                    tools.append({"type": "function", "function": {
                        "name": item["name"], "description": item["description"],
                        "parameters": item["parameters"],
                    }})
        if grants and self.plugins:
            for item in self.plugins.discover():
                if item["name"] in grants:
                    external[item["name"]] = item
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

    async def run(self, session: dict) -> dict:
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
            if kind == "chat":
                content, status, count = raw, "active", 0
            else:
                payload = raw.strip()
                if payload.startswith("```") and payload.endswith("```"):
                    language, separator, fenced = payload.partition("\n")
                    if separator and language.lower() in ("```", "```json"):
                        payload = fenced[:-3].strip()
                data = json.loads(payload)
                if not isinstance(data, dict):
                    raise ValueError("面试模型必须返回 JSON 对象")
                if not {"feedback", "question", "finished", "evidence"}.issubset(data):
                    raise ValueError("面试模型缺少必要字段")
                feedback = data["feedback"]
                question = data["question"]
                evidence = data["evidence"]
                summary = data.get("summary", "")
                if (not isinstance(feedback, str) or not isinstance(question, str)
                        or not isinstance(data["finished"], bool) or not isinstance(evidence, list)
                        or not isinstance(summary, str)):
                    raise ValueError("面试模型输出字段格式错误")
                if (len(question) > 1000 or len(feedback) > 4000 or len(summary) > 4000
                        or len(evidence) > 5):
                    raise ValueError("面试模型输出超过长度上限")
                old_count = session["question_count"]
                max_questions = session["max_questions"]
                report = self.store.interview_report(session["id"])
                skipped = bool(old_count and report["items"][-1]["skipped"])
                if data["finished"] != (old_count >= max_questions):
                    raise ValueError("面试模型完成状态与题数不符")
                if not old_count and (feedback.strip() or summary.strip()):
                    raise ValueError("首题不能包含反馈或总结")
                if old_count < max_questions and summary.strip():
                    raise ValueError("未结束时不能生成全场总结")
                if old_count >= max_questions and question.strip():
                    raise ValueError("结束时不能生成下一题")
                if skipped and (feedback.strip() or evidence):
                    raise ValueError("跳过的题目不能生成反馈或回答证据")
                if old_count and not skipped and not feedback.strip():
                    raise ValueError("面试模型未返回反馈")
                if old_count and not skipped:
                    latest = self.store.messages(session["id"], "interview", limit=1)
                    answer = latest[0]["content"] if latest and latest[0]["role"] == "user" else ""
                    if not evidence or any(
                        not isinstance(quote, str) or not quote.strip() or len(quote) > 200 or quote not in answer
                        for quote in evidence
                    ):
                        raise ValueError("反馈证据必须是本题原始回答中的原文短句")
                elif evidence:
                    raise ValueError("首题不能引用尚未提交的回答")
                if old_count < max_questions and not question.strip():
                    raise ValueError("面试模型未返回下一题")
                if old_count >= max_questions:
                    if not summary.strip():
                        raise ValueError("面试模型未返回总结")
                    content, status, count = summary.strip(), "finished", old_count
                else:
                    content = (f"反馈：{feedback.strip()}\n\n" if feedback.strip() else "") + f"第 {old_count + 1} 题：{question.strip()}"
                    status, count = "awaiting_answer", old_count + 1
            committed = self.store.finish_run(
                session["id"], session["version"], content, status, count,
                question=question.strip() if kind == "interview" and status == "awaiting_answer" else None,
                feedback=feedback.strip() if kind == "interview" and session["question_count"] and not skipped else None,
                evidence=tuple(dict.fromkeys(evidence)) if kind == "interview" else (),
            )
            if not committed:
                outcome = "interrupted"
                return {"interrupted": True, "session": self.store.get(session["id"], kind)}
            outcome = "completed"
            return {"message": content, "session": self.store.get(session["id"], kind)}
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
