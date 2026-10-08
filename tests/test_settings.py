import pytest
from pydantic import ValidationError

from integrationkit.settings import Settings

FAKE_SECRET = "whsec_test_fake_settings_secret"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "WEBHOOK_SECRET",
        "SIGNATURE_TOLERANCE_SECONDS",
        "MAX_BODY_BYTES",
        "DATABASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def make_settings(**kwargs) -> Settings:
    # _env_file=None keeps a developer's real .env out of the tests.
    return Settings(_env_file=None, **kwargs)


def test_defaults_load_with_only_secret(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", FAKE_SECRET)
    settings = make_settings()
    assert settings.webhook_secret.get_secret_value() == FAKE_SECRET
    assert settings.signature_tolerance_seconds == 300
    assert settings.max_body_bytes == 262_144


def test_missing_secret_fails():
    with pytest.raises(ValidationError, match="webhook_secret"):
        make_settings()


@pytest.mark.parametrize("tolerance", ["0", "-1"])
def test_non_positive_tolerance_fails(monkeypatch, tolerance):
    monkeypatch.setenv("WEBHOOK_SECRET", FAKE_SECRET)
    monkeypatch.setenv("SIGNATURE_TOLERANCE_SECONDS", tolerance)
    with pytest.raises(ValidationError, match="signature_tolerance_seconds"):
        make_settings()


def test_secret_not_exposed_in_repr_or_str(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", FAKE_SECRET)
    settings = make_settings()
    assert FAKE_SECRET not in repr(settings)
    assert FAKE_SECRET not in str(settings)


def test_database_url_not_exposed_in_repr_or_str(monkeypatch):
    url = "postgresql+psycopg://user:fake_db_password@db.example.com:5432/app"
    monkeypatch.setenv("WEBHOOK_SECRET", FAKE_SECRET)
    monkeypatch.setenv("DATABASE_URL", url)
    settings = make_settings()
    assert settings.database_url.get_secret_value() == url
    assert "fake_db_password" not in repr(settings)
    assert "fake_db_password" not in str(settings)


def test_secret_not_exposed_in_validation_error(monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", FAKE_SECRET)
    monkeypatch.setenv("SIGNATURE_TOLERANCE_SECONDS", "0")
    with pytest.raises(ValidationError) as exc_info:
        make_settings()
    assert FAKE_SECRET not in str(exc_info.value)
