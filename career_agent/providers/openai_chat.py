"""OpenAI Chat Completions, shared by `openai` and `openai_compatible`."""


def request(model: str, messages: list[dict], tools: list[dict],
            max_tokens: int) -> tuple[str, dict, dict]:
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if tools:
        payload["tools"] = tools
    return "chat/completions", {}, payload


def parse(data: dict) -> dict:
    choice = data["choices"][0]
    message = choice["message"]
    if not isinstance(message, dict):
        raise TypeError("模型消息不是对象")
    return {**message, "usage": data.get("usage"), "finish_reason": choice.get("finish_reason")}
