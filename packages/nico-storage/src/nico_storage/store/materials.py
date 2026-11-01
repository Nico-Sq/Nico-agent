from __future__ import annotations

import uuid

from .base import now


class MaterialRepositoryMixin:
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

    def confirmed_claims(self, query: str = "", offset: int = 0, limit: int = 20) -> list[dict]:
        where = "status = 'confirmed' AND revoked_at IS NULL"
        params: list[object] = []
        if query:
            where += " AND instr(lower(text), lower(?)) > 0"
            params.append(query)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT id, text, material_id FROM claims WHERE {where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
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
                "SELECT goal, model_id FROM sessions WHERE id = ? AND kind = 'interview' AND status IN ('finished', 'cancelled')",
                (source_session_id,),
            ).fetchone()
            item = db.execute(
                "SELECT 1 FROM interview_items WHERE session_id = ? AND number = ? AND feedback IS NOT NULL",
                (source_session_id, question_number),
            ).fetchone()
            if source is None or item is None:
                raise ValueError("只能复练已完成面试中有反馈的题目")
            db.execute(
                "INSERT INTO sessions(id, kind, status, goal, max_questions, model_id, created_at) "
                "VALUES (?, 'interview', 'new', ?, 1, ?, ?)",
                (session_id, source["goal"], source["model_id"], now()),
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
