import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

        Store(path)
        self.assertFalse((self.root / "migration-backups").exists())

    def test_schema_v1_upgrades_to_v2_without_losing_data(self):
        path = self.root / "career_agent.sqlite3"
        with database(path) as db:
            sql = resources.files("nico_storage.migrations").joinpath(
                "001_business.sql",
            ).read_text(encoding="utf-8")
            db.executescript(sql)
            db.execute("PRAGMA user_version = 1")
            db.execute(
                "INSERT INTO sessions(id, kind, status, created_at) VALUES (?, ?, ?, ?)",
                ("existing-chat", "chat", "active", "2026-01-01"),
            )

        migrated = Store(path)
        self.assertEqual(migrated.get("existing-chat", "chat")["id"], "existing-chat")
        with database(path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM channel_messages").fetchone()[0], 0)
        backups = list((self.root / "migration-backups").glob("*.pre-v1-to-v2-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with database(backups[0]) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)

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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
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
        bad_history = (
            Migration(1, "CREATE TABLE migration_probe (value TEXT);\nTHIS IS NOT SQL;"),
            Migration(2, "CREATE TABLE channel_probe (value TEXT);"),
        )

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
            db.execute("PRAGMA user_version = 3")
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
        for path in (root_store.path, account_store.path):
            with database(path) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
        with database(auth_store.path) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_channel_ledger_survives_reopen_and_reuses_delivery_id(self):
        path = self.root / "career_agent.sqlite3"
        store = Store(path)
        session = store.create("chat")
        binding = store.bind_channel("console", "console", "local", session["id"])
        self.assertEqual(binding["session_id"], session["id"])
        claimed, event = store.claim_inbound(
            "console", "console", "message-1", "local", session["id"], "sha256:payload",
        )
        self.assertTrue(claimed)
        self.assertEqual(event["status"], "processing")

        reopened = Store(path)
        duplicate, event = reopened.claim_inbound(
            "console", "console", "message-1", "local", session["id"], "sha256:payload",
        )
        self.assertFalse(duplicate)
        self.assertEqual(event["status"], "processing")
        event = reopened.store_channel_reply(
            "console", "console", "message-1", "生成一次", "console:console:message-1:reply",
        )
        reopened.mark_delivery_failed(event["delivery_id"], "ConnectionError")

        after_failure = Store(path).channel_message("console", "console", "message-1")
        self.assertEqual(after_failure["status"], "delivery_failed")
        self.assertEqual(after_failure["response"], "生成一次")
        self.assertEqual(after_failure["delivery_id"], "console:console:message-1:reply")
        Store(path).mark_delivered(after_failure["delivery_id"])
        self.assertEqual(Store(path).channel_message("console", "console", "message-1")["status"], "completed")

        reopened = Store(path)
        reopened.delete_session(session["id"], "chat")
        self.assertIsNone(reopened.channel_binding("console", "console")["session_id"])
        self.assertEqual(reopened.channel_message("console", "console", "message-1")["response"], "生成一次")

    def test_concurrent_inbound_claim_has_one_winner(self):
        path = self.root / "career_agent.sqlite3"
        session = Store(path).create("chat")

        def claim():
            return Store(path).claim_inbound(
                "console", "console", "same-id", "local", session["id"], "sha256:payload",
            )[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = list(pool.map(lambda _: claim(), range(2)))
        self.assertEqual(claimed.count(True), 1)

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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

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
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)

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
