"""Translate the runtime's tool messages to and from Anthropic Messages."""

import json

PROTOCOL_VERSION = "2026-10-21"


def request(model: str, messages: list[dict], tools: list[dict],
            max_tokens: int) -> tuple[str, dict, dict]:
    return "messages", {"anthropic-version": PROTOCOL_VERSION}, request_payload(
        model, messages, tools, max_tokens)


def parse(data: dict) -> dict:
    return parse_response(data)


def request_payload(model: str, messages: list[dict], tools: list[dict], max_tokens: int) -> dict:
    system = "\n\n".join(message["content"] for message in messages if message["role"] == "system")
    converted = []
    for message in messages:
        role = message["role"]
        if role == "system":
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": message["tool_call_id"],
                     "content": message["content"]}
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list) \
                    and converted[-1]["content"][0]["type"] == "tool_result":
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
        elif role == "assistant" and message.get("tool_calls"):
            blocks = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                blocks.append({"type": "tool_use", "id": call["id"],
                               "name": call["function"]["name"],
                               "input": json.loads(call["function"]["arguments"] or "{}")})
            converted.append({"role": "assistant", "content": blocks})
        else:
            converted.append({"role": role, "content": message["content"]})
    payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": converted}
    if tools:
        payload["tools"] = [{"name": item["function"]["name"],
                             "description": item["function"]["description"],
                             "input_schema": item["function"]["parameters"]} for item in tools]
    return payload


def parse_response(data: dict) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise ValueError("invalid content")
    text = []
    calls = []
    for block in data["content"]:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise ValueError("invalid content")
        if block["type"] == "text":
            text.append(block["text"])
        elif block["type"] == "tool_use":
            calls.append({"id": block["id"], "type": "function", "function": {
                "name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False),
            }})
    reason = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}.get(
        data.get("stop_reason"), "content_filter"
    )
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        raise ValueError("invalid usage")
    prompt = usage.get("input_tokens", 0)
    completion = usage.get("output_tokens", 0)
    if not isinstance(prompt, int) or not isinstance(completion, int):
        raise ValueError("invalid usage")
    return {"content": "\n".join(text), "tool_calls": calls, "finish_reason": reason,
            "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                      "total_tokens": prompt + completion}}
