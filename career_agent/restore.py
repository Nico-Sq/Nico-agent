"""Restore a downloaded business database while the app is stopped."""

import argparse
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path


TABLES = {
    "sessions", "messages", "events", "materials", "claims",
    "session_materials", "session_claims", "interview_items",
    "interview_evidence", "practice_sessions",
}
OPTIONAL_TABLES = {
    "session_tool_grants", "memories", "memory_candidates", "memory_candidate_sources",
}


def validate_backup(db: sqlite3.Connection):
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite 文件完整性检查失败")
    objects = db.execute("SELECT type, name FROM sqlite_master").fetchall()
    tables = {name for kind, name in objects if kind == "table"}
    if not TABLES.issubset(tables) or any(
        kind in ("trigger", "view") or (kind == "table" and name not in TABLES | OPTIONAL_TABLES | {"sqlite_sequence"})
        for kind, name in objects
    ):
        raise ValueError("文件不是 Nico-agent 业务数据备份")
    if db.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("备份中的数据关联检查失败")


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
                validate_backup(staged)
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


def main():
    parser = argparse.ArgumentParser(description="恢复 Nico-agent 业务数据；运行前请停止应用。")
    parser.add_argument("backup", type=Path, help="页面下载的 SQLite 备份文件")
    default_target = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data")) / "career_agent.sqlite3"
    parser.add_argument("--target", type=Path, default=default_target, help="目标业务数据库路径")
    args = parser.parse_args()
    try:
        previous = restore(args.backup, args.target)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"恢复失败：{exc}\n")
    print(f"已恢复到 {args.target}")
    if previous:
        print(f"原数据快照：{previous}")


if __name__ == "__main__":
    main()
