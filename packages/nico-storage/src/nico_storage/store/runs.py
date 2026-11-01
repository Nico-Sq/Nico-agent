from __future__ import annotations

import json

from .base import now


class RunRepositoryMixin:
    def claim_run(self, session_id: str, kind: str, expected: str, user_text: str | None = None,
                  expected_question: int | None = None) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if kind == "chat" and user_text is not None:
                changed = db.execute(
                    "UPDATE sessions SET status = 'running', version = version + 1, run_started = 0 "
                    "WHERE id = ? AND kind = 'chat' AND status IN ('active', 'running')",
                    (session_id,),
                ).rowcount
            elif kind == "interview" and user_text is not None and expected_question is not None:
                changed = db.execute(
                    """UPDATE sessions SET status = 'running', version = version + 1, run_started = 0
                    WHERE id = ? AND kind = 'interview' AND question_count = ?
                      AND (status = 'awaiting_answer' OR
                           (status = 'running' AND EXISTS (
                               SELECT 1 FROM interview_items
                               WHERE session_id = ? AND number = ? AND answer IS NOT NULL)))""",
                    (session_id, expected_question, session_id, expected_question),
                ).rowcount
            else:
                changed = db.execute(
                    "UPDATE sessions SET status = 'running', version = version + 1, run_started = 0 "
                    "WHERE id = ? AND kind = ? AND status = ?",
                    (session_id, kind, expected),
                ).rowcount
            if not changed:
                return None
            if user_text is not None:
                db.execute(
                    "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, 'user', ?, ?)",
                    (session_id, user_text, now()),
                )
                if kind == "interview":
                    changed_answer = db.execute(
                        "UPDATE interview_items SET answer = ? WHERE session_id = ? "
                        "AND number = (SELECT question_count FROM sessions WHERE id = ?)",
                        (user_text, session_id, session_id),
                    ).rowcount
                    if changed_answer != 1:
                        raise ValueError("当前题目记录缺失")
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        session = dict(row)
        session.pop("run_started")
        return session

    def skip_interview_question(self, session_id: str, question_number: int) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status = 'running', version = version + 1, run_started = 0 "
                "WHERE id = ? AND kind = 'interview' AND status = 'awaiting_answer' AND question_count = ?",
                (session_id, question_number),
            ).rowcount
            if not changed:
                return None
            db.execute(
                "UPDATE interview_items SET skipped = 1 WHERE session_id = ? AND number = ?",
                (session_id, question_number),
            )
            db.execute(
                "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, 'user', '[SKIPPED_QUESTION]', ?)",
                (session_id, now()),
            )
            db.execute(
                "INSERT INTO events(session_id, kind, detail, created_at) VALUES (?, 'skip', ?, ?)",
                (session_id, json.dumps({"question_number": question_number}), now()),
            )
        return self.get(session_id, "interview")

    def begin_run(self, session_id: str, version: int) -> bool:
        """原子认领一次模型运行，阻止同一版本的重复请求再次计费。"""
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET run_started = 1 "
                "WHERE id = ? AND version = ? AND status = 'running' AND run_started = 0",
                (session_id, version),
            ).rowcount
        return bool(changed)

    def set_chat_memory(self, session_id: str, text: str) -> dict | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET chat_memory = ?, status = 'active', "
                "version = version + 1, run_started = 0 WHERE id = ? AND kind = 'chat'",
                (text, session_id),
            ).rowcount
        return self.get(session_id, "chat") if changed else None

    def retry_chat(self, session_id: str) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status = 'running', version = version + 1, run_started = 0 "
                "WHERE id = ? AND kind = 'chat' AND status = 'active' "
                "AND (SELECT role FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1) = 'user'",
                (session_id, session_id),
            ).rowcount
        return self.get(session_id, "chat") if changed else None

    def run_current(self, session_id: str, version: int) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM sessions WHERE id = ? AND version = ? "
                "AND status = 'running' AND run_started = 1",
                (session_id, version),
            ).fetchone()
        return row is not None

    def chat_authorizes(self, session_id: str, version: int, target: str) -> bool:
        """授权只依据该版本的当前用户输入，不借用后续消息。"""
        with self.connect() as db:
            row = db.execute(
                """SELECT m.content FROM messages m JOIN sessions s ON s.id = m.session_id
                   WHERE s.id = ? AND s.kind = 'chat' AND s.version = ?
                     AND s.status = 'running' AND m.role = 'user'
                   ORDER BY m.id DESC LIMIT 1""",
                (session_id, version),
            ).fetchone()
        return row is not None and target in row["content"]

    def finish_run(
        self, session_id: str, version: int, content: str, status: str, question_count: int,
        question: str | None = None, feedback: str | None = None, evidence: tuple[str, ...] = (),
    ) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            before = db.execute(
                "SELECT kind, question_count FROM sessions WHERE id = ? AND status = 'running' AND version = ?",
                (session_id, version),
            ).fetchone()
            changed = db.execute(
                "UPDATE sessions SET status = ?, question_count = ?, run_started = 0 "
                "WHERE id = ? AND status = 'running' AND version = ?",
                (status, question_count, session_id, version),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                    (session_id, content, now()),
                )
                if before["kind"] == "interview":
                    if before["question_count"]:
                        db.execute(
                            "UPDATE interview_items SET feedback = ? WHERE session_id = ? AND number = ?",
                            (feedback, session_id, before["question_count"]),
                        )
                        db.executemany(
                            "INSERT INTO interview_evidence(session_id, number, quote) VALUES (?, ?, ?)",
                            [(session_id, before["question_count"], quote) for quote in evidence],
                        )
                    if question:
                        db.execute(
                            "INSERT INTO interview_items(session_id, number, question) VALUES (?, ?, ?)",
                            (session_id, question_count, question),
                        )
        return bool(changed)

    def fail_run(self, session_id: str, version: int, kind: str, error: str):
        status = "active" if kind == "chat" else "paused"
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET status = ?, run_started = 0 "
                "WHERE id = ? AND status = 'running' AND version = ?",
                (status, session_id, version),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO events(session_id, kind, detail, created_at) VALUES (?, 'error', ?, ?)",
                    (session_id, json.dumps({"message": error}, ensure_ascii=False), now()),
                )

    def interrupt(self, session_id: str, kind: str) -> dict | None:
        new_status = "active" if kind == "chat" else "paused"
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET status = ?, version = version + 1, run_started = 0 "
                "WHERE id = ? AND kind = ? AND status IN ('running', 'awaiting_answer')",
                (new_status, session_id, kind),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO events(session_id, kind, detail, created_at) VALUES (?, 'interrupt', '{}', ?)",
                    (session_id, now()),
                )
        return self.get(session_id, kind) if changed else None

    def cancel_interview(self, session_id: str) -> dict | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET status = 'cancelled', version = version + 1, run_started = 0 "
                "WHERE id = ? AND kind = 'interview' "
                "AND status IN ('new', 'running', 'awaiting_answer', 'paused')",
                (session_id,),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO events(session_id, kind, detail, created_at) VALUES (?, 'cancel', '{}', ?)",
                    (session_id, now()),
                )
        return self.get(session_id, "interview") if changed else None

    def resume(self, session_id: str) -> tuple[dict, bool] | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND kind = 'interview' AND status = 'paused'",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            last = db.execute(
                "SELECT role FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            needs_run = last is None or last["role"] == "user"
            status = "running" if needs_run else "awaiting_answer"
            db.execute(
                "UPDATE sessions SET status = ?, version = version + 1, run_started = 0 WHERE id = ?",
                (status, session_id),
            )
        return self.get(session_id, "interview"), needs_run
