"""Stable text-channel message and transport contracts."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TextMessage:
    sender_id: str
    conversation_id: str
    message_id: str
    text: str


class TextChannel(Protocol):
    async def receive(self) -> TextMessage | None: ...

    async def send(self, conversation_id: str, text: str, delivery_id: str) -> None: ...


@dataclass(frozen=True)
class ChannelResult:
    session_id: str
    response: str
    duplicate: bool = False
    delivered: bool = True
