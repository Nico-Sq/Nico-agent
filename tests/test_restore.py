import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from career_agent.restore import restore
from career_agent.store import Store


class RestoreTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).parent / ".tmp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.target = self.root / "data" / "career_agent.sqlite3"
        Store(self.target).create_material("project", "当前项目", "恢复前的数据")

    def tearDown(self):
        self.temp.cleanup()

    def test_restore_replaces_business_data_and_preserves_previous_snapshot(self):
        source = Store(self.root / "source" / "career_agent.sqlite3")
        source.create_material("resume", "备份简历", "恢复后的数据")
        backup = self.root / "download.sqlite3"
        backup.write_bytes(source.backup())

        previous = restore(backup, self.target)

        self.assertEqual(Store(self.target).list_materials()[0]["content"], "恢复后的数据")
        self.assertEqual(Store(previous).list_materials()[0]["content"], "恢复前的数据")

    def test_invalid_backup_does_not_touch_existing_data(self):
        invalid = self.root / "invalid.sqlite3"
        invalid.write_bytes(b"not a SQLite database")
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            restore(invalid, self.target)
        self.assertEqual(Store(self.target).list_materials()[0]["content"], "恢复前的数据")
        self.assertEqual(list(self.target.parent.glob("*.before-restore-*.sqlite3")), [])
        with self.assertRaisesRegex(ValueError, "目标文件必须命名"):
            restore(self.target, self.root / "auth.sqlite3")

    def test_restore_creates_missing_target(self):
        backup = self.root / "download.sqlite3"
        backup.write_bytes(Store(self.target).backup())
        new_target = self.root / "other" / "career_agent.sqlite3"

        self.assertIsNone(restore(backup, new_target))
        self.assertEqual(Store(new_target).list_materials()[0]["content"], "恢复前的数据")

    def test_failed_replace_keeps_original_database(self):
        source = Store(self.root / "source" / "career_agent.sqlite3")
        source.create_material("resume", "备份简历", "新数据")
        backup = self.root / "download.sqlite3"
        backup.write_bytes(source.backup())

        with patch("career_agent.restore.os.replace", side_effect=OSError("替换失败")):
            with self.assertRaises(OSError):
                restore(backup, self.target)

        self.assertEqual(Store(self.target).list_materials()[0]["content"], "恢复前的数据")
        self.assertEqual(list(self.target.parent.glob(".career_agent.restore-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
