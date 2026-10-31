"""A runnable, read-only local plugin example."""


def echo(arguments):
    return f"收到：{arguments['text']}"


TOOLS = [{
    "name": "echo",
    "description": "原样确认输入文本，用于检查本地插件接入。",
    "parameters": {"type": "object", "properties": {"text": {"type": "string", "maxLength": 200}},
                   "required": ["text"], "additionalProperties": False},
    "run": echo,
}]
