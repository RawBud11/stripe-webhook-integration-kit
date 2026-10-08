"""Alembic environment. Online migrations only."""

from alembic import context

from integrationkit.db import make_engine
from integrationkit.models import Base
from integrationkit.settings import Settings

config = context.config


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
