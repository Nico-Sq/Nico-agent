"""Install built wheels in a temporary venv and verify imports outside the repository."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from build_wheels import changed_package_names
from check_dependency_graph import PACKAGES, ROOT, existing_packages, topological_order


def wheel_for(output: Path, distribution: str) -> Path | None:
    normalized = distribution.replace("_", "-").lower()
    candidates = []
    for wheel in output.glob("*.whl"):
        with zipfile.ZipFile(wheel) as archive:
            metadata = next((name for name in archive.namelist()
                             if name.endswith(".dist-info/METADATA")), None)
            if metadata is None:
                continue
            content = archive.read(metadata).decode("utf-8", errors="replace")
            match = re.search(r"^Name: (.+)$", content, re.MULTILINE)
            if match and match.group(1).strip().replace("_", "-").lower() == normalized:
                candidates.append(wheel)
    return sorted(candidates)[-1] if candidates else None


def external_dependencies() -> list[str]:
    local_distributions = {
        str(config["distribution"]).lower().replace("_", "-")
        for config in PACKAGES.values()
    }
    dependencies = set()
    for config in existing_packages().values():
        project = tomllib.loads(
            (ROOT / config["directory"] / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        for dependency in project.get("dependencies", []):
            match = re.match(r"[A-Za-z0-9_.-]+", dependency)
            if match and match.group().lower().replace("_", "-") not in local_distributions:
                dependencies.add(dependency)
    return sorted(dependencies)


def verify_wheel_contents(wheels: list[tuple[str, Path]]) -> None:
    required = {
        "nico_adapters": {"nico_adapters/extract.py"},
        "nico_application": {
            "nico_application/resources/prompts/chat.md",
            "nico_application/resources/prompts/interview.md",
        },
        "nico_web": {
            "nico_web/resources/static/index.html",
            "nico_web/resources/static/app.js",
            "nico_web/resources/static/chat.js",
            "nico_web/resources/static/interview.js",
            "nico_web/resources/static/settings.js",
            "nico_web/resources/static/shared.js",
        },
        "nico_app": {
            "nico_app/main.py",
            "career_agent/main.py",
            "career_agent/cli.py",
            "career_agent/restore.py",
            "career_agent/__main__.py",
        },
    }
    for name, wheel in wheels:
        missing = required.get(name, set())
        if missing:
            with zipfile.ZipFile(wheel) as archive:
                present = set(archive.namelist())
            absent = sorted(missing - present)
            if absent:
                raise ValueError(f"{wheel.name} is missing package resources: {absent}")


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

    verify_wheel_contents(wheels)

    with tempfile.TemporaryDirectory(prefix="nico-wheel-install-") as temp:
        root = Path(temp)
        venv_dir = root / "venv"
        work_dir = root / "outside-repository"
        work_dir.mkdir()
        subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], check=True)
        python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [str(python), "-m", "pip", "install", *external_dependencies()],
            cwd=work_dir,
            check=True,
        )
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
            "import career_agent, career_agent.context, career_agent.extract, career_agent.main\n"
            "from fastapi.testclient import TestClient\n"
            "with TestClient(career_agent.main.app) as client:\n"
            "    assert client.get('/').status_code == 200\n"
            "    assert client.get('/static/shared.js').status_code == 200\n"
            "    assert client.get('/api/config').status_code == 200\n"
            "    created=client.post('/api/chat/sessions', json={}).json()\n"
            "    response=client.post(f\"/api/chat/sessions/{created['id']}/messages\", json={'text':'wheel smoke'})\n"
            "    assert response.status_code == 200, response.text\n"
            "prompt=career_agent.context.skill_file('chat').read_text(encoding='utf-8')\n"
            "assert prompt\n"
            "print(json.dumps({'loaded': loaded, 'tool_adapters': tool_adapters, 'channels': channels, "
            "'cwd': __import__('os').getcwd()}))\n"
        )
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["DATA_DIR"] = str(root / "installed-data")
        environment["MODEL_PROVIDER"] = "demo"
        environment.pop("AUTH_REQUIRED", None)
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
        for module in ("career_agent.cli", "career_agent.restore"):
            subprocess.run([str(python), "-m", module, "--help"], cwd=work_dir,
                           env=environment, check=True, stdout=subprocess.DEVNULL)
        path_check = (
            "from nico_app.config import data_dir\n"
            "import os\n"
            "from pathlib import Path\n"
            "expected=(Path(os.getenv('LOCALAPPDATA') or (Path.home()/'AppData'/'Local'))/'Nico-agent') if os.name=='nt' else "
            "(Path(os.environ['XDG_DATA_HOME'])/'nico-agent' if os.getenv('XDG_DATA_HOME') else Path.home()/'.nico-agent')\n"
            "assert data_dir() == expected, (data_dir(), expected)\n"
        )
        default_environment = environment.copy()
        default_environment.pop("DATA_DIR", None)
        subprocess.run([str(python), "-c", path_check], cwd=work_dir,
                       env=default_environment, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
