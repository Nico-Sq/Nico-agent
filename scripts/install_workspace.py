"""Install external requirements and all local packages in dependency order."""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib

from check_dependency_graph import PACKAGES, ROOT, existing_packages, topological_order


def main() -> int:
    present = existing_packages()
    local_distributions = {
        str(config["distribution"]).lower().replace("_", "-")
        for config in PACKAGES.values()
    }
    external = []
    for config in present.values():
        project = tomllib.loads((ROOT / config["directory"] / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        for dependency in project.get("dependencies", []):
            match = re.match(r"[A-Za-z0-9_.-]+", dependency)
            if match and match.group().lower().replace("_", "-") not in local_distributions:
                external.append(dependency)
    subprocess.run([sys.executable, "-m", "pip", "install", *sorted(set(external))], check=True)
    for name in topological_order():
        if name in present:
            directory = ROOT / PACKAGES[name]["directory"]
            print(f"Installing editable workspace package: {name}")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--editable", str(directory)],
                cwd=ROOT,
                check=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
