"""Alembic environment: the models' metadata against the platform's database.

Alembic executes this file as a script whenever it runs a command; nothing
imports it. Everything it needs to know -- the metadata, the comparison
options, where the engine comes from -- is defined in ``chaos.migrations`` so
that ``chaos migrate`` and the drift test share one definition.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from chaos.migrations import CONTEXT_OPTIONS


def _run_offline() -> None:
    """Emit SQL for the configured dialect without connecting (``--sql``)."""
    context.configure(
        url=context.config.get_main_option("sqlalchemy.url"), literal_binds=True, **CONTEXT_OPTIONS
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_online() -> None:
    """Run against the engine the caller handed over, or one built from the URL.

    ``chaos.migrations.upgrade`` passes the CLI's engine in
    ``config.attributes`` so an in-memory SQLite database, which exists only on
    the engine that created it, is migrated rather than a fresh empty one.
    """
    engine = context.config.attributes.get("engine")
    if engine is None:
        engine = create_engine(context.config.get_main_option("sqlalchemy.url"))
    with engine.connect() as connection:
        context.configure(connection=connection, **CONTEXT_OPTIONS)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    _run_online()
