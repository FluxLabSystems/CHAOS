"""Schema migrations: the baseline, the drift check and the `chaos migrate` command.

All of it on SQLite, which is what CI and the Windows product run. The three
promises under test:

1. ``migrate`` on an empty database builds every table the models declare.
2. Autogenerate finds nothing to say about a migrated database -- the baseline
   and ``Base.metadata`` have not drifted apart.
3. ``init-db`` followed by ``migrate`` is a no-op, so the two entry points can
   be used in either order without one undoing the other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from chaos import cli, migrations
from chaos.db import create_all
from chaos.models.base import Base

VERSIONS_DIR = Path(migrations.SCRIPT_LOCATION) / "versions"


def _declared_tables() -> set[str]:
    import chaos.models  # noqa: F401

    return set(Base.metadata.tables)


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {migrations.VERSION_TABLE}


@pytest.fixture()
def db_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migrations.db'}"


@pytest.fixture()
def engine(db_url):
    engine = create_engine(db_url)
    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# The script directory itself
# ---------------------------------------------------------------------------


def test_there_is_exactly_one_head():
    assert migrations.head_revision()


def test_the_baseline_is_a_real_revision_with_no_timestamp():
    text_ = (VERSIONS_DIR / "0001_baseline.py").read_text(encoding="utf-8")
    assert 'revision = "0001"' in text_
    assert "down_revision = None" in text_
    assert not re.search(r"Create Date|\b(19|20)\d\d-\d\d-\d\d", text_), (
        "a generated date would make the file churn"
    )
    assert "please adjust" not in text_, "Alembic's scaffolding comments were left in"
    assert "astext_type=Text()" not in text_, "an unimported name that NameErrors on PostgreSQL"


def test_the_configuration_is_built_in_code_without_an_ini_file():
    config = migrations.alembic_config("sqlite:///x.db")
    assert config.get_main_option("script_location") == str(migrations.SCRIPT_LOCATION)
    assert config.get_main_option("sqlalchemy.url") == "sqlite:///x.db"
    assert not list(Path(migrations.SCRIPT_LOCATION).glob("*.ini"))


def test_a_percent_sign_in_the_url_survives_configparser():
    config = migrations.alembic_config("postgresql://u:p%40ss@db/homestead")
    assert config.get_main_option("sqlalchemy.url") == "postgresql://u:p%40ss@db/homestead"


# ---------------------------------------------------------------------------
# upgrade / stamp / drift
# ---------------------------------------------------------------------------


def test_upgrade_on_a_fresh_database_creates_every_table(engine):
    assert _tables(engine) == set()
    migrations.upgrade(engine)
    assert _tables(engine) == _declared_tables()
    assert migrations.current_revision(engine) == migrations.head_revision()
    assert migrations.pending_revisions(engine) == []


def test_autogenerate_sees_no_difference_after_an_upgrade(engine):
    migrations.upgrade(engine)
    assert migrations.metadata_differences(engine) == []


def test_autogenerate_sees_no_difference_after_create_all(engine):
    """The baseline and create_all must describe the same schema."""
    create_all(engine)
    assert migrations.metadata_differences(engine) == []


def test_autogenerate_does_notice_a_missing_table(engine):
    migrations.upgrade(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE spare_parts"))
    diffs = migrations.metadata_differences(engine)
    assert any(diff[0] == "add_table" and diff[1].name == "spare_parts" for diff in diffs)


def test_stamp_records_head_without_touching_the_schema(engine):
    migrations.stamp(engine)
    assert migrations.current_revision(engine) == migrations.head_revision()
    assert _tables(engine) == set()


def test_pending_revisions_on_an_empty_database_is_the_whole_history(engine):
    pending = migrations.pending_revisions(engine)
    assert pending and pending[0] == migrations.BASELINE_REVISION
    assert pending[-1] == migrations.head_revision()


def test_has_platform_tables_ignores_the_version_table(engine):
    assert migrations.has_platform_tables(engine) is False
    migrations.stamp(engine)
    assert migrations.has_platform_tables(engine) is False
    migrations.upgrade(engine)  # already stamped at head: nothing runs
    create_all(engine)
    assert migrations.has_platform_tables(engine) is True


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def test_migrate_on_a_fresh_database_creates_every_table(db_url, engine, capsys):
    assert cli.main(["--database-url", db_url, "migrate"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Pending  : 0001" in out
    assert "Applied  : 1 revision(s); now at 0001" in out
    assert _tables(engine) == _declared_tables()


def test_init_db_then_migrate_is_a_no_op(db_url, engine, capsys):
    assert cli.main(["--database-url", db_url, "init-db"]) == cli.EXIT_OK
    assert "Schema   : stamped at revision 0001 (head)" in capsys.readouterr().out
    before = _tables(engine)

    assert cli.main(["--database-url", db_url, "migrate"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "Pending  : none -- already at head" in out
    assert _tables(engine) == before == _declared_tables()
    assert migrations.metadata_differences(engine) == []


def test_migrate_then_init_db_is_a_no_op_too(db_url, engine, capsys):
    assert cli.main(["--database-url", db_url, "migrate"]) == cli.EXIT_OK
    capsys.readouterr()
    assert cli.main(["--database-url", db_url, "init-db"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "36 present, 0 created" in out or "0 created" in out
    assert "Schema   : at revision 0001 (head)" in out


def test_init_db_stamps_a_pre_migration_database_at_the_baseline(db_url, engine, capsys):
    """Tables that exist with no revision recorded are of unknown vintage: baseline, then migrate."""
    create_all(engine)
    assert cli.main(["--database-url", db_url, "init-db"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert f"existing tables stamped at baseline {migrations.BASELINE_REVISION}" in out
    assert migrations.current_revision(engine) == migrations.BASELINE_REVISION


def test_migrate_refuses_an_unstamped_database_that_already_has_tables(db_url, engine, capsys):
    create_all(engine)
    assert cli.main(["--database-url", db_url, "migrate"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "records no schema revision" in err
    assert "chaos migrate --stamp" in err
    assert migrations.current_revision(engine) is None


def test_migrate_stamp_marks_an_init_db_database_as_current(db_url, engine, capsys):
    create_all(engine)
    assert cli.main(["--database-url", db_url, "migrate", "--stamp"]) == cli.EXIT_OK
    assert "(nothing was run)" in capsys.readouterr().out
    assert migrations.current_revision(engine) == migrations.head_revision()
    assert cli.main(["--database-url", db_url, "migrate"]) == cli.EXIT_OK
    assert "already at head" in capsys.readouterr().out


def test_migrate_reports_a_missing_alembic_as_unavailable(db_url, monkeypatch, capsys):
    import builtins

    real_import = builtins.__import__

    def no_alembic(name, *args, **kwargs):
        if name == "alembic" or name.startswith("alembic."):
            raise ImportError("No module named 'alembic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(__import__("sys").modules, "chaos.migrations", raising=False)
    monkeypatch.setattr(builtins, "__import__", no_alembic)
    assert cli.main(["--database-url", db_url, "migrate"]) == cli.EXIT_UNAVAILABLE
    err = capsys.readouterr().err
    assert "Schema migrations unavailable" in err
    assert "pip install 'alembic>=1.13'" in err


def test_migrate_help_is_available(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["migrate", "--help"])
    assert excinfo.value.code == 0
    assert "--stamp" in capsys.readouterr().out
