"""Database engine construction."""

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine


def make_engine(url: SecretStr | str) -> Engine:
    """Create an engine whose sessions run in UTC.

    hide_parameters keeps bound values (raw webhook bodies, emails) out of SQLAlchemy
    error messages and logs. SQLAlchemy already masks the password in the URL's repr.
    """
    raw_url = url.get_secret_value() if isinstance(url, SecretStr) else url
    return create_engine(
        raw_url,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"options": "-c timezone=UTC", "connect_timeout": 5},
    )
