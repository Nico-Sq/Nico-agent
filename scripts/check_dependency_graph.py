"""Validate the declared package graph and source-level import boundaries."""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Edges point from an importer to the repository packages it may import.
PACKAGES: dict[str, dict[str, object]] = {
    "nico_contracts": {
        "distribution": "nico-contracts",
        "directory": Path("packages/nico-contracts"),
        "depends": set(),
    },
    "nico_domain": {
        "distribution": "nico-domain",
        "directory": Path("packages/nico-domain"),
        "depends": set(),
    },
    "nico_agent_core": {
        "distribution": "nico-agent-core",
        "directory": Path("packages/nico-agent-core"),
        "depends": {"nico_contracts"},
    },
    "nico_application": {
        "distribution": "nico-application",
        "directory": Path("packages/nico-application"),
        "depends": {"nico_contracts", "nico_domain"},
    },
    "nico_adapters": {
        "distribution": "nico-adapters",
        "directory": Path("packages/nico-adapters"),
        "depends": {"nico_contracts", "nico_domain", "nico_agent_core", "nico_application"},
    },
    "nico_storage": {
        "distribution": "nico-storage",
        "directory": Path("packages/nico-storage"),
        "depends": {"nico_contracts", "nico_domain", "nico_application"},
    },
    "nico_cli": {
        "distribution": "nico-cli",
        "directory": Path("packages/nico-cli"),
        "depends": {"nico_contracts", "nico_application"},
    },
    "nico_web": {
        "distribution": "nico-web",
        "directory": Path("packages/nico-web"),
        "depends": {"nico_contracts", "nico_application"},
    },
    "nico_app": {
        "distribution": "nico-agent",
        "directory": Path("packages/nico-app"),
        "depends": set(),
    },
}

PACKAGES["nico_app"]["depends"] = {
    name for name in PACKAGES if name != "nico_app"
}

FORBIDDEN_PURE_IMPORTS = {
    "anthropic",
    "argparse",
    "docx",
    "fastapi",
    "httpx",
    "mcp",
    "openai",
    "pypdf",
    "sqlite3",
    "uvicorn",
}

CORE_FORBIDDEN = {
    "nico_adapters",
    "nico_application",
    "nico_domain",
    "nico_storage",
    "career_agent",
}


def topological_order() -> list[str]:
    order: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str, stack: list[str]) -> None:
        if name in visiting:
            cycle = " -> ".join([*stack, name])
            raise ValueError(f"package dependency cycle: {cycle}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in PACKAGES[name]["depends"]:
            visit(dependency, [*stack, name])
        visiting.remove(name)
        visited.add(name)
        order.append(name)

    for name in PACKAGES:
        visit(name, [])
    return order


def existing_packages() -> dict[str, dict[str, object]]:
    return {
        name: config
        for name, config in PACKAGES.items()
        if (ROOT / config["directory"]).is_dir()
    }


def package_imports(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module.split(".", 1)[0])
    return imports


def validate_imports(require_all: bool) -> list[str]:
    errors: list[str] = []
    present = existing_packages()
    if require_all:
        missing = sorted(set(PACKAGES) - set(present))
        if missing:
            errors.append("missing planned packages: " + ", ".join(missing))

    for name, config in present.items():
        directory = ROOT / config["directory"]
        pyproject = directory / "pyproject.toml"
        source_root = directory / "src" / name
        internal: set[str] = set()
        if not pyproject.is_file():
            errors.append(f"{name}: missing {pyproject.relative_to(ROOT)}")
        else:
            declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"].get("dependencies", [])
            distribution_names = {
                str(item["distribution"]): package_name
                for package_name, item in PACKAGES.items()
            }
            for dependency in declared:
                match = re.match(r"[A-Za-z0-9_.-]+", dependency)
                if match:
                    package_name = distribution_names.get(match.group().lower().replace("_", "-"))
                    if package_name:
                        internal.add(package_name)
            unexpected = internal - set(config["depends"])
            if unexpected:
                errors.append(f"{name}: undeclared graph dependencies in pyproject: {', '.join(sorted(unexpected))}")
        if not source_root.is_dir():
            errors.append(f"{name}: missing source directory {source_root.relative_to(ROOT)}")
            continue
        allowed = set(config["depends"])
        for source in source_root.rglob("*.py"):
            imports = package_imports(source)
            for imported in sorted(imports & set(PACKAGES)):
                if imported not in allowed:
                    errors.append(
                        f"{source.relative_to(ROOT)}: {name} imports undeclared "
                        f"repository package {imported}"
                    )
                elif imported != name and imported not in internal:
                    errors.append(
                        f"{source.relative_to(ROOT)}: {name} imports {imported} "
                        "without a distribution dependency"
                    )
            if name in {"nico_contracts", "nico_domain", "nico_application"}:
                for imported in sorted(imports & FORBIDDEN_PURE_IMPORTS):
                    errors.append(
                        f"{source.relative_to(ROOT)}: {name} must not import {imported}"
                    )
            if name == "nico_agent_core":
                for imported in sorted(imports & CORE_FORBIDDEN):
                    errors.append(
                        f"{source.relative_to(ROOT)}: nico_agent_core must not import {imported}"
                    )
            if name == "nico_contracts" and imports & (set(PACKAGES) - {"nico_contracts"}):
                errors.append(
                    f"{source.relative_to(ROOT)}: nico_contracts may not import other packages"
                )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-all", action="store_true",
                        help="fail until every planned package exists")
    args = parser.parse_args()
    try:
        order = topological_order()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    errors = validate_imports(args.require_all)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    present = sorted(existing_packages())
    print("package_graph=acyclic")
    print("topological_order=" + ", ".join(order))
    print("materialized_packages=" + ", ".join(present))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
