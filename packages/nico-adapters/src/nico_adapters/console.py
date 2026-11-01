"""The local interactive console text channel."""

import asyncio
import uuid

from nico_contracts.channels import TextMessage


class ConsoleChannel:
    name = "console"
    sender_id = "local"
    conversation_id = "console"
    allowed_senders = frozenset({sender_id})
    PROMPT = "你> "
    EXITS = ("/exit", "/quit")

    def __init__(self):
        self.sent_delivery_ids = set()

    async def receive(self) -> TextMessage | None:
        try:
            value = await asyncio.to_thread(input, self.PROMPT)
        except (EOFError, KeyboardInterrupt):
            return None
        if value.strip() in self.EXITS:
            return None
        return TextMessage(self.sender_id, self.conversation_id, uuid.uuid4().hex, value)

    async def send(self, conversation_id: str, text: str, delivery_id: str) -> None:
        if delivery_id in self.sent_delivery_ids:
            return
        print(text)
        self.sent_delivery_ids.add(delivery_id)
