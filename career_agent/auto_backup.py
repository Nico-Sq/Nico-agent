"""Daily local snapshots of business databases."""

import logging
import os
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from .store import Store


def backup_database(path: Path, day: date | None = None) -> Path:
    day = day or datetime.now(timezone.utc).date()
    directory = path.parent / "backups"
    directory.mkdir(exist_ok=True)
    destination = directory / f"career_agent-{day:%Y%m%d}.sqlite3"
    if not destination.exists():
        temporary = directory / f".{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_bytes(Store(path).backup())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    snapshots = [item for item in directory.glob("career_agent-*.sqlite3")
                 if len(item.stem) == len("career_agent-") + 8 and item.stem[-8:].isdigit()]
    for old in sorted(snapshots)[:-7]:
        old.unlink()
    return destination


def backup_all(data_dir: Path, day: date | None = None) -> list[Path]:
    databases = [data_dir / "career_agent.sqlite3", *sorted((data_dir / "users").glob("*/career_agent.sqlite3"))]
    snapshots = []
    for path in databases:
        if path.is_file():
            try:
                snapshots.append(backup_database(path, day))
            except Exception:
                logging.exception("自动备份业务库失败：%s", path)
    return snapshots
