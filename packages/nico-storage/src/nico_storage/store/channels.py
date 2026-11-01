from __future__ import annotations

from .base import now


class ChannelRepositoryMixin:
    def channel_binding(self, channel_name: str, conversation_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM channel_bindings WHERE channel_name = ? AND conversation_id = ?",
                (channel_name, conversation_id),
            ).fetchone()
        return dict(row) if row else None

    def bind_channel(self, channel_name: str, conversation_id: str,
                     sender_id: str, session_id: str, *, allow_rebind: bool = False) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO channel_bindings "
                "(channel_name, conversation_id, sender_id, session_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (channel_name, conversation_id, sender_id, session_id, now()),
            )
            if allow_rebind:
                db.execute(
                    "UPDATE channel_bindings SET session_id = ? "
                    "WHERE channel_name = ? AND conversation_id = ? AND sender_id = ?",
                    (session_id, channel_name, conversation_id, sender_id),
                )
            else:
                db.execute(
                    "UPDATE channel_bindings SET session_id = ? "
                    "WHERE channel_name = ? AND conversation_id = ? AND sender_id = ? AND session_id IS NULL",
                    (session_id, channel_name, conversation_id, sender_id),
                )
            row = db.execute(
                "SELECT * FROM channel_bindings WHERE channel_name = ? AND conversation_id = ?",
                (channel_name, conversation_id),
            ).fetchone()
        return dict(row)

    def claim_inbound(self, channel_name: str, conversation_id: str, message_id: str,
                      sender_id: str, session_id: str, message_hash: str) -> tuple[bool, dict]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            created_at = now()
            cursor = db.execute(
                "INSERT OR IGNORE INTO channel_messages "
                "(channel_name, conversation_id, message_id, sender_id, session_id, message_hash, "
                "status, response, delivery_id, delivery_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'processing', NULL, NULL, NULL, ?, ?)",
                (channel_name, conversation_id, message_id, sender_id, session_id,
                 message_hash, created_at, created_at),
            )
            row = db.execute(
                "SELECT * FROM channel_messages WHERE channel_name = ? AND conversation_id = ? AND message_id = ?",
                (channel_name, conversation_id, message_id),
            ).fetchone()
        return cursor.rowcount == 1, dict(row)

    def channel_message(self, channel_name: str, conversation_id: str,
                        message_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM channel_messages "
                "WHERE channel_name = ? AND conversation_id = ? AND message_id = ?",
                (channel_name, conversation_id, message_id),
            ).fetchone()
        return dict(row) if row else None

    def store_channel_reply(self, channel_name: str, conversation_id: str, message_id: str,
                            response: str, delivery_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE channel_messages SET response = ?, delivery_id = ?, status = 'response_ready', "
                "delivery_error = NULL, updated_at = ? "
                "WHERE channel_name = ? AND conversation_id = ? AND message_id = ? AND status = 'processing'",
                (response, delivery_id, now(), channel_name, conversation_id, message_id),
            ).rowcount
            row = db.execute(
                "SELECT * FROM channel_messages WHERE channel_name = ? AND conversation_id = ? AND message_id = ?",
                (channel_name, conversation_id, message_id),
            ).fetchone()
            if not changed and (row is None or row["response"] != response or row["delivery_id"] != delivery_id):
                raise ValueError("渠道消息不能保存第二个回复")
        return dict(row)

    def mark_delivered(self, delivery_id: str) -> None:
        self._update_delivery(delivery_id, "completed", None)

    def mark_delivery_failed(self, delivery_id: str, error: str) -> None:
        self._update_delivery(delivery_id, "delivery_failed", error)

    def _update_delivery(self, delivery_id: str, status: str, error: str | None) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE channel_messages SET status = ?, delivery_error = ?, updated_at = ? "
                "WHERE delivery_id = ? AND status IN ('response_ready', 'delivery_failed')",
                (status, error, now(), delivery_id),
            ).rowcount
            row = db.execute(
                "SELECT status FROM channel_messages WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            if row is None or (not changed and row["status"] != "completed"):
                raise ValueError("渠道投递记录不存在")
