import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from career_agent import main
from career_agent.auto_backup import backup_all
from career_agent.store import Store


class AutoBackupTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.data_dir = Path(self.temp.name)
        self.root = Store(self.data_dir / "career_agent.sqlite3")
        self.root.create_material("project", "旧项目", "旧内容")
        self.account = Store(self.data_dir / "users" / "account-id" / "career_agent.sqlite3")
        self.account.create_material("resume", "账户简历", "私有内容")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def material_contents(path: Path) -> list[str]:
        with closing(sqlite3.connect(path)) as db:
            return [row[0] for row in db.execute("SELECT content FROM materials ORDER BY content")]

    def test_daily_snapshots_are_scoped_and_keep_seven_days(self):
        start = date(2026, 1, 1)
        first = backup_all(self.data_dir, start)
        self.assertEqual(len(first), 2)
        self.assertEqual(self.material_contents(first[0]), ["旧内容"])
        self.assertEqual(self.material_contents(first[1]), ["私有内容"])
        self.root.create_material("project", "新项目", "新内容")
        self.assertEqual(backup_all(self.data_dir, start), first)
        self.assertEqual(self.material_contents(first[0]), ["旧内容"])
        self.assertEqual(self.material_contents(self.data_dir / "backups" / "career_agent-latest.sqlite3"),
                         ["新内容", "旧内容"])
        self.assertEqual(self.material_contents(self.data_dir / "users" / "account-id" / "backups" /
                                                "career_agent-latest.sqlite3"), ["私有内容"])
        unrelated = self.data_dir / "backups" / "career_agent-abcdefgh.sqlite3"
        unrelated.write_bytes(b"user file")

        for offset in range(1, 8):
            backup_all(self.data_dir, start + timedelta(days=offset))
        root_backups = sorted(item for item in (self.data_dir / "backups").glob("career_agent-*.sqlite3")
                              if item.stem[-8:].isdigit())
        account_backups = sorted(item for item in (self.data_dir / "users" / "account-id" / "backups").glob("career_agent-*.sqlite3")
                                 if item.stem[-8:].isdigit())
        self.assertEqual(len(root_backups), 7)
        self.assertEqual(len(account_backups), 7)
        self.assertFalse(first[0].exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue((self.data_dir / "backups" / "career_agent-latest.sqlite3").exists())
        self.assertEqual(self.material_contents(root_backups[-1]), ["新内容", "旧内容"])
        self.assertEqual(self.material_contents(account_backups[-1]), ["私有内容"])

    def test_app_startup_creates_local_snapshot(self):
        with patch.object(main, "DATA_DIR", self.data_dir):
            with TestClient(main.app):
                daily = [item for item in (self.data_dir / "backups").glob("career_agent-*.sqlite3")
                         if item.stem[-8:].isdigit()]
                self.assertEqual(len(daily), 1)
                self.root.create_material("project", "退出前项目", "退出前内容")
            latest = self.data_dir / "backups" / "career_agent-latest.sqlite3"
            self.assertEqual(self.material_contents(latest), ["旧内容", "退出前内容"])


if __name__ == "__main__":
    unittest.main()
