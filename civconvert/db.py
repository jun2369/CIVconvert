"""Database connection (SQLAlchemy) and schema migrations (Alembic).

DATABASE_URL picks the database: PostgreSQL in production, SQLite locally. Only portable
column types are used (see models.py), so moving to another database or hosting platform
is a dump/restore plus a different DATABASE_URL.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .settings import database_url, env, env_flag

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
log = logging.getLogger("civconvert")
_lock = threading.Lock()
_engine: Engine | None = None
_sessions: sessionmaker[Session] | None = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite gives naive datetimes back – they are UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def iso(value: datetime | None) -> str | None:
    value = as_utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(conn, _):  # foreign keys are off by default in SQLite
            cur = conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


def configure(url: str | None = None) -> Engine:
    """(Re)connect – tests point this at a temporary database."""
    global _engine, _sessions
    if url is None and env_flag("REQUIRE_DATABASE_URL") and not env("DATABASE_URL"):
        # set in the Docker image: a container's own disk is lost on every redeploy
        raise RuntimeError("DATABASE_URL is not set. In a container the database must be external (e.g. PostgreSQL), "
                           "or point DATABASE_URL at a file on a mounted volume: sqlite:////data/civconvert.db")
    with _lock:
        if _engine is not None:
            _engine.dispose()
        _engine = _make_engine(url or database_url())
        _sessions = sessionmaker(_engine, expire_on_commit=False)
        return _engine


def engine() -> Engine:
    if _engine is None:
        configure()
    assert _engine is not None
    return _engine


def new_session() -> Session:
    engine()
    assert _sessions is not None
    return _sessions()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A session for background work: commit on success, roll back on error."""
    db = new_session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def migrate() -> None:
    """Bring the schema up to date (alembic upgrade head). Safe to run on every start."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection_engine"] = engine()
    command.upgrade(cfg, "head")
    log.info("database schema is up to date (%s)", engine().url.render_as_string(hide_password=True))
