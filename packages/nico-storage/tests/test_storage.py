import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from nico_storage import AuthStore, Store
from nico_storage.restore import restore
from nico_storage.schema import DatabaseSchemaError
from nico_storage.schema_migrations import Migration, _apply_migrations


@contextmanager
def database(path, isolation_level=None):
    db = sqlite3.connect(path, isolation_level=isolation_level)
    try:
        yield db
    finally:
        db.close()


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_new_and_current_databases_migrate_once(self):
        path = self.root / "career_agent.sqlite3"
        store = Store(path)
        session = store.create("chat")
        self.assertEqual(store.get(session["id"], "chat")["id"], session["id"])
        with database(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

        Store(path)
        self.assertFalse((self.root / "migration-backups").exists())

    def test_historical_database_is_migrated_with_a_safety_copy(self):
        path = self.root / "legacy.sqlite3"
        with database(path) as db:
            db.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, kind TEXT, status TEXT, goal TEXT,
                    question_count INTEGER, version INTEGER, created_at TEXT
                );
                CREATE TABLE materials (
                    id TEXT PRIMARY KEY, kind TEXT, title TEXT, content TEXT, created_at TEXT
                );
                CREATE TABLE claims (
                    id TEXT PRIMARY KEY, material_id TEXT, text TEXT, status TEXT, created_at TEXT
                );
                CREATE TABLE interview_items (
                    session_id TEXT, number INTEGER, question TEXT, answer TEXT, feedback TEXT,
                    PRIMARY KEY (session_id, number)
                );
                INSERT INTO sessions VALUES ('legacy-chat', 'chat', 'active', '', 0, 0, '2026-01-01');
                INSERT INTO materials VALUES ('legacy-material', 'project', '旧材料', '原始内容', '2026-01-01');
                INSERT INTO claims VALUES ('legacy-claim', 'legacy-material', '原始经历', 'confirmed', '2026-01-01');
            """)

        store = Store(path)
        self.assertEqual(store.get("legacy-chat", "chat")["max_questions"], 3)
        self.assertEqual(store.list_materials()[0]["content"], "原始内容")
        self.assertEqual(store.list_claims()[0]["text"], "原始经历")
        with database(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        backups = list((self.root / "migration-backups").glob("*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with database(backups[0]) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertEqual(backup.execute("SELECT id FROM sessions").fetchone()[0], "legacy-chat")

    def test_unknown_legacy_database_is_left_untouched(self):
        path = self.root / "unknown.sqlite3"
        with database(path) as db:
            db.execute("CREATE TABLE private_data (value TEXT)")
            db.execute("INSERT INTO private_data VALUES ('keep')")
        before = path.read_bytes()

        with self.assertRaises(DatabaseSchemaError):
            Store(path)

        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "migration-backups").exists())

    def test_failed_migration_keeps_original_and_creates_no_backup(self):
        path = self.root / "legacy.sqlite3"
        with database(path) as db:
            db.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, kind TEXT, status TEXT, goal TEXT,
                    question_count INTEGER, version INTEGER, created_at TEXT
                );
                INSERT INTO sessions VALUES ('legacy-chat', 'chat', 'active', '', 0, 0, '2026-01-01');
            """)
        before = path.read_bytes()
        bad_history = (Migration(1, "CREATE TABLE migration_probe (value TEXT);\nTHIS IS NOT SQL;"),)

        with patch("nico_storage.schema_migrations._BUSINESS_MIGRATIONS", bad_history):
            with self.assertRaises(sqlite3.Error):
                Store(path)

        self.assertEqual(path.read_bytes(), before)
        self.assertFalse((self.root / "migration-backups").exists())
        self.assertEqual(list(self.root.glob(".*.migration-*.tmp")), [])

    def test_future_schema_is_rejected_without_rewriting_it(self):
        path = self.root / "career_agent.sqlite3"
        Store(path)
        with database(path) as db:
            db.execute("PRAGMA user_version = 2")
        before = path.read_bytes()

        with self.assertRaisesRegex(DatabaseSchemaError, "newer than supported version"):
            Store(path)

        self.assertEqual(path.read_bytes(), before)

    def test_migrations_apply_two_ordered_versions_atomically(self):
        with database(":memory:", isolation_level=None) as db:
            _apply_migrations(
                db,
                "business",
                (
                    Migration(1, "CREATE TABLE first_version (value TEXT);"),
                    Migration(2, "CREATE TABLE second_version (value TEXT);"),
                ),
                current_version=2,
            )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                ).fetchall(),
                [("first_version",), ("second_version",)],
            )

    def test_failed_migration_rolls_back_schema_and_version(self):
        with database(":memory:", isolation_level=None) as db:
            with self.assertRaises(sqlite3.OperationalError):
                _apply_migrations(
                    db,
                    "business",
                    (
                        Migration(1, "CREATE TABLE migration_probe (value TEXT);"),
                        Migration(2, "THIS IS NOT SQL;"),
                    ),
                    current_version=2,
                )
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'migration_probe'"
                ).fetchone()
            )

    def test_auth_and_account_databases_migrate_independently(self):
        root_store = Store(self.root / "career_agent.sqlite3")
        account_store = Store(self.root / "users" / "account-id" / "career_agent.sqlite3")
        auth_store = AuthStore(self.root / "auth.sqlite3")
        root_session = root_store.create("chat")
        account_session = account_store.create("chat")

        self.assertNotEqual(root_session["id"], account_session["id"])
        for path in (
            root_store.path,
            account_store.path,
            auth_store.path,
        ):
            with database(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_historical_auth_database_is_upgraded_without_losing_users(self):
        path = self.root / "auth.sqlite3"
        with database(path) as db:
            db.executescript("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                    salt BLOB NOT NULL, password_hash BLOB NOT NULL
                );
                CREATE TABLE login_tokens (
                    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE login_attempts (
                    username TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                    first_failed_at INTEGER NOT NULL
                );
                INSERT INTO users VALUES ('user-id', 'alice', X'01', X'02');
            """)

        store = AuthStore(path)
        self.assertEqual(store.username("user-id"), "alice")
        with database(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        backups = list((self.root / "migration-backups").glob("auth.sqlite3.pre-v0-to-v1-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with database(backups[0]) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_restore_upgrades_a_known_legacy_backup(self):
        source_path = self.root / "source" / "career_agent.sqlite3"
        source = Store(source_path)
        source.create_material("project", "材料", "旧备份内容")
        backup = self.root / "legacy-backup.sqlite3"
        backup.write_bytes(source.backup())
        with database(backup) as db:
            db.execute("PRAGMA user_version = 0")

        target = self.root / "target" / "career_agent.sqlite3"
        Store(target).create_material("resume", "当前", "当前内容")
        restore(backup, target)

        restored = Store(target)
        self.assertEqual(restored.list_materials()[0]["content"], "旧备份内容")
        with database(target) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_restore_migrates_a_partial_known_legacy_schema(self):
        backup = self.root / "legacy-backup.sqlite3"
        with database(backup) as db:
            db.executescript("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, kind TEXT, status TEXT, goal TEXT,
                    question_count INTEGER, version INTEGER, created_at TEXT
                );
                CREATE TABLE materials (
                    id TEXT PRIMARY KEY, kind TEXT, title TEXT, content TEXT, created_at TEXT
                );
                CREATE TABLE claims (
                    id TEXT PRIMARY KEY, material_id TEXT, text TEXT, status TEXT, created_at TEXT
                );
                CREATE TABLE interview_items (
                    session_id TEXT, number INTEGER, question TEXT, answer TEXT, feedback TEXT,
                    PRIMARY KEY (session_id, number)
                );
                INSERT INTO materials VALUES ('legacy-material', 'project', '旧材料', '旧备份内容', '2026-01-01');
            """)

        target = self.root / "target" / "career_agent.sqlite3"
        Store(target).create_material("resume", "当前", "当前内容")
        restore(backup, target)

        restored = Store(target)
        self.assertEqual(restored.list_materials()[0]["content"], "旧备份内容")
        with database(target) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_restore_rejects_an_empty_database_snapshot(self):
        backup = self.root / "empty.sqlite3"
        with database(backup):
            pass
        target = self.root / "target" / "career_agent.sqlite3"
        Store(target).create_material("resume", "当前", "当前内容")

        with self.assertRaisesRegex(ValueError, "不是 Nico-agent 业务数据备份"):
            restore(backup, target)

        self.assertEqual(Store(target).list_materials()[0]["content"], "当前内容")


if __name__ == "__main__":
    unittest.main()
