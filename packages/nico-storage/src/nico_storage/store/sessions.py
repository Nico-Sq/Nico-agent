from __future__ import annotations

import json
import uuid

from .base import now


class SessionRepositoryMixin:
    def create(
        self, kind: str, goal: str = "", material_ids: tuple[str, ...] = (), claim_ids: tuple[str, ...] = (),
        max_questions: int = 3, model_id: str = "default",
    ) -> dict:
        if kind == "interview" and not 1 <= max_questions <= 10:
            raise ValueError("面试题数必须在 1 到 10 之间")
        session_id = uuid.uuid4().hex
        status = "active" if kind == "chat" else "new"
        with self.connect() as db:
            if kind == "interview":
                for material_id in set(material_ids):
                    if not db.execute("SELECT 1 FROM materials WHERE id = ? AND revoked_at IS NULL", (material_id,)).fetchone():
                        raise ValueError(f"材料不存在或已撤回：{material_id}")
                for claim_id in set(claim_ids):
                    if not db.execute(
                        "SELECT 1 FROM claims WHERE id = ? AND status = 'confirmed' AND revoked_at IS NULL", (claim_id,)
                    ).fetchone():
                        raise ValueError(f"经历尚未确认或不存在：{claim_id}")
            db.execute(
                "INSERT INTO sessions(id, kind, status, goal, max_questions, model_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, kind, status, goal, max_questions, model_id, now()),
            )
            if kind == "interview":
                db.executemany(
                    "INSERT INTO session_materials(session_id, material_id) VALUES (?, ?)",
                    [(session_id, item) for item in set(material_ids)],
                )
                db.executemany(
                    "INSERT INTO session_claims(session_id, claim_id) VALUES (?, ?)",
                    [(session_id, item) for item in set(claim_ids)],
                )
        return self.get(session_id, kind)

    def get(self, session_id: str, kind: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND kind = ?", (session_id, kind)
            ).fetchone()
        session = dict(row) if row else None
        if session:
            session.pop("run_started")
        return session

    def list_sessions(self, kind: str, query: str = "") -> list[dict]:
        where = "s.kind = ?"
        params = [kind]
        if query.strip():
            match = "instr(lower({field}), lower(?)) > 0"
            conditions = [match.format(field=field) for field in ("s.id", "s.title", "s.goal")]
            conditions.append("EXISTS(SELECT 1 FROM messages m WHERE m.session_id = s.id AND "
                              + match.format(field="m.content") + ")")
            if kind == "interview":
                conditions.append("EXISTS(SELECT 1 FROM interview_items i WHERE i.session_id = s.id AND ("
                                  + " OR ".join(match.format(field=f"i.{field}") for field in
                                                ("question", "answer", "feedback")) + "))")
            where += " AND (" + " OR ".join(conditions) + ")"
            params.extend([query.strip()] * (7 if kind == "interview" else 4))
        with self.connect() as db:
            if kind == "interview":
                rows = db.execute(
                    "SELECT s.*, EXISTS(SELECT 1 FROM interview_items i "
                    "WHERE i.session_id = s.id AND i.feedback IS NOT NULL) AS has_feedback "
                    f"FROM sessions s WHERE {where} ORDER BY s.created_at DESC", params
                ).fetchall()
            else:
                rows = db.execute(
                    f"SELECT s.* FROM sessions s WHERE {where} ORDER BY s.created_at DESC", params
                ).fetchall()
        sessions = [dict(row) for row in rows]
        for session in sessions:
            session.pop("run_started")
            session.pop("chat_memory")
        return sessions

    def set_session_title(self, session_id: str, kind: str, title: str) -> dict | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET title = ? WHERE id = ? AND kind = ?",
                (title, session_id, kind),
            ).rowcount
        return self.get(session_id, kind) if changed else None

    def tool_grants(self, session_id: str) -> list[str]:
        with self.connect() as db:
            rows = db.execute("SELECT tool_name FROM session_tool_grants WHERE session_id = ? ORDER BY tool_name",
                              (session_id,)).fetchall()
        return [row["tool_name"] for row in rows]

    def set_tool_grants(self, session_id: str, names: list[str]) -> list[str]:
        with self.connect() as db:
            db.execute("DELETE FROM session_tool_grants WHERE session_id = ?", (session_id,))
            db.executemany("INSERT INTO session_tool_grants(session_id, tool_name) VALUES (?, ?)",
                           [(session_id, name) for name in sorted(set(names))])
        return self.tool_grants(session_id)

    def delete_session(self, session_id: str, kind: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""WITH RECURSIVE related(id) AS (
                    SELECT id FROM sessions WHERE id = ? AND kind = ?
                    UNION ALL
                    SELECT p.session_id FROM practice_sessions p JOIN related r ON p.source_session_id = r.id
                ) SELECT id FROM related""", (session_id, kind)).fetchall()
            ids = [row["id"] for row in rows]
            if not ids:
                return False
            for table in ("interview_evidence", "interview_items", "session_materials",
                          "session_claims", "session_tool_grants", "messages", "events", "practice_sessions"):
                db.executemany(f"DELETE FROM {table} WHERE session_id = ?", [(item,) for item in ids])
            db.executemany("DELETE FROM sessions WHERE id = ?", [(item,) for item in ids])
        return True

    def messages(self, session_id: str, kind: str, limit: int | None = None) -> list[dict]:
        # The kind check is part of the query, so interview context cannot read chat rows.
        sql = """SELECT m.id, m.role, m.content, m.created_at FROM messages m
                 JOIN sessions s ON s.id = m.session_id
                 WHERE m.session_id = ? AND s.kind = ?"""
        with self.connect() as db:
            if limit:
                rows = db.execute(sql + " ORDER BY m.id DESC LIMIT ?",
                                  (session_id, kind, limit)).fetchall()
                return [dict(row) for row in reversed(rows)]
            rows = db.execute(sql + " ORDER BY m.id", (session_id, kind)).fetchall()
        return [dict(row) for row in rows]

    def events(self, session_id: str, kind: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT e.kind, e.detail, e.created_at FROM events e
                JOIN sessions s ON s.id = e.session_id
                WHERE e.session_id = ? AND s.kind = ? ORDER BY e.id""", (session_id, kind)).fetchall()
        return [dict(row) for row in rows]

    def event(self, session_id: str, kind: str, detail: dict):
        from . import MAX_EVENTS_PER_SESSION

        with self.connect() as db:
            db.execute(
                "INSERT INTO events(session_id, kind, detail, created_at) "
                "SELECT id, ?, ?, ? FROM sessions WHERE id = ?",
                (kind, json.dumps(detail, ensure_ascii=False), now(), session_id),
            )
            db.execute(
                "DELETE FROM events WHERE session_id = ? AND id NOT IN ("
                "SELECT id FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, MAX_EVENTS_PER_SESSION),
            )
