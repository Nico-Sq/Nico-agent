"""OpenAI-compatible Chat Completions adapter."""

import json
from collections.abc import AsyncIterator, Callable

from nico_contracts.errors import ModelError
from nico_contracts.messages import AgentMessage
from nico_contracts.models import ModelReply
from nico_contracts.tools import ToolDefinition


def request(model: str, messages: list[AgentMessage], tools: list[ToolDefinition],
            max_tokens: int, temperature: float | None = None) -> tuple[str, dict, dict]:
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
    if temperature is not None:
        payload["temperature"] = temperature
    return "chat/completions", {}, payload


def parse(data: dict) -> ModelReply:
    choice = data["choices"][0]
    message = choice["message"]
    if not isinstance(message, dict):
        raise TypeError("模型消息不是对象")
    return {**message, "usage": data.get("usage"), "finish_reason": choice.get("finish_reason")}


async def parse_stream(chunks: AsyncIterator[str], on_delta: Callable[[str], None]) -> ModelReply:
    text: list[str] = []
    tools: dict[int, dict] = {}
    finish_reason = None
    usage = None
    finished = False

    async for chunk in chunks:
        if chunk == "[DONE]":
            finished = True
            break
        try:
            data = json.loads(chunk)
        except (TypeError, ValueError) as exc:
            raise ModelError("model_invalid_response", "模型服务返回了无效响应") from exc
        if not isinstance(data, dict):
            raise ModelError("model_invalid_response", "模型服务返回了无效响应")
        if "usage" in data:
            usage = data["usage"]
            if usage is not None and not isinstance(usage, dict):
                raise ModelError("model_invalid_response", "模型服务返回了无效响应")
        choices = data.get("choices", [])
        if not isinstance(choices, list):
            raise ModelError("model_invalid_response", "模型服务返回了无效响应")
        if not choices:
            continue
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ModelError("model_invalid_response", "模型服务返回了无效响应")
        delta = choice.get("delta", {})
        if not isinstance(delta, dict):
            raise ModelError("model_invalid_response", "模型服务返回了无效响应")
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                raise ModelError("model_invalid_response", "模型服务返回了无效响应")
            if content:
                text.append(content)
                on_delta(content)
        fragments = delta.get("tool_calls", [])
        if not isinstance(fragments, list):
            raise ModelError("model_invalid_response", "模型服务返回了无效响应")
        for fragment in fragments:
            if not isinstance(fragment, dict):
                raise ModelError("model_invalid_response", "模型服务返回了无效响应")
            index = fragment.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                raise ModelError("model_invalid_response", "模型服务返回了无效响应")
            call = tools.setdefault(index, {"id": "", "type": "function",
                                            "function": {"name": "", "arguments": ""}})
            call_id = fragment.get("id")
            if call_id is not None:
                if not isinstance(call_id, str):
                    raise ModelError("model_invalid_response", "模型服务返回了无效响应")
                call["id"] = call_id
            function = fragment.get("function", {})
            if not isinstance(function, dict):
                raise ModelError("model_invalid_response", "模型服务返回了无效响应")
            for key in ("name", "arguments"):
                part = function.get(key)
                if part is not None:
                    if not isinstance(part, str):
                        raise ModelError("model_invalid_response", "模型服务返回了无效响应")
                    call["function"][key] += part
        reason = choice.get("finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise ModelError("model_invalid_response", "模型服务返回了无效响应")
            finish_reason = reason

    if not finished or finish_reason is None:
        raise ModelError("model_invalid_response", "模型服务的流式响应未正常结束")
    message = {"role": "assistant", "content": "".join(text) or None}
    if tools:
        message["tool_calls"] = [tools[index] for index in sorted(tools)]
    try:
        return parse({"choices": [{"message": message, "finish_reason": finish_reason}],
                      "usage": usage})
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ModelError("model_invalid_response", "模型服务返回了无效响应") from exc
