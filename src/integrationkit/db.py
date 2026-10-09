"""Database engine construction and error classification."""

from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

# SQLSTATEs meaning "the database is not available right now", not "our query is wrong":
# admin_shutdown, crash_shutdown, cannot_connect_now, too_many_connections.
UNAVAILABLE_SQLSTATES = frozenset({"57P01", "57P02", "57P03", "53300"})


def is_database_unavailable(exc: BaseException) -> bool:
    """True for outages (connection refused, server restarting, pool exhausted)."""
    if isinstance(exc, PoolTimeoutError):
        return True
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    if isinstance(exc, (OperationalError, InterfaceError)):
        sqlstate = getattr(exc.orig, "sqlstate", None)
        # Connection failures happen before the server sends any SQLSTATE.
        return sqlstate is None or sqlstate.startswith("08") or sqlstate in UNAVAILABLE_SQLSTATES
    return False


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
