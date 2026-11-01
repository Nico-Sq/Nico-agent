from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager, closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator

from ..schema_migrations import migrate_database


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_memory_content(content: str) -> str:
    return " ".join(content.split())


MAX_MEMORIES = 200
MAX_EVENTS_PER_SESSION = 500
MAX_MEMORY_CANDIDATES = 200


class StoreBase:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        migrate_database(self.path, "business")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
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

    def transaction(self) -> AbstractContextManager[object]:
        return self.connect()

    def backup(self) -> bytes:
        with TemporaryDirectory() as directory:
            target_path = Path(directory) / "backup.sqlite3"
            with self.connect() as source, closing(sqlite3.connect(target_path)) as target:
                source.backup(target)
            return target_path.read_bytes()
