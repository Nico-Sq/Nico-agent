import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('chat', 'interview')),
                    status TEXT NOT NULL,
                    goal TEXT NOT NULL DEFAULT '',
                    question_count INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS materials (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL CHECK (kind IN ('resume', 'job_description', 'project')),
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    material_id TEXT REFERENCES materials(id),
                    text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS session_materials (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    material_id TEXT NOT NULL REFERENCES materials(id),
                    PRIMARY KEY (session_id, material_id)
                );
                CREATE TABLE IF NOT EXISTS session_claims (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    claim_id TEXT NOT NULL REFERENCES claims(id),
                    PRIMARY KEY (session_id, claim_id)
                );
                CREATE INDEX IF NOT EXISTS messages_session_id ON messages(session_id, id);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def create(
        self, kind: str, goal: str = "", material_ids: tuple[str, ...] = (), claim_ids: tuple[str, ...] = ()
    ) -> dict:
        session_id = uuid.uuid4().hex
        status = "active" if kind == "chat" else "new"
        with self.connect() as db:
            if kind == "interview":
                for material_id in set(material_ids):
                    if not db.execute("SELECT 1 FROM materials WHERE id = ?", (material_id,)).fetchone():
                        raise ValueError(f"材料不存在：{material_id}")
                for claim_id in set(claim_ids):
                    if not db.execute(
                        "SELECT 1 FROM claims WHERE id = ? AND status = 'confirmed'", (claim_id,)
                    ).fetchone():
                        raise ValueError(f"经历尚未确认或不存在：{claim_id}")
            db.execute(
                "INSERT INTO sessions(id, kind, status, goal, created_at) VALUES (?, ?, ?, ?, ?)",
                (session_id, kind, status, goal, now()),
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

    def create_material(self, kind: str, title: str, content: str) -> dict:
        material_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO materials(id, kind, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (material_id, kind, title, content, now()),
            )
        return self.get_material(material_id)

    def get_material(self, material_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM materials WHERE id = ?", (material_id,)).fetchone()
        return dict(row) if row else None

    def list_materials(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM materials ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def create_claim(self, text: str, material_id: str | None = None) -> dict:
        if material_id:
            material = self.get_material(material_id)
            if material is None or material["kind"] == "job_description":
                raise ValueError("经历来源必须是简历或项目材料")
        claim_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO claims(id, material_id, text, status, created_at) "
                "VALUES (?, ?, ?, 'draft', ?)",
                (claim_id, material_id, text, now()),
            )
        return self.get_claim(claim_id)

    def get_claim(self, claim_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        return dict(row) if row else None

    def list_claims(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM claims ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def confirm_claim(self, claim_id: str) -> dict | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE claims SET status = 'confirmed' WHERE id = ? AND status = 'draft'",
                (claim_id,),
            ).rowcount
        return self.get_claim(claim_id) if changed else None

    def interview_resources(self, session_id: str, include_content: bool = False) -> dict:
        with self.connect() as db:
            materials = db.execute("""SELECT m.id, m.kind, m.title, m.content
                FROM materials m JOIN session_materials sm ON sm.material_id = m.id
                JOIN sessions s ON s.id = sm.session_id
                WHERE s.id = ? AND s.kind = 'interview' ORDER BY m.created_at""", (session_id,)).fetchall()
            claims = db.execute("""SELECT c.id, c.text, c.material_id
                FROM claims c JOIN session_claims sc ON sc.claim_id = c.id
                JOIN sessions s ON s.id = sc.session_id
                WHERE s.id = ? AND s.kind = 'interview' AND c.status = 'confirmed'
                ORDER BY c.created_at""", (session_id,)).fetchall()
        selected_materials = [dict(row) for row in materials]
        if not include_content:
            for material in selected_materials:
                del material["content"]
        return {"materials": selected_materials, "claims": [dict(row) for row in claims]}

    def get(self, session_id: str, kind: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND kind = ?", (session_id, kind)
            ).fetchone()
        return dict(row) if row else None

    def list_sessions(self, kind: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM sessions WHERE kind = ? ORDER BY created_at DESC", (kind,)
            ).fetchall()
        return [dict(row) for row in rows]

    def messages(self, session_id: str, kind: str, limit: int | None = None) -> list[dict]:
        # The kind check is part of the query, so interview context cannot read chat rows.
        sql = """SELECT m.id, m.role, m.content, m.created_at FROM messages m
                 JOIN sessions s ON s.id = m.session_id
                 WHERE m.session_id = ? AND s.kind = ? ORDER BY m.id"""
        with self.connect() as db:
            rows = db.execute(sql, (session_id, kind)).fetchall()
        result = [dict(row) for row in rows]
        return result[-limit:] if limit else result

    def events(self, session_id: str, kind: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""SELECT e.kind, e.detail, e.created_at FROM events e
                JOIN sessions s ON s.id = e.session_id
                WHERE e.session_id = ? AND s.kind = ? ORDER BY e.id""", (session_id, kind)).fetchall()
        return [dict(row) for row in rows]

    def event(self, session_id: str, kind: str, detail: dict):
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(session_id, kind, detail, created_at) VALUES (?, ?, ?, ?)",
                (session_id, kind, json.dumps(detail, ensure_ascii=False), now()),
            )

    def claim_run(self, session_id: str, kind: str, expected: str, user_text: str | None = None) -> dict | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status = 'running', version = version + 1 "
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
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row)

    def finish_run(self, session_id: str, version: int, content: str, status: str, question_count: int) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE sessions SET status = ?, question_count = ? "
                "WHERE id = ? AND status = 'running' AND version = ?",
                (status, question_count, session_id, version),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO messages(session_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)",
                    (session_id, content, now()),
                )
        return bool(changed)

    def fail_run(self, session_id: str, version: int, kind: str, error: str):
        status = "active" if kind == "chat" else "paused"
        with self.connect() as db:
            changed = db.execute(
                "UPDATE sessions SET status = ? WHERE id = ? AND status = 'running' AND version = ?",
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
                "UPDATE sessions SET status = ?, version = version + 1 "
                "WHERE id = ? AND kind = ? AND status IN ('running', 'awaiting_answer')",
                (new_status, session_id, kind),
            ).rowcount
            if changed:
                db.execute(
                    "INSERT INTO events(session_id, kind, detail, created_at) VALUES (?, 'interrupt', '{}', ?)",
                    (session_id, now()),
                )
        return self.get(session_id, kind) if changed else None

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
                "UPDATE sessions SET status = ?, version = version + 1 WHERE id = ?",
                (status, session_id),
            )
        return self.get(session_id, "interview"), needs_run
