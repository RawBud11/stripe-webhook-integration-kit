"""Test-database helpers: where the test database is, and the safety guard."""

import os
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


class UnsafeTestDatabaseError(RuntimeError):
    """Raised when the configured test database could be a real one."""


class TestDatabaseSettings(BaseSettings):
    __test__ = False  # not a pytest test class
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    test_database_url: SecretStr | None = None


def require_db() -> bool:
    return os.environ.get("REQUIRE_DB") == "1"


def configured_test_database_url() -> str | None:
    url = TestDatabaseSettings().test_database_url
    return url.get_secret_value() if url is not None else None


def assert_safe_test_database(url: str | None) -> str:
    """Refuse any database whose name does not end in `_test`.

    Migration tests drop tables, so pointing them at a real database must be impossible.
    Messages show the database name only, never the password.
    """
    if not url:
        raise UnsafeTestDatabaseError("no test database URL configured")
    name = make_url(url).database or ""
    if not name.endswith("_test"):
        raise UnsafeTestDatabaseError(
            f"refusing to use database {name!r}: test database names must end in '_test'"
        )
    return url


def describe(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)
