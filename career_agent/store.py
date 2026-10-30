import json
import sqlite3
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


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
                    max_questions INTEGER NOT NULL DEFAULT 3,
                    version INTEGER NOT NULL DEFAULT 0,
                    run_started INTEGER NOT NULL DEFAULT 0,
                    chat_memory TEXT NOT NULL DEFAULT '',
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
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
                );
                CREATE TABLE IF NOT EXISTS claims (
                    id TEXT PRIMARY KEY,
                    material_id TEXT REFERENCES materials(id),
                    text TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('draft', 'confirmed')),
                    created_at TEXT NOT NULL,
                    revoked_at TEXT
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
                CREATE TABLE IF NOT EXISTS interview_items (
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    number INTEGER NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT,
                    feedback TEXT,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (session_id, number)
                );
                CREATE TABLE IF NOT EXISTS interview_evidence (
                    session_id TEXT NOT NULL,
                    number INTEGER NOT NULL,
                    quote TEXT NOT NULL,
                    PRIMARY KEY (session_id, number, quote),
                    FOREIGN KEY (session_id, number) REFERENCES interview_items(session_id, number)
                );
                CREATE TABLE IF NOT EXISTS practice_sessions (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(id),
                    source_session_id TEXT NOT NULL REFERENCES sessions(id),
                    question_number INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS messages_session_id ON messages(session_id, id);
            """)
            db.execute("BEGIN IMMEDIATE")
            for table in ("materials", "claims"):
                columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
                if "revoked_at" not in columns:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN revoked_at TEXT")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(sessions)")}
            if "max_questions" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN max_questions INTEGER NOT NULL DEFAULT 3")
            if "run_started" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN run_started INTEGER NOT NULL DEFAULT 0")
            if "chat_memory" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN chat_memory TEXT NOT NULL DEFAULT ''")
            item_columns = {row["name"] for row in db.execute("PRAGMA table_info(interview_items)")}
            if "skipped" not in item_columns:
                db.execute("ALTER TABLE interview_items ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0")

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

    def backup(self) -> bytes:
        with TemporaryDirectory() as directory:
            target_path = Path(directory) / "backup.sqlite3"
            with self.connect() as source, closing(sqlite3.connect(target_path)) as target:
                source.backup(target)
            return target_path.read_bytes()

    def create(
        self, kind: str, goal: str = "", material_ids: tuple[str, ...] = (), claim_ids: tuple[str, ...] = (),
        max_questions: int = 3,
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
                "INSERT INTO sessions(id, kind, status, goal, max_questions, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, kind, status, goal, max_questions, now()),
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
            rows = db.execute("SELECT * FROM materials WHERE revoked_at IS NULL ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def replace_material(self, material_id: str, title: str, content: str) -> dict | None:
        new_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT kind FROM materials WHERE id = ? AND revoked_at IS NULL", (material_id,)).fetchone()
            if old is None:
                return None
            db.execute("UPDATE materials SET revoked_at = ? WHERE id = ?", (now(), material_id))
            db.execute("UPDATE claims SET revoked_at = ? WHERE material_id = ? AND revoked_at IS NULL", (now(), material_id))
            db.execute(
                "INSERT INTO materials(id, kind, title, content, created_at) VALUES (?, ?, ?, ?, ?)",
                (new_id, old["kind"], title, content, now()),
            )
        return self.get_material(new_id)

    def revoke_material(self, material_id: str) -> bool:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute(
                "UPDATE materials SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now(), material_id),
            ).rowcount
            if changed:
                db.execute("UPDATE claims SET revoked_at = ? WHERE material_id = ? AND revoked_at IS NULL", (now(), material_id))
        return bool(changed)

    def create_claim(self, text: str, material_id: str | None = None) -> dict:
        if material_id:
            material = self.get_material(material_id)
            if material is None or material["revoked_at"] or material["kind"] == "job_description":
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
            rows = db.execute("SELECT * FROM claims WHERE revoked_at IS NULL ORDER BY created_at DESC").fetchall()
        return [dict(row) for row in rows]

    def confirmed_claims(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, text, material_id FROM claims WHERE status = 'confirmed' "
                "AND revoked_at IS NULL ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def confirm_claim(self, claim_id: str) -> dict | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE claims SET status = 'confirmed' WHERE id = ? AND status = 'draft' AND revoked_at IS NULL",
                (claim_id,),
            ).rowcount
        return self.get_claim(claim_id) if changed else None

    def replace_claim(self, claim_id: str, text: str) -> dict | None:
        new_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT material_id FROM claims WHERE id = ? AND revoked_at IS NULL", (claim_id,)
            ).fetchone()
            if old is None:
                return None
            db.execute("UPDATE claims SET revoked_at = ? WHERE id = ?", (now(), claim_id))
            db.execute(
                "INSERT INTO claims(id, material_id, text, status, created_at) "
                "VALUES (?, ?, ?, 'draft', ?)", (new_id, old["material_id"], text, now()),
            )
        return self.get_claim(new_id)

    def revoke_claim(self, claim_id: str) -> bool:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE claims SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (now(), claim_id),
            ).rowcount
        return bool(changed)

    def interview_resources(self, session_id: str, include_content: bool = False) -> dict:
        with self.connect() as db:
            materials = db.execute("""SELECT m.id, m.kind, m.title, m.content
                FROM materials m JOIN session_materials sm ON sm.material_id = m.id
                JOIN sessions s ON s.id = sm.session_id
                WHERE s.id = ? AND s.kind = 'interview' ORDER BY m.created_at""", (session_id,)).fetchall()
            claims = db.execute("""SELECT c.id, c.text, c.material_id
                FROM claims c JOIN session_claims sc ON sc.claim_id = c.id
                JOIN sessions s ON s.id = sc.session_id
                WHERE s.id = ? AND s.kind = 'interview'
                ORDER BY c.created_at""", (session_id,)).fetchall()
        selected_materials = [dict(row) for row in materials]
        if not include_content:
            for material in selected_materials:
                del material["content"]
        return {"materials": selected_materials, "claims": [dict(row) for row in claims]}

    def interview_report(self, session_id: str) -> dict | None:
        session = self.get(session_id, "interview")
        if session is None:
            return None
        with self.connect() as db:
            rows = db.execute(
                "SELECT number, question, answer, feedback, skipped FROM interview_items "
                "WHERE session_id = ? ORDER BY number", (session_id,)
            ).fetchall()
            evidence = db.execute(
                "SELECT number, quote FROM interview_evidence WHERE session_id = ? ORDER BY number, quote",
                (session_id,),
            ).fetchall()
        messages = self.messages(session_id, "interview")
        summary = messages[-1]["content"] if session["status"] == "finished" and messages else None
        items = [dict(row) | {"evidence": [item["quote"] for item in evidence if item["number"] == row["number"]]}
                 for row in rows]
        return {"session_id": session_id, "goal": session["goal"], "status": session["status"],
                "items": items, "summary": summary, "practice_source": self.practice_source(session_id)}

    def practice_source(self, session_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("""SELECT p.source_session_id, p.question_number,
                    i.question, i.answer, i.feedback
                FROM practice_sessions p JOIN interview_items i
                  ON i.session_id = p.source_session_id AND i.number = p.question_number
                WHERE p.session_id = ?""", (session_id,)).fetchone()
        return dict(row) if row else None

    def create_practice(self, source_session_id: str, question_number: int) -> dict:
        session_id = uuid.uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT goal FROM sessions WHERE id = ? AND kind = 'interview' AND status IN ('finished', 'cancelled')",
                (source_session_id,),
            ).fetchone()
            item = db.execute(
                "SELECT 1 FROM interview_items WHERE session_id = ? AND number = ? AND feedback IS NOT NULL",
                (source_session_id, question_number),
            ).fetchone()
            if source is None or item is None:
                raise ValueError("只能复练已完成面试中有反馈的题目")
            db.execute(
                "INSERT INTO sessions(id, kind, status, goal, max_questions, created_at) "
                "VALUES (?, 'interview', 'new', ?, 1, ?)",
                (session_id, source["goal"], now()),
            )
            db.execute(
                "INSERT INTO session_materials(session_id, material_id) "
                "SELECT ?, material_id FROM session_materials WHERE session_id = ?",
                (session_id, source_session_id),
            )
            db.execute(
                "INSERT INTO session_claims(session_id, claim_id) "
                "SELECT ?, claim_id FROM session_claims WHERE session_id = ?",
                (session_id, source_session_id),
            )
            db.execute(
                "INSERT INTO practice_sessions(session_id, source_session_id, question_number) "
                "VALUES (?, ?, ?)", (session_id, source_session_id, question_number),
            )
        return self.get(session_id, "interview")

    def get(self, session_id: str, kind: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id = ? AND kind = ?", (session_id, kind)
            ).fetchone()
        session = dict(row) if row else None
        if session:
            session.pop("run_started")
        return session

    def list_sessions(self, kind: str) -> list[dict]:
        with self.connect() as db:
            if kind == "interview":
                rows = db.execute(
                    "SELECT s.*, EXISTS(SELECT 1 FROM interview_items i "
                    "WHERE i.session_id = s.id AND i.feedback IS NOT NULL) AS has_feedback "
                    "FROM sessions s WHERE s.kind = ? ORDER BY s.created_at DESC", (kind,)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM sessions WHERE kind = ? ORDER BY created_at DESC", (kind,)
                ).fetchall()
        sessions = [dict(row) for row in rows]
        for session in sessions:
            session.pop("run_started")
        return sessions

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
                          "session_claims", "messages", "events", "practice_sessions"):
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
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(session_id, kind, detail, created_at) "
                "SELECT id, ?, ?, ? FROM sessions WHERE id = ?",
                (kind, json.dumps(detail, ensure_ascii=False), now(), session_id),
            )

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
