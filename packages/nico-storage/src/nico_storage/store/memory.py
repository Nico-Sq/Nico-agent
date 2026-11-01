from __future__ import annotations

import sqlite3
import uuid

from .base import normalize_memory_content, now


class MemoryRepositoryMixin:
    def add_memory(self, content: str) -> dict:
        """Save one memory; repeated text returns the existing row instead of a copy."""
        from . import MAX_MEMORIES

        content = normalize_memory_content(content)
        if not content or len(content) > 2000:
            raise ValueError("记忆内容需为 1–2000 字")
        with self.connect() as db:
            existing = db.execute("SELECT id, content FROM memories WHERE content = ?",
                                  (content,)).fetchone()
            if existing:
                return {"id": existing["id"], "content": existing["content"], "created": False}
            count = db.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            if count >= MAX_MEMORIES:
                raise ValueError(f"长期记忆已达 {MAX_MEMORIES} 条上限，请先删除不再需要的记忆")
            memory_id = uuid.uuid4().hex
            db.execute("INSERT INTO memories(id, content, created_at) VALUES (?, ?, ?)",
                       (memory_id, content, now()))
        return {"id": memory_id, "content": content, "created": True}

    def search_memories(self, query: str = "") -> list[dict]:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self.connect() as db:
            rows = db.execute("SELECT id, content, created_at FROM memories WHERE content LIKE ? ESCAPE '\\' "
                              "ORDER BY created_at DESC LIMIT 50", (f"%{escaped}%",)).fetchall()
        return [dict(row) for row in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self.connect() as db:
            return bool(db.execute("DELETE FROM memories WHERE id = ?", (memory_id,)).rowcount)

    @staticmethod
    def _memory_candidate(db: sqlite3.Connection, candidate_id: str) -> dict | None:
        row = db.execute(
            """SELECT c.*, s.kind AS session_kind FROM memory_candidates c
               JOIN sessions s ON s.id = c.session_id WHERE c.id = ?""",
            (candidate_id,),
        ).fetchone()
        if row is None:
            return None
        sources = db.execute(
            """SELECT m.id, m.role, m.content, m.created_at
               FROM memory_candidate_sources cs JOIN messages m ON m.id = cs.message_id
               WHERE cs.candidate_id = ? ORDER BY m.id""",
            (candidate_id,),
        ).fetchall()
        result = dict(row)
        result["sources"] = [dict(source) for source in sources]
        return result

    def memory_candidate(self, candidate_id: str) -> dict | None:
        with self.connect() as db:
            return self._memory_candidate(db, candidate_id)

    def list_memory_candidates(self, session_id: str | None = None,
                               status: str | None = None) -> list[dict]:
        clauses = ["1 = 1"]
        parameters = []
        if session_id is not None:
            clauses.append("session_id = ?")
            parameters.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM memory_candidates WHERE " + " AND ".join(clauses)
                + " ORDER BY created_at DESC LIMIT 100",
                parameters,
            ).fetchall()
            return [self._memory_candidate(db, row["id"]) for row in rows]

    def create_memory_candidate(self, session_id: str, content: str,
                                source_message_ids: list[int],
                                expected_version: int | None = None) -> dict:
        from . import MAX_MEMORY_CANDIDATES

        content = normalize_memory_content(content)
        if not content or len(content) > 2000:
            raise ValueError("候选记忆需为 1–2000 字")
        source_ids = list(dict.fromkeys(source_message_ids))
        if not source_ids or len(source_ids) > 16:
            raise ValueError("候选记忆需要 1–16 条来源消息")
        with self.connect() as db:
            session = db.execute(
                "SELECT kind, version, status, run_started FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session is None or session["kind"] != "chat":
                raise ValueError("候选记忆只能来自聊天会话")
            if expected_version is not None and (
                session["version"] != expected_version
                or session["status"] != "running" or not session["run_started"]
            ):
                raise ValueError("会话运行已失效")
            placeholders = ",".join("?" for _ in source_ids)
            found = db.execute(
                f"""SELECT id FROM messages WHERE session_id = ? AND id IN ({placeholders})""",
                [session_id, *source_ids],
            ).fetchall()
            if len(found) != len(source_ids):
                raise ValueError("来源消息必须属于当前聊天会话")
            existing = db.execute(
                """SELECT id FROM memory_candidates
                   WHERE session_id = ? AND status = 'pending' AND content = ?""",
                (session_id, content),
            ).fetchone()
            if existing:
                return {**self._memory_candidate(db, existing["id"]), "created": False}
            if db.execute(
                "SELECT COUNT(*) FROM memory_candidates WHERE status = 'pending'"
            ).fetchone()[0] >= MAX_MEMORY_CANDIDATES:
                raise ValueError(f"待确认记忆已达 {MAX_MEMORY_CANDIDATES} 条上限，请先处理")
            candidate_id = uuid.uuid4().hex
            timestamp = now()
            db.execute(
                """INSERT INTO memory_candidates
                   (id, session_id, content, status, memory_id, created_at, updated_at, confirmed_at)
                   VALUES (?, ?, ?, 'pending', NULL, ?, ?, NULL)""",
                (candidate_id, session_id, content, timestamp, timestamp),
            )
            db.executemany(
                "INSERT INTO memory_candidate_sources(candidate_id, message_id) VALUES (?, ?)",
                [(candidate_id, message_id) for message_id in source_ids],
            )
            result = self._memory_candidate(db, candidate_id)
        return {**result, "created": True}

    def update_memory_candidate(self, candidate_id: str, content: str) -> dict | None:
        content = normalize_memory_content(content)
        if not content or len(content) > 2000:
            raise ValueError("候选记忆需为 1–2000 字")
        with self.connect() as db:
            changed = db.execute(
                """UPDATE memory_candidates SET content = ?, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (content, now(), candidate_id),
            ).rowcount
            if not changed:
                return None
            return self._memory_candidate(db, candidate_id)

    def confirm_memory_candidate(self, candidate_id: str) -> dict | None:
        from . import MAX_MEMORIES

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            candidate = self._memory_candidate(db, candidate_id)
            if candidate is None:
                return None
            if candidate["status"] == "confirmed":
                if candidate["memory_id"]:
                    candidate["memory"] = {
                        "id": candidate["memory_id"], "content": candidate["content"],
                        "created": False,
                    }
                return candidate
            existing = db.execute(
                "SELECT id, content FROM memories WHERE content = ?",
                (candidate["content"],),
            ).fetchone()
            if existing:
                memory_id = existing["id"]
                created = False
            else:
                if db.execute("SELECT COUNT(*) FROM memories").fetchone()[0] >= MAX_MEMORIES:
                    raise ValueError(f"长期记忆已达 {MAX_MEMORIES} 条上限，请先删除不再需要的记忆")
                memory_id = uuid.uuid4().hex
                db.execute(
                    "INSERT INTO memories(id, content, created_at) VALUES (?, ?, ?)",
                    (memory_id, candidate["content"], now()),
                )
                created = True
            confirmed_at = now()
            db.execute(
                """UPDATE memory_candidates
                   SET status = 'confirmed', memory_id = ?, updated_at = ?, confirmed_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (memory_id, confirmed_at, confirmed_at, candidate_id),
            )
            result = self._memory_candidate(db, candidate_id)
            result["memory"] = {"id": memory_id, "content": candidate["content"],
                                "created": created}
            return result

    def delete_memory_candidate(self, candidate_id: str) -> bool:
        with self.connect() as db:
            return bool(db.execute(
                "DELETE FROM memory_candidates WHERE id = ? AND status = 'pending'",
                (candidate_id,),
            ).rowcount)
