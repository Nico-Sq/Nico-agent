"""Channel identity, inbound idempotency and delivery use cases."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from typing import Protocol

from nico_contracts.channels import ChannelResult, TextMessage
from nico_contracts.errors import ChannelAccessError, ChannelDeliveryError


class ChannelLedgerPort(Protocol):
    def get(self, session_id: str, kind: str) -> dict | None: ...
    def create(self, kind: str, *args, **kwargs) -> dict: ...
    def channel_binding(self, channel_name: str, conversation_id: str) -> dict | None: ...
    def bind_channel(self, channel_name: str, conversation_id: str,
                     sender_id: str, session_id: str, *, allow_rebind: bool = False) -> dict: ...
    def claim_inbound(self, channel_name: str, conversation_id: str, message_id: str,
                      sender_id: str, session_id: str, message_hash: str) -> tuple[bool, dict]: ...
    def channel_message(self, channel_name: str, conversation_id: str,
                        message_id: str) -> dict | None: ...
    def store_channel_reply(self, channel_name: str, conversation_id: str, message_id: str,
                            response: str, delivery_id: str) -> dict: ...
    def mark_delivered(self, delivery_id: str) -> None: ...
    def mark_delivery_failed(self, delivery_id: str, error: str) -> None: ...


ChannelHandler = Callable[[dict, TextMessage], Awaitable[str]]
ChannelSend = Callable[[str, str, str], Awaitable[None]]


class ChannelRouter:
    """Bind channel conversations and make model work/reply delivery durable."""

    def __init__(self, store: ChannelLedgerPort, channel_name: str,
                 allowed_senders: set[str], model_id: str = "default",
                 create_session: Callable[[str], dict] | None = None):
        self.store = store
        self.channel_name = channel_name
        self.allowed_senders = set(allowed_senders)
        self.model_id = model_id
        self.create_session = create_session
        self._lock = asyncio.Lock()

    def _check_sender(self, sender_id: str) -> None:
        if "*" not in self.allowed_senders and sender_id not in self.allowed_senders:
            raise ChannelAccessError("发送者没有接入权限")

    def bind(self, sender_id: str, conversation_id: str, session_id: str) -> dict:
        self._check_sender(sender_id)
        session = self.store.get(session_id, "chat")
        if session is None:
            raise ValueError("聊天会话不存在")
        binding = self.store.bind_channel(
            self.channel_name, conversation_id, sender_id, session_id, allow_rebind=True,
        )
        if binding["sender_id"] != sender_id or binding["session_id"] != session_id:
            raise ChannelAccessError("该渠道会话已属于其他用户")
        return session

    def session_id(self, sender_id: str, conversation_id: str) -> str:
        self._check_sender(sender_id)
        binding = self.store.channel_binding(self.channel_name, conversation_id)
        if binding is None:
            raise ValueError("渠道会话尚未绑定")
        if binding["sender_id"] != sender_id:
            raise ChannelAccessError("该渠道会话已属于其他用户")
        if binding["session_id"] is None:
            raise ValueError("渠道会话尚未绑定")
        return binding["session_id"]

    def ensure_session(self, sender_id: str, conversation_id: str) -> dict:
        self._check_sender(sender_id)
        binding = self.store.channel_binding(self.channel_name, conversation_id)
        if binding is not None and binding["sender_id"] != sender_id:
            raise ChannelAccessError("该渠道会话已属于其他用户")
        if binding is not None and binding["session_id"] is not None:
            session = self.store.get(binding["session_id"], "chat")
            if session is not None:
                return session
        created = (self.create_session(self.model_id) if self.create_session else
                   self.store.create("chat", model_id=self.model_id))
        binding = self.store.bind_channel(
            self.channel_name, conversation_id, sender_id, created["id"],
        )
        if binding["sender_id"] != sender_id:
            raise ChannelAccessError("该渠道会话已属于其他用户")
        if binding["session_id"] != created["id"]:
            session = self.store.get(binding["session_id"], "chat")
            if session is None:
                raise ValueError("渠道绑定的聊天会话不存在")
            return session
        return created

    async def _deliver(self, event: dict, conversation_id: str, channel_send: ChannelSend) -> None:
        try:
            await channel_send(conversation_id, event["response"], event["delivery_id"])
        except Exception as exc:
            self.store.mark_delivery_failed(event["delivery_id"], type(exc).__name__)
            raise ChannelDeliveryError(event["message_id"], event["delivery_id"]) from exc
        self.store.mark_delivered(event["delivery_id"])

    async def process(self, message: TextMessage, handler: ChannelHandler,
                      channel_send: ChannelSend) -> ChannelResult:
        self._check_sender(message.sender_id)
        async with self._lock:
            session = self.ensure_session(message.sender_id, message.conversation_id)
            message_hash = hashlib.sha256(message.text.encode("utf-8")).hexdigest()
            created, event = self.store.claim_inbound(
                self.channel_name, message.conversation_id, message.message_id,
                message.sender_id, session["id"], message_hash,
            )
            if (event["sender_id"] != message.sender_id
                    or event["session_id"] != session["id"]
                    or event["message_hash"] != message_hash):
                raise ChannelAccessError("该消息 ID 已属于其他内容或会话")
            if not created:
                if event["status"] == "completed":
                    return ChannelResult(session["id"], event["response"], duplicate=True)
                if event["response"] is None:
                    return ChannelResult(session["id"], "", duplicate=True, delivered=False)
            else:
                response = await handler(session, message)
                delivery_id = f"{self.channel_name}:{message.conversation_id}:{message.message_id}:reply"
                event = self.store.store_channel_reply(
                    self.channel_name, message.conversation_id, message.message_id,
                    response, delivery_id,
                )
            await self._deliver(event, message.conversation_id, channel_send)
            return ChannelResult(session["id"], event["response"], duplicate=not created)

    async def retry_delivery(self, message_id: str, conversation_id: str,
                             channel_send: ChannelSend, sender_id: str | None = None) -> ChannelResult:
        async with self._lock:
            event = self.store.channel_message(self.channel_name, conversation_id, message_id)
            if event is None or event["response"] is None:
                raise ValueError("消息不存在，无法重试投递")
            if sender_id is not None:
                self._check_sender(sender_id)
                if event["sender_id"] != sender_id:
                    raise ChannelAccessError("该渠道会话已属于其他用户")
            if event["status"] == "completed":
                return ChannelResult(event["session_id"], event["response"], duplicate=True)
            await self._deliver(event, conversation_id, channel_send)
            return ChannelResult(event["session_id"], event["response"], duplicate=True)
