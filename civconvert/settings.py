"""Where the customer profiles and CargoWise templates live."""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    """config/ next to the code, or CIVCONVERT_CONFIG_DIR (e.g. a mounted volume)."""
    return Path(os.environ.get("CIVCONVERT_CONFIG_DIR") or PROJECT_DIR / "config").resolve()
