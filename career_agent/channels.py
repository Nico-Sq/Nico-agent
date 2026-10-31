"""Channel adapters: the boundary between a conversation surface and the runtime.

The runtime only needs ``receive`` and ``send``, so another surface (a socket,
a message queue or a test double) can be added without touching the model loop.
"""

import asyncio
from typing import Protocol


class TextChannel(Protocol):
    async def receive(self) -> str | None: ...
    async def send(self, text: str) -> None: ...


class ConsoleChannel:
    """Read one line at a time from the terminal; /exit, /quit and EOF end it."""

    PROMPT = "你> "
    EXITS = ("/exit", "/quit")

    async def receive(self) -> str | None:
        try:
            value = await asyncio.to_thread(input, self.PROMPT)
        except (EOFError, KeyboardInterrupt):
            return None
        return None if value.strip() in self.EXITS else value

    async def send(self, text: str) -> None:
        print(text)
