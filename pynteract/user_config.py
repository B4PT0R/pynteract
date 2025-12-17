from __future__ import annotations

import os
from pathlib import Path


def config_dir() -> Path:
    """Return the user config directory for pynteract.

    Defaults to `~/.pynteract`. Override with `PYNTERACT_CONFIG_DIR`.
    """
    override = os.environ.get("PYNTERACT_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".pynteract"


def ensure_config_dir() -> Path:
    path = config_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def history_path(*, kind: str) -> Path:
    """Return the persistent prompt history path for `kind` (e.g. 'python', 'text')."""
    return ensure_config_dir() / f"history_{kind}.txt"


def startup_path() -> Path:
    """Return the startup script path (`~/.pynteract/startup.py`)."""
    return ensure_config_dir() / "startup.py"

