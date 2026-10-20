"""Where the customer profiles and CargoWise templates live, and the runtime settings.

Everything that differs between machines or hosting platforms comes from environment
variables (see .env.example), so the same image runs locally, on Northflank or elsewhere.
"""

import os
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    """config/ next to the code, or CIVCONVERT_CONFIG_DIR (e.g. a mounted volume)."""
    return Path(os.environ.get("CIVCONVERT_CONFIG_DIR") or PROJECT_DIR / "config").resolve()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_flag(name: str) -> bool:
    return env(name).lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name) or default)
    except ValueError:
        return default


def database_url() -> str:
    """DATABASE_URL (PostgreSQL in production), else a local SQLite file in data/."""
    url = env("DATABASE_URL")
    if not url:
        return f"sqlite:///{(PROJECT_DIR / 'data' / 'civconvert.db').as_posix()}"
    # hosting platforms often hand out postgres:// or postgresql:// – use the psycopg 3 driver
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def nimbus_api_url() -> str:
    """Base URL of the company Nimbus account system; empty = Nimbus login switched off."""
    return env("NIMBUS_API_URL", "https://nimbusgroup.us").rstrip("/")


def dev_login_enabled() -> bool:
    """Log in with just an e-mail address – for local development and tests only."""
    return env_flag("AUTH_DEV_LOGIN")


def session_ttl_hours() -> int:
    return env_int("SESSION_TTL_HOURS", 168)  # 7 days, extended while the user keeps working


def file_storage() -> str:
    """Where new original PDFs are kept: 'db' (default, inside the database) or 'local' (LOCAL_FILE_DIR)."""
    return env("FILE_STORAGE", "db").lower()


def local_file_dir() -> Path:
    return Path(env("LOCAL_FILE_DIR") or PROJECT_DIR / "data" / "files")


def sync_jobs() -> bool:
    """Run conversions inside the request instead of a background thread (tests)."""
    return env_flag("CIV_SYNC_JOBS")
