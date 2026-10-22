"""Schema migrations, run through Alembic and configured in code.

``chaos.db.create_all`` adds tables that do not exist and never alters one that
does. That is enough for a fresh database and for nothing else: the moment a
column changes shape, a node with real telemetry in its historian needs its
schema moved forward in place. This package is that path.

There is no ``alembic.ini``. The script location is this package, the database
URL is the platform's own ``Settings.database_url``, and the metadata is
``Base.metadata`` -- so ``chaos migrate`` and the tests cannot drift from what
the API runs against. ``versions/`` holds the revisions; ``0001`` is the
baseline, equal to the metadata as it stood when migrations were introduced,
so a database built by ``init-db`` at that point can be stamped and carried on
from there.

Programmatic use takes an :class:`~sqlalchemy.engine.Engine`, not a URL,
because the CLI already holds one and a SQLite ``:memory:`` database only
exists on the engine that created it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import alembic.command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine
from sqlalchemy import inspect as sa_inspect

import chaos.models  # noqa: F401  (registers every mapper on Base.metadata)
from chaos.models.base import Base

#: The directory Alembic treats as ``script_location``: it holds ``env.py``,
#: ``script.py.mako`` and ``versions/``.
SCRIPT_LOCATION = Path(__file__).resolve().parent

#: Options env.py hands to Alembic, shared with :func:`metadata_differences` so
#: the drift test compares exactly the way autogenerate does.
#:
#: ``render_as_batch`` matters for SQLite, which cannot ALTER a column in
#: place; Alembic then rewrites a table through a copy. ``compare_type`` makes
#: autogenerate notice a changed column type, not only a changed column.
CONTEXT_OPTIONS: dict[str, Any] = {
    "target_metadata": Base.metadata,
    "render_as_batch": True,
    "compare_type": True,
    "compare_server_default": False,
}

#: The revision whose schema equals ``create_all`` on the day migrations were
#: introduced. ``init-db`` stamps a pre-existing, unstamped database here so
#: that ``chaos migrate`` then applies everything that came later.
BASELINE_REVISION = "0001"

#: The table Alembic keeps its bookmark in.
VERSION_TABLE = "alembic_version"


def alembic_config(database_url: str | None = None) -> Config:
    """An Alembic configuration equivalent to an ``alembic.ini``, built in code."""
    config = Config()
    config.set_main_option("script_location", str(SCRIPT_LOCATION))
    # Revision ids are chosen by hand (0001, 0002, ...) and named after what
    # they do; the template is the date-free one in this directory.
    config.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    config.set_main_option("prepend_sys_path", ".")
    if database_url is not None:
        # Alembic reads this with configparser interpolation on, so a literal
        # percent sign (URL-encoded passwords have them) must be doubled.
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def script_directory() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config())


def head_revision() -> str:
    """The single head. A branch would mean two people added a revision in parallel."""
    heads = script_directory().get_heads()
    if len(heads) != 1:
        raise RuntimeError(
            f"chaos.migrations has {len(heads)} heads ({', '.join(heads)}); expected exactly one"
        )
    return heads[0]


def current_revision(engine: Engine) -> str | None:
    """The revision recorded in the database, or ``None`` when none is."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def pending_revisions(engine: Engine) -> list[str]:
    """Revisions ``upgrade`` would apply, oldest first; empty means at head."""
    script = script_directory()
    current = current_revision(engine)
    walk = script.iterate_revisions("head", current)
    pending = [revision.revision for revision in walk if revision.revision != current]
    pending.reverse()
    return pending


def has_platform_tables(engine: Engine) -> bool:
    """True when any platform table exists, the version table aside."""
    names = set(sa_inspect(engine).get_table_names())
    names.discard(VERSION_TABLE)
    return bool(names)


def _bound_config(engine: Engine) -> Config:
    config = alembic_config(str(engine.url))
    config.attributes["engine"] = engine
    return config


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Apply every revision up to ``revision`` (``env.py`` reads the engine)."""
    alembic.command.upgrade(_bound_config(engine), revision)


def stamp(engine: Engine, revision: str = "head") -> None:
    """Record ``revision`` without running anything."""
    alembic.command.stamp(_bound_config(engine), revision)


def revision(message: str, rev_id: str, *, database_url: str, autogenerate: bool = True) -> Any:
    """Write a new revision file. Development only; the tests use it against SQLite."""
    config = alembic_config(database_url)
    return alembic.command.revision(config, message=message, rev_id=rev_id, autogenerate=autogenerate)


def metadata_differences(engine: Engine) -> list[Any]:
    """What autogenerate would emit for ``engine`` against the models' metadata.

    Empty means the live schema and ``Base.metadata`` agree. The comparison is
    the same one ``revision --autogenerate`` runs, with the same options env.py
    sets, so this is the test that the baseline and the models have not
    drifted apart.
    """
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts=dict(CONTEXT_OPTIONS))
        return compare_metadata(context, Base.metadata)
