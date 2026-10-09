import pytest
from alembic import command
from alembic.config import Config
from db_support import (
    ROOT,
    UnsafeTestDatabaseError,
    assert_safe_test_database,
    configured_test_database_url,
    describe,
    require_db,
)
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from integrationkit.db import make_engine


def _unavailable(reason: str):
    hint = "start it with: docker compose up -d db; set REQUIRE_DB=1 to fail instead of skip"
    if require_db():
        pytest.fail(f"REQUIRE_DB=1 and test database unavailable: {reason}", pytrace=False)
    pytest.skip(f"test database unavailable: {reason} ({hint})")


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = configured_test_database_url()
    if url is None:
        _unavailable("TEST_DATABASE_URL is not set")
    try:
        # A wrong name is a configuration error, never a reason to skip.
        assert_safe_test_database(url)
    except UnsafeTestDatabaseError as exc:
        pytest.fail(str(exc), pytrace=False)
    return url


@pytest.fixture(scope="session")
def db_engine(test_database_url):
    engine = make_engine(test_database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        engine.dispose()
        _unavailable(f"cannot connect to {describe(test_database_url)}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def alembic_config(test_database_url) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = assert_safe_test_database(test_database_url)
    # Keep alembic.ini's logging config away from pytest's log capture.
    config.attributes["configure_logger"] = False
    return config


def reset_schema(engine, url: str) -> None:
    """Drop everything in the test database. Only ever runs on a `_test` database."""
    assert_safe_test_database(url)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def migrated_db(db_engine, alembic_config, test_database_url):
    """A freshly migrated schema for each test, whatever state the last test left."""
    reset_schema(db_engine, test_database_url)
    command.upgrade(alembic_config, "head")
    yield db_engine
    reset_schema(db_engine, test_database_url)
