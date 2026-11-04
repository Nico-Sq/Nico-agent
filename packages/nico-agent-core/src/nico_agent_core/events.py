"""Read-only observations for one engine attempt."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .run import RunKey


@dataclass(frozen=True)
class EngineEvent:
    key: RunKey | None
    sequence: int
    type: Literal["text_delta", "tool_start", "completed", "failed", "cancelled", "interrupted"]
    text: str = ""
    name: str = ""
    error_code: str | None = None


RunObserver = Callable[[EngineEvent], None]
