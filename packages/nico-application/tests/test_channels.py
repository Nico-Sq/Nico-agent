import asyncio
import copy
import hashlib
import unittest

from nico_application import ChannelRouter
from nico_contracts.channels import TextMessage
from nico_contracts.errors import ChannelAccessError, ChannelDeliveryError


class FakeLedger:
    def __init__(self):
        self.sessions = {}
        self.bindings = {}
        self.messages = {}

    def get(self, session_id, kind):
        session = self.sessions.get(session_id)
        return copy.deepcopy(session) if session and session["kind"] == kind else None

    def create(self, kind, model_id="default"):
        session = {"id": f"session-{len(self.sessions) + 1}", "kind": kind,
                   "status": "active", "model_id": model_id}
        self.sessions[session["id"]] = session
        return copy.deepcopy(session)

    def channel_binding(self, channel_name, conversation_id):
        value = self.bindings.get((channel_name, conversation_id))
        return copy.deepcopy(value)

    def bind_channel(self, channel_name, conversation_id, sender_id, session_id,
                     *, allow_rebind=False):
        key = channel_name, conversation_id
        self.bindings.setdefault(key, {
            "channel_name": channel_name, "conversation_id": conversation_id,
            "sender_id": sender_id, "session_id": session_id,
        })
        value = self.bindings[key]
        if (value["session_id"] is None or allow_rebind) and value["sender_id"] == sender_id:
            value["session_id"] = session_id
        return copy.deepcopy(value)

    def claim_inbound(self, channel_name, conversation_id, message_id,
                      sender_id, session_id, message_hash):
        key = channel_name, conversation_id, message_id
        created = key not in self.messages
        self.messages.setdefault(key, {
            "channel_name": channel_name, "conversation_id": conversation_id,
            "message_id": message_id, "sender_id": sender_id, "session_id": session_id,
            "message_hash": message_hash, "status": "processing", "response": None,
            "delivery_id": None, "delivery_error": None,
        })
        return created, copy.deepcopy(self.messages[key])

    def channel_message(self, channel_name, conversation_id, message_id):
        value = self.messages.get((channel_name, conversation_id, message_id))
        return copy.deepcopy(value)

    def store_channel_reply(self, channel_name, conversation_id, message_id, response, delivery_id):
        value = self.messages[(channel_name, conversation_id, message_id)]
        value.update(response=response, delivery_id=delivery_id, status="response_ready")
        return copy.deepcopy(value)

    def mark_delivered(self, delivery_id):
        for value in self.messages.values():
            if value["delivery_id"] == delivery_id:
                value.update(status="completed", delivery_error=None)
                return

    def mark_delivery_failed(self, delivery_id, error):
        for value in self.messages.values():
            if value["delivery_id"] == delivery_id:
                value.update(status="delivery_failed", delivery_error=error)
                return


class ChannelUseCaseTests(unittest.TestCase):
    def setUp(self):
        self.ledger = FakeLedger()
        self.router = ChannelRouter(self.ledger, "console", {"local"}, "demo")
        self.message = TextMessage("local", "console", "message-1", "你好")

    def test_restart_duplicate_does_not_repeat_handler_or_delivery(self):
        calls = []
        sends = []

        async def handle(session, message):
            calls.append(message.message_id)
            return "回复"

        async def send(conversation_id, text, delivery_id):
            sends.append(delivery_id)

        first = asyncio.run(self.router.process(self.message, handle, send))
        restarted = ChannelRouter(self.ledger, "console", {"local"}, "demo")
        duplicate = asyncio.run(restarted.process(self.message, handle, send))
        self.assertEqual(first.response, "回复")
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(calls, ["message-1"])
        self.assertEqual(len(sends), 1)

    def test_delivery_retry_after_restart_reuses_reply_and_delivery_id(self):
        calls = []
        delivery_ids = []

        async def handle(session, message):
            calls.append(message.message_id)
            return "只生成一次"

        async def fail(conversation_id, text, delivery_id):
            delivery_ids.append(delivery_id)
            raise ConnectionError("offline")

        async def succeed(conversation_id, text, delivery_id):
            delivery_ids.append(delivery_id)

        with self.assertRaises(ChannelDeliveryError):
            asyncio.run(self.router.process(self.message, handle, fail))
        restarted = ChannelRouter(self.ledger, "console", {"local"}, "demo")
        result = asyncio.run(restarted.process(self.message, handle, succeed))
        self.assertTrue(result.duplicate)
        self.assertEqual(calls, ["message-1"])
        self.assertEqual(delivery_ids[0], delivery_ids[1])

    def test_claimed_message_with_unknown_result_is_not_run_again(self):
        session = self.router.ensure_session("local", "console")
        self.ledger.claim_inbound("console", "console", "message-1", "local",
                                  session["id"], hashlib.sha256(
                                      self.message.text.encode("utf-8")
                                  ).hexdigest())
        calls = []

        async def handle(current, message):
            calls.append(message.message_id)
            return "不应生成"

        async def send(*_):
            self.fail("没有可投递的回复")

        result = asyncio.run(self.router.process(self.message, handle, send))
        self.assertTrue(result.duplicate)
        self.assertFalse(result.delivered)
        self.assertEqual(result.response, "")
        self.assertEqual(calls, [])

    def test_binding_and_message_identity_cannot_be_taken_over(self):
        self.router.ensure_session("local", "console")
        with self.assertRaises(ChannelAccessError):
            self.router.ensure_session("other", "console")

        async def handle(session, message):
            return "回复"

        async def send(*_):
            pass

        asyncio.run(self.router.process(self.message, handle, send))
        changed = TextMessage("local", "console", "message-1", "不同内容")
        with self.assertRaises(ChannelAccessError):
            asyncio.run(self.router.process(changed, handle, send))


if __name__ == "__main__":
    unittest.main()
