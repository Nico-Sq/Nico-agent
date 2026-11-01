"""Stable run bookkeeping and token-usage contracts."""

from collections.abc import Callable
from typing import Literal, TypedDict


MAX_RUN_TOKENS = 16_000
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


class Usage:
    """Accumulate reported token usage without trusting the provider's shape."""

    def __init__(self):
        self.request_count = 0
        self.covered_requests = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0

    def add(self, reported) -> None:
        """Add one reply's usage; a request counts as covered only if it reports a total."""
        if not isinstance(reported, dict):
            return
        for key in USAGE_KEYS:
            value = reported.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(self, key, getattr(self, key) + value)
                if key == "total_tokens":
                    self.covered_requests += 1

    @property
    def exceeded(self) -> bool:
        return self.total_tokens >= MAX_RUN_TOKENS

    def coverage(self) -> str:
        if self.request_count and self.covered_requests == self.request_count:
            return "exact"
        return "partial" if self.covered_requests else "unavailable"

    def as_dict(self) -> dict:
        return {
            "request_count": self.request_count,
            "covered_requests": self.covered_requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class RunEvent(TypedDict):
    version: int
    status: str
    error_code: str | None
    provider: str
    model: str
    prompt_version: str
    context_chars: int
    context_dropped: list[str]
    tool_names: list[str]
    usage: dict
    usage_coverage: str
    elapsed_ms: int


class TextDeltaEvent(TypedDict):
    type: Literal["text_delta"]
    text: str


class ToolStartEvent(TypedDict):
    type: Literal["tool_start"]
    name: str


RunUpdateEvent = TextDeltaEvent | ToolStartEvent
RunEventSink = Callable[[RunUpdateEvent], None]


class RunStartedEvent(TypedDict):
    type: Literal["started"]


class RunCompletedEvent(TypedDict):
    type: Literal["done"]


class RunErrorEvent(TypedDict):
    type: Literal["error"]
    detail: str
    code: str
    status: int


RunStreamEvent = RunStartedEvent | RunUpdateEvent | RunCompletedEvent | RunErrorEvent


def run_event(*, version: int, status: str, error_code: str | None, provider, model: str,
              prompt_version: str, context_chars: int, context_dropped: list[str],
              tool_names: list[str], usage: Usage, elapsed_ms: int) -> RunEvent:
    """Describe one finished, failed or interrupted run for the session log."""
    return {
        "version": version,
        "status": status,
        "error_code": error_code,
        "provider": provider or "custom",
        "model": model,
        "prompt_version": prompt_version,
        "context_chars": context_chars,
        "context_dropped": context_dropped,
        "tool_names": tool_names,
        "usage": usage.as_dict(),
        "usage_coverage": usage.coverage(),
        "elapsed_ms": elapsed_ms,
    }
