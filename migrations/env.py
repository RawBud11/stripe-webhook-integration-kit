"""Alembic environment. Online migrations only."""

from logging.config import fileConfig

from alembic import context

from integrationkit.db import make_engine
from integrationkit.models import Base
from integrationkit.settings import Settings

config = context.config

# The CLI (and the container start) logs each migration via alembic.ini. Code that runs
# migrations in-process, like the tests, sets configure_logger to False so its own
# logging (pytest's caplog handlers) is not replaced.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def database_url():
    url = config.attributes.get("database_url")
    return url if url is not None else Settings().database_url


def run_migrations_online() -> None:
    engine = make_engine(database_url())
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


run_migrations_online()
