"""Install built wheels in a temporary venv and verify imports outside the repository."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from build_wheels import changed_package_names
from check_dependency_graph import PACKAGES, ROOT, existing_packages, topological_order


def wheel_for(output: Path, distribution: str) -> Path | None:
    normalized = distribution.replace("-", "_").lower()
    candidates = [
        wheel for wheel in output.glob("*.whl")
        if wheel.name.replace("-", "_").lower().startswith(normalized + "_")
    ]
    return sorted(candidates)[-1] if candidates else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="install every available wheel")
    selection.add_argument("--changed", action="store_true", help="install changed package wheels")
    parser.add_argument("--wheel-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()

    present = existing_packages()
    selected = set(present) if args.all or not args.changed else changed_package_names()
    order = [name for name in topological_order() if name in selected and name in present]
    if not order:
        print("No materialized package wheels selected.")
        return 0

    wheels: list[tuple[str, Path]] = []
    for name in order:
        wheel = wheel_for(args.wheel_dir.resolve(), str(PACKAGES[name]["distribution"]))
        if wheel is None:
            print(f"ERROR: missing wheel for {name} in {args.wheel_dir}", file=sys.stderr)
            return 1
        wheels.append((name, wheel))

    with tempfile.TemporaryDirectory(prefix="nico-wheel-install-") as temp:
        root = Path(temp)
        venv_dir = root / "venv"
        work_dir = root / "outside-repository"
        work_dir.mkdir()
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        for _, wheel in wheels:
            subprocess.run(
                [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
                cwd=work_dir,
                check=True,
            )

        modules = [name for name, _ in wheels]
        verification = (
            "import importlib, importlib.metadata, json\n"
            "import tempfile\n"
            "from pathlib import Path\n"
            f"modules={modules!r}\n"
            "loaded=[]\n"
            "for module in modules:\n"
            "    importlib.import_module(module)\n"
            "    loaded.append(module)\n"
            "tool_adapters=[]\n"
            "channels=[]\n"
            "if 'nico_adapters' in modules:\n"
            "    tool_adapters=sorted(ep.name for ep in importlib.metadata.entry_points("
            "group='nico_agent.tool_adapters'))\n"
            "    assert {'stdio_mcp','trusted_local_plugins'} <= set(tool_adapters), tool_adapters\n"
            "    channels=sorted(ep.name for ep in importlib.metadata.entry_points("
            "group='nico_agent.channels'))\n"
            "    assert {'console'} <= set(channels), channels\n"
            "if 'nico_storage' in modules:\n"
            "    from nico_storage import Store\n"
            "    from nico_storage.schema import BUSINESS_SCHEMA_VERSION\n"
            "    with tempfile.TemporaryDirectory() as directory:\n"
            "        store=Store(Path(directory) / 'career_agent.sqlite3')\n"
            "        with store.connect() as db:\n"
            "            assert db.execute('PRAGMA user_version').fetchone()[0] == BUSINESS_SCHEMA_VERSION\n"
            "print(json.dumps({'loaded': loaded, 'tool_adapters': tool_adapters, 'channels': channels, "
            "'cwd': __import__('os').getcwd()}))\n"
        )
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        result = subprocess.run(
            [str(python), "-c", verification],
            cwd=work_dir,
            env=environment,
            check=True,
            text=True,
            capture_output=True,
        )
        payload = json.loads(result.stdout)
        expected_cwd = str(work_dir.resolve())
        if payload["loaded"] != modules or str(Path(payload["cwd"]).resolve()) != expected_cwd:
            print(f"ERROR: unexpected install result: {payload}", file=sys.stderr)
            return 1
        for name, wheel in wheels:
            print(f"Verified outside repository: {name} <= {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
