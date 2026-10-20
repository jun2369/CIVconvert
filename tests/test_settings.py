import pytest

from civconvert import db, settings


@pytest.fixture
def no_database_env(monkeypatch):
    for name in settings.DATABASE_URL_NAMES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_database_url_from_a_linked_postgres_addon(no_database_env):
    no_database_env.setenv("POSTGRES_URI", "postgresql://user:pw@primary.db:5432/civ")
    assert settings.database_url() == "postgresql+psycopg://user:pw@primary.db:5432/civ"
    no_database_env.setenv("DATABASE_URL", "postgres://other:pw@host/db")  # DATABASE_URL wins
    assert settings.database_url() == "postgresql+psycopg://other:pw@host/db"


def test_container_refuses_to_start_without_a_database(no_database_env):
    no_database_env.setenv("REQUIRE_DATABASE_URL", "1")
    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        db.configure()
