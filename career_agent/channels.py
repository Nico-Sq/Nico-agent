"""Channel boundary with sender identity, idempotency, and delivery retry."""

import asyncio
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .store import Store


@dataclass(frozen=True)
class TextMessage:
    sender_id: str
    conversation_id: str
    message_id: str
    text: str


class TextChannel(Protocol):
    async def receive(self) -> TextMessage | None: ...
    async def send(self, conversation_id: str, text: str, delivery_id: str) -> None: ...


class ChannelAccessError(ValueError):
    pass


class ChannelDeliveryError(RuntimeError):
    def __init__(self, message_id: str, delivery_id: str):
        super().__init__("渠道投递失败，可以使用同一消息 ID 重试")
        self.message_id = message_id
        self.delivery_id = delivery_id


@dataclass(frozen=True)
class ChannelResult:
    session_id: str
    response: str
    duplicate: bool = False
    delivered: bool = True


class ChannelRouter:
    """Bind allowed senders to chat sessions and make message handling idempotent."""

    def __init__(self, store: Store, channel_name: str, allowed_senders: set[str],
                 model_id: str = "default", max_events: int = 1000):
        self.store = store
        self.channel_name = channel_name
        self.allowed_senders = set(allowed_senders)
        self.model_id = model_id
        self.max_events = max_events
        self.bindings: dict[tuple[str, str], tuple[str, str]] = {}
        self.events: OrderedDict[str, dict] = OrderedDict()
        self._lock = asyncio.Lock()

    def _check_sender(self, sender_id: str):
        if "*" not in self.allowed_senders and sender_id not in self.allowed_senders:
            raise ChannelAccessError("发送者没有接入权限")

    def bind(self, sender_id: str, conversation_id: str, session_id: str) -> dict:
        self._check_sender(sender_id)
        session = self.store.get(session_id, "chat")
        if session is None:
            raise ValueError("聊天会话不存在")
        key = self.channel_name, conversation_id
        existing = self.bindings.get(key)
        if existing and existing != (sender_id, session_id):
            raise ChannelAccessError("该渠道会话已属于其他用户")
        self.bindings[key] = sender_id, session_id
        return session

    def session_id(self, sender_id: str, conversation_id: str) -> str:
        self._check_sender(sender_id)
        binding = self.bindings.get((self.channel_name, conversation_id))
        if binding is None:
            raise ValueError("渠道会话尚未绑定")
        if binding[0] != sender_id:
            raise ChannelAccessError("该渠道会话已属于其他用户")
        return binding[1]

    def ensure_session(self, sender_id: str, conversation_id: str) -> dict:
        self._check_sender(sender_id)
        key = self.channel_name, conversation_id
        binding = self.bindings.get(key)
        if binding:
            if binding[0] != sender_id:
                raise ChannelAccessError("该渠道会话已属于其他用户")
            session = self.store.get(binding[1], "chat")
            if session is None:
                raise ValueError("渠道绑定的聊天会话不存在")
            return session
        session = self.store.create("chat", model_id=self.model_id)
        self.bindings[key] = sender_id, session["id"]
        return session

    async def _deliver(self, message_id: str, conversation_id: str, response: str,
                       channel_send: Callable[[str, str, str], Awaitable[None]]) -> None:
        event = self.events[message_id]
        try:
            await channel_send(conversation_id, response, event["delivery_id"])
        except Exception as exc:
            event["state"] = "delivery_failed"
            event["delivery_error"] = type(exc).__name__
            raise ChannelDeliveryError(message_id, event["delivery_id"]) from exc
        event["state"] = "completed"
        event["delivery_error"] = None

    async def process(self, message: TextMessage,
                      handler: Callable[[dict, TextMessage], Awaitable[str]],
                      channel_send: Callable[[str, str, str], Awaitable[None]]) -> ChannelResult:
        self._check_sender(message.sender_id)
        async with self._lock:
            session = self.ensure_session(message.sender_id, message.conversation_id)
            event = self.events.get(message.message_id)
            if event:
                if event["session_id"] != session["id"]:
                    raise ChannelAccessError("该消息 ID 已属于其他会话")
                if event["state"] == "completed":
                    return ChannelResult(session["id"], event["response"], duplicate=True)
                if event["state"] == "delivery_failed":
                    await self._deliver(
                        message.message_id, message.conversation_id, event["response"], channel_send
                    )
                    return ChannelResult(session["id"], event["response"], duplicate=True)
            else:
                response = await handler(session, message)
                event = {
                    "session_id": session["id"],
                    "response": response,
                    "delivery_id": f"{self.channel_name}:{message.message_id}:reply",
                    "state": "response_ready",
                    "delivery_error": None,
                }
                self.events[message.message_id] = event
                while len(self.events) > self.max_events:
                    self.events.popitem(last=False)
            await self._deliver(
                message.message_id, message.conversation_id, event["response"], channel_send
            )
            return ChannelResult(session["id"], event["response"])

    async def retry_delivery(self, message_id: str, conversation_id: str,
                             channel_send: Callable[[str, str, str], Awaitable[None]]) -> ChannelResult:
        async with self._lock:
            event = self.events.get(message_id)
            if event is None:
                raise ValueError("消息不存在，无法重试投递")
            if event["state"] == "completed":
                return ChannelResult(event["session_id"], event["response"], duplicate=True)
            await self._deliver(message_id, conversation_id, event["response"], channel_send)
            return ChannelResult(event["session_id"], event["response"], duplicate=True)


class ConsoleChannel:
    """Read one line at a time and attach the local user/conversation identity."""

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
