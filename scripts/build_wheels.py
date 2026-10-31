"""Build independently installable wheels for materialized Nico-agent packages."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from check_dependency_graph import PACKAGES, ROOT, existing_packages, topological_order


def changed_package_names() -> set[str]:
    commands = [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    changed: set[str] = set()
    for command in commands:
        result = subprocess.run(
            command, cwd=ROOT, check=True, text=True, capture_output=True
        )
        changed.update(line.strip().replace("\\", "/") for line in result.stdout.splitlines())
    selected = set()
    for name, config in existing_packages().items():
        directory = str(config["directory"]).replace("\\", "/").rstrip("/") + "/"
        if any(path == directory[:-1] or path.startswith(directory) for path in changed):
            selected.add(name)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="build every materialized package")
    selection.add_argument("--changed", action="store_true", help="build changed materialized packages")
    parser.add_argument("--clean", action="store_true", help="remove the output directory first")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    output = args.out.resolve()
    if args.clean and output.exists():
        try:
            output.relative_to(ROOT)
        except ValueError:
            parser.error("--clean only accepts an output directory inside the repository")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    present = existing_packages()
    selected = set(present) if args.all or not args.changed else changed_package_names()
    order = [name for name in topological_order() if name in selected and name in present]
    if not order:
        print("No materialized package changes to build.")
        return 0

    for name in order:
        package_dir = ROOT / PACKAGES[name]["directory"]
        print(f"Building {name} from {package_dir.relative_to(ROOT)}")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-cache-dir",
                "--wheel-dir",
                str(output),
                str(package_dir),
            ],
            cwd=ROOT,
            check=True,
        )

    for name in order:
        distribution = str(PACKAGES[name]["distribution"]).replace("-", "_")
        wheels = sorted(output.glob(f"{distribution}-*.whl"))
        if not wheels:
            print(f"ERROR: no wheel produced for {name}", file=sys.stderr)
            return 1
        print(f"Wheel {name}: {wheels[-1].name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
