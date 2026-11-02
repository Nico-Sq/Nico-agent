from __future__ import annotations

import uuid

from .base import now


class InterviewRepositoryMixin:
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
