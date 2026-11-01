from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Literal

from .schema import (
    AUTH_SCHEMA_VERSION,
    BUSINESS_SCHEMA_VERSION,
    DatabaseSchemaError,
    schema_version,
    validate_schema,
)


DatabaseKind = Literal["business", "auth"]


@dataclass(frozen=True)
class Migration:
    version: int
    sql: str


_BUSINESS_MIGRATIONS = (
    Migration(1, resources.files("nico_storage.migrations").joinpath("001_business.sql").read_text(encoding="utf-8")),
)
_AUTH_MIGRATIONS = (
    Migration(1, resources.files("nico_storage.migrations").joinpath("001_auth.sql").read_text(encoding="utf-8")),
)


def migrate_database(path: Path, kind: DatabaseKind, *, backup: bool = True) -> None:
    """Validate and migrate a database copy before atomically replacing the original."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    migrations = _migrations(kind)
    current_version = _current_version(kind)

    if not path.exists() or path.stat().st_size == 0:
        _migrate_new_database(path, kind, migrations)
        return

    with closing(_readonly(path)) as original:
        version = validate_schema(original, kind, allow_legacy=True)
        if version == current_version:
            return

        temporary = path.with_name(f".{path.name}.migration-{uuid.uuid4().hex}.tmp")
        try:
            with closing(sqlite3.connect(temporary, isolation_level=None)) as candidate:
                original.backup(candidate)
                _apply_migrations(candidate, kind, migrations)
                validate_schema(candidate, kind)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    try:
        if backup:
            with closing(_readonly(path)) as original:
                _write_safety_backup(original, path, kind, version, current_version)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _migrate_new_database(path: Path, kind: DatabaseKind, migrations: tuple[Migration, ...]) -> None:
    temporary = path.with_name(f".{path.name}.migration-{uuid.uuid4().hex}.tmp")
    try:
        with closing(sqlite3.connect(temporary, isolation_level=None)) as candidate:
            _apply_migrations(candidate, kind, migrations)
            validate_schema(candidate, kind)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _apply_migrations(
    db: sqlite3.Connection,
    kind: DatabaseKind,
    migrations: tuple[Migration, ...],
    *,
    current_version: int | None = None,
) -> None:
    version = schema_version(db)
    current_version = current_version or _current_version(kind)
    if version > current_version:
        raise DatabaseSchemaError(
            f"database schema version {version} is newer than supported version {current_version}"
        )
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, current_version + 1)):
        raise DatabaseSchemaError("migration history must contain ordered consecutive versions")
    validate_schema(db, kind, allow_legacy=True)
    db.execute("BEGIN IMMEDIATE")
    try:
        for migration in migrations:
            if migration.version <= version:
                continue
            _execute_script(db, migration.sql)
            if kind == "business" and migration.version == 1:
                _add_legacy_columns(db)
            db.execute(f"PRAGMA user_version = {migration.version}")
        db.commit()
    except Exception:
        db.rollback()
        raise


def _execute_script(db: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                db.execute(statement)
            statement = ""
    if statement.strip():
        raise DatabaseSchemaError("incomplete SQL migration statement")


def _add_legacy_columns(db: sqlite3.Connection) -> None:
    definitions = {
        "materials": {"revoked_at": "TEXT"},
        "claims": {"revoked_at": "TEXT"},
        "sessions": {
            "max_questions": "INTEGER NOT NULL DEFAULT 3",
            "run_started": "INTEGER NOT NULL DEFAULT 0",
            "chat_memory": "TEXT NOT NULL DEFAULT ''",
            "title": "TEXT NOT NULL DEFAULT ''",
            "model_id": "TEXT NOT NULL DEFAULT 'default'",
        },
        "interview_items": {"skipped": "INTEGER NOT NULL DEFAULT 0"},
    }
    for table, columns in definitions.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for column, definition in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _write_safety_backup(
    original: sqlite3.Connection,
    path: Path,
    kind: DatabaseKind,
    old_version: int,
    current_version: int,
) -> Path:
    directory = path.parent / "migration-backups"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / (
        f"{path.name}.pre-v{old_version}-to-v{current_version}-{uuid.uuid4().hex}.sqlite3"
    )
    try:
        with closing(sqlite3.connect(destination)) as backup:
            original.backup(backup)
        with closing(sqlite3.connect(destination)) as backup:
            validate_schema(backup, kind, allow_legacy=True)
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _readonly(path: Path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)


def _migrations(kind: DatabaseKind) -> tuple[Migration, ...]:
    return _BUSINESS_MIGRATIONS if kind == "business" else _AUTH_MIGRATIONS


def _current_version(kind: DatabaseKind) -> int:
    return BUSINESS_SCHEMA_VERSION if kind == "business" else AUTH_SCHEMA_VERSION
