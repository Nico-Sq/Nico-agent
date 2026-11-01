"""Validate and restore a Nico-agent business database snapshot."""

import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .schema_migrations import migrate_database
from .schema import (
    DatabaseSchemaError,
    RESTORE_OPTIONAL_TABLES as OPTIONAL_TABLES,
    RESTORE_REQUIRED_TABLES as TABLES,
    validate_schema,
)


def validate_backup(db: sqlite3.Connection) -> int:
    try:
        return validate_schema(
            db,
            "business",
            allow_legacy=True,
            require_restore_tables=True,
        )
    except DatabaseSchemaError as exc:
        if str(exc) == "SQLite file integrity check failed":
            raise ValueError("SQLite 文件完整性检查失败") from exc
        if str(exc) == "SQLite foreign key check failed":
            raise ValueError("备份中的数据关联检查失败") from exc
        raise ValueError("文件不是 Nico-agent 业务数据备份") from exc


def restore(backup_path: Path, target_path: Path) -> Path | None:
    if target_path.name != "career_agent.sqlite3":
        raise ValueError("目标文件必须命名为 career_agent.sqlite3")
    if not backup_path.is_file() or backup_path.resolve() == target_path.resolve():
        raise ValueError("请选择与目标数据库不同的现有备份文件")
    with closing(sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        validate_backup(source)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = target_path.with_name(f".career_agent.restore-{uuid.uuid4().hex}.tmp")
        try:
            with closing(sqlite3.connect(temporary)) as staged:
                source.backup(staged)
            migrate_database(temporary, "business", backup=False)
            with closing(sqlite3.connect(temporary)) as staged:
                validate_schema(staged, "business")

            previous = None
            if target_path.exists():
                previous = target_path.with_name(f"career_agent.before-restore-{uuid.uuid4().hex}.sqlite3")
                try:
                    with closing(sqlite3.connect(target_path)) as current:
                        validate_backup(current)
                        with closing(sqlite3.connect(previous)) as safety_copy:
                            current.backup(safety_copy)
                except Exception:
                    previous.unlink(missing_ok=True)
                    raise
            os.replace(temporary, target_path)
        finally:
            temporary.unlink(missing_ok=True)
    return previous
