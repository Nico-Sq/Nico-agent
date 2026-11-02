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
