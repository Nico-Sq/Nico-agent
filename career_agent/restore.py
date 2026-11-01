"""Compatibility CLI for restoring a Nico-agent business database snapshot."""

import argparse
import os
import sqlite3
from pathlib import Path

from nico_storage.restore import OPTIONAL_TABLES, TABLES, restore, validate_backup


def main():
    parser = argparse.ArgumentParser(description="恢复 Nico-agent 业务数据；运行前请停止应用。")
    parser.add_argument("backup", type=Path, help="页面下载的 SQLite 业务数据快照")
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
