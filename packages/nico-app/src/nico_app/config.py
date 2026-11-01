"""Application paths derived from environment and installation mode."""

import os
from pathlib import Path


def data_dir() -> Path:
    if "DATA_DIR" in os.environ:
        return Path(os.environ["DATA_DIR"]).expanduser()

    source_root = Path(__file__).resolve().parents[4]
    if (source_root / "EXECUTION_PLAN.md").is_file():
        return source_root / "data"

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA")
        return (Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local") / "Nico-agent"

    xdg_data_home = os.getenv("XDG_DATA_HOME")
    return Path(xdg_data_home) / "nico-agent" if xdg_data_home else Path.home() / ".nico-agent"
