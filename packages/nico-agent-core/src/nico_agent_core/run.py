"""Host-neutral input and outcome for one engine attempt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING

from nico_contracts.events import MAX_RUN_TOKENS, RunEventSink
from nico_contracts.messages import AgentMessage

from .ports import ModelPort, ToolSnapshot
from .events import RunObserver

if TYPE_CHECKING:
    from .core import AgentRunTrace

MAX_MODEL_INPUT_CHARS = 64_000
MAX_TOOL_CALLS = 8
MAX_RUN_SECONDS = 120
MAX_MODEL_OUTPUT_CHARS = 12_000
MAX_MODEL_TURNS = 4


@dataclass(frozen=True)
class RunKey:
    namespace: str
    session_id: str
    version: int
    attempt: int = 0


@dataclass(frozen=True)
class RunPolicy:
    max_turns: int = MAX_MODEL_TURNS
    max_tool_calls: int = MAX_TOOL_CALLS
    timeout_seconds: float = MAX_RUN_SECONDS
    max_input_chars: int = MAX_MODEL_INPUT_CHARS
    max_output_chars: int = MAX_MODEL_OUTPUT_CHARS
    max_reported_tokens: int = MAX_RUN_TOKENS

    def __post_init__(self) -> None:
        integer_limits = (self.max_turns, self.max_tool_calls, self.max_input_chars,
                          self.max_output_chars, self.max_reported_tokens)
        if (any(type(value) is not int or value <= 0 for value in integer_limits)
                or type(self.timeout_seconds) not in (int, float)
                or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("run policy limits must be positive")


@dataclass(frozen=True)
class PreparedRun:
    key: RunKey
    model: ModelPort
    messages: tuple[AgentMessage, ...]
    tools: ToolSnapshot
    is_current: Callable[[], bool]
    policy: RunPolicy = field(default_factory=RunPolicy)
    on_event: RunEventSink | None = None
    observer: RunObserver | None = None
    trace: AgentRunTrace | None = None


@dataclass(frozen=True)
class RunOutcome:
    content: str
    trace: AgentRunTrace
