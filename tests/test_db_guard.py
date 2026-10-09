"""The guard that keeps destructive tests away from non-test databases. No database needed."""

import pytest
from db_support import UnsafeTestDatabaseError, assert_safe_test_database

BASE = "postgresql+psycopg://user:fake_pw_for_guard@localhost:5433/"


@pytest.mark.parametrize("name", ["integrationkit_test", "x_test", "_test"])
def test_names_ending_in_test_are_allowed(name):
    assert assert_safe_test_database(BASE + name) == BASE + name


@pytest.mark.parametrize(
    "name",
    [
        "integrationkit",
        "integrationkit_test_backup",
        "test_integrationkit",
        "integrationkit_TEST",
        "integrationkittest",
        "",
    ],
)
def test_other_names_are_refused(name):
    with pytest.raises(UnsafeTestDatabaseError, match="must end in '_test'"):
        assert_safe_test_database(BASE + name)


@pytest.mark.parametrize("url", [None, ""])
def test_missing_url_is_refused(url):
    with pytest.raises(UnsafeTestDatabaseError):
        assert_safe_test_database(url)


def test_refusal_message_does_not_contain_password():
    with pytest.raises(UnsafeTestDatabaseError) as exc_info:
        assert_safe_test_database(BASE + "integrationkit")
    assert "fake_pw_for_guard" not in str(exc_info.value)
