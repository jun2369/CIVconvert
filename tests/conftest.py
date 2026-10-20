import os
from pathlib import Path

import pytest

# conversions run inside the request in tests (no background thread)
os.environ["CIV_SYNC_JOBS"] = "1"

# Real invoices are kept out of git (samples/ is ignored); tests that need one are skipped without it.
SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "8100800394 Commercial Invoice.pdf"

needs_sample = pytest.mark.skipif(not SAMPLE.is_file(), reason=f"sample invoice not found: {SAMPLE}")

API_KEY = "test-key-123"


@pytest.fixture
def api_key(monkeypatch) -> dict[str, str]:
    """The conversion API needs a key (or a login): headers to send."""
    monkeypatch.setenv("API_KEY", API_KEY)
    return {"X-API-Key": API_KEY}


@pytest.fixture
def database(tmp_path):
    """A fresh database with the migrated schema: SQLite, or TEST_DATABASE_URL (e.g. a PostgreSQL to try)."""
    from civconvert import db

    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{(tmp_path / 'civconvert-test.db').as_posix()}"
    db.configure(url)
    if url.startswith("postgresql"):  # start from an empty schema
        with db.engine().begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
    db.migrate()
    yield url
    db.engine().dispose()


@pytest.fixture
def app_client(database, monkeypatch):
    """The app with its start-up (migrations) run, dev login allowed."""
    from fastapi.testclient import TestClient

    from civconvert.api import app

    monkeypatch.setenv("AUTH_DEV_LOGIN", "1")
    with TestClient(app) as client:
        yield client


def login(client, email: str = "op.one@agslogistics.com", name: str | None = None) -> dict:
    res = client.post("/api/auth/dev-login", json={"email": email, "name": name})
    assert res.status_code == 200, res.text
    client.headers["X-CIV-Request"] = "1"
    return res.json()["user"]


@pytest.fixture
def user_client(app_client):
    login(app_client)
    return app_client
