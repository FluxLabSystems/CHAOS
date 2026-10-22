"""The backup-age monitor: ``backup_age_h`` published from the newest archive.

``backup_overdue`` in data/alarm_definitions.yaml watches
``it.application_service.rack_01.postgres_01/backup_age_h`` and could never
fire, because nothing published the point. These tests pin the monitor that
now does, and -- the one that matters -- that the shipped alarm actually
activates once an archive is old enough.

Nothing sleeps: the monitor's clock and ``now`` are injected.
"""

from __future__ import annotations

import datetime as dt
import json
import tarfile
from pathlib import Path

import pytest

from chaos import cli, topics
from chaos.config import Settings
from chaos.envelope import parse_telemetry
from chaos.ingest.backup_age import (
    POINT_NAME,
    SOURCE,
    BackupAgeService,
    newest_backup,
    read_backup_manifest,
)
from chaos.ingest.service import IngestService
from chaos.models.registry import ConfigurationRevision
from chaos.models.telemetry import CurrentState
from chaos.runtime import build_services

T0 = dt.datetime(2026, 8, 7, 12, 0, 0, tzinfo=dt.UTC)
POSTGRES = "it.application_service.rack_01.postgres_01"


def hours(n: float) -> dt.timedelta:
    return dt.timedelta(hours=n)


def write_archive(directory: Path, name: str, *, created_at: dt.datetime, kind: str = "chaos-backup") -> Path:
    """A minimal archive shaped like `chaos backup` writes: a manifest and nothing else needed."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    manifest = {
        "kind": kind,
        "format_version": 1,
        "created_at": created_at.isoformat(),
        "platform_version": "0.4.0",
        "node_role": "primary",
        "row_counts": {},
    }
    with tarfile.open(path, "w:gz") as tar:
        data = json.dumps(manifest).encode("utf-8")
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        import io

        tar.addfile(info, io.BytesIO(data))
        filler = b"x" * 64
        info = tarfile.TarInfo("tables/assets.json")
        info.size = len(filler)
        tar.addfile(info, io.BytesIO(filler))
    return path


@pytest.fixture()
def backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture()
def monitor_settings(backup_dir: Path) -> Settings:
    return Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
        backup_directory=str(backup_dir),
        default_stale_after_s=60,
    )


@pytest.fixture()
def monitor(session_factory, bus, monitor_settings) -> BackupAgeService:
    return BackupAgeService(session_factory, bus, monitor_settings, clock=lambda: T0)


# ---------------------------------------------------------------------------
# Reading archives
# ---------------------------------------------------------------------------


def test_manifest_is_read_without_touching_the_tables(backup_dir):
    path = write_archive(backup_dir, "a.tar.gz", created_at=T0 - hours(5))
    manifest = read_backup_manifest(path)
    assert manifest is not None
    assert manifest["created_at"] == (T0 - hours(5)).isoformat()


def test_a_tarball_that_is_not_a_chaos_backup_is_ignored(backup_dir, caplog):
    write_archive(backup_dir, "other.tar.gz", created_at=T0 - hours(1), kind="something-else")
    with caplog.at_level("WARNING"):
        assert newest_backup(backup_dir, cache={}) is None
    assert "not a chaos backup archive" in caplog.text


def test_a_corrupt_file_is_ignored(backup_dir):
    backup_dir.mkdir()
    (backup_dir / "broken.tar.gz").write_bytes(b"this is not a tar file")
    assert read_backup_manifest(backup_dir / "broken.tar.gz") is None
    assert newest_backup(backup_dir, cache={}) is None


def test_newest_is_by_manifest_created_at_not_by_name(backup_dir):
    write_archive(backup_dir, "z-old.tar.gz", created_at=T0 - hours(48))
    write_archive(backup_dir, "a-new.tar.gz", created_at=T0 - hours(2))
    newest = newest_backup(backup_dir, cache={})
    assert newest is not None
    assert newest.path.name == "a-new.tar.gz"
    assert newest.created_at == T0 - hours(2)


def test_archives_are_parsed_once_and_reread_when_they_change(backup_dir, monkeypatch):
    import chaos.ingest.backup_age as module

    write_archive(backup_dir, "a.tar.gz", created_at=T0 - hours(2))
    cache: dict = {}
    calls: list[Path] = []
    real = module.read_backup_manifest
    monkeypatch.setattr(module, "read_backup_manifest", lambda path: calls.append(path) or real(path))
    newest_backup(backup_dir, cache=cache)
    newest_backup(backup_dir, cache=cache)
    assert len(calls) == 1
    write_archive(backup_dir, "a.tar.gz", created_at=T0 - hours(1))  # rewritten: size/mtime change
    newest_backup(backup_dir, cache=cache)
    assert len(calls) == 2


def test_a_missing_directory_means_no_archive(tmp_path):
    assert newest_backup(tmp_path / "nope", cache={}) is None


def test_real_chaos_backup_archives_are_recognised(tmp_path):
    """The reader must accept what `chaos backup` writes, not a lookalike."""
    db_url = f"sqlite:///{tmp_path / 'b.db'}"
    assert cli.main(["--database-url", db_url, "init-db"]) == cli.EXIT_OK
    out = tmp_path / "out"
    out.mkdir()
    assert cli.main(["--database-url", db_url, "backup", "--output", str(out)]) == cli.EXIT_OK
    newest = newest_backup(out, cache={})
    assert newest is not None
    assert newest.node_role == "primary"
    assert abs((dt.datetime.now(dt.UTC) - newest.created_at).total_seconds()) < 120


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


def test_tick_publishes_the_age_of_the_newest_archive(monitor, bus, backup_dir):
    write_archive(backup_dir, "a.tar.gz", created_at=T0 - hours(30))
    reading = monitor.tick(T0)

    assert reading.age_h == 30.0
    assert reading.quality == "calculated"
    message = bus.last(topics.telemetry_topic(POSTGRES, POINT_NAME, "chaos"))
    assert message is not None
    envelope = parse_telemetry(message.payload)
    assert envelope.asset_id == POSTGRES
    assert envelope.point == POINT_NAME
    assert envelope.value == 30.0
    assert envelope.unit == "h"
    assert envelope.quality == "calculated"
    assert envelope.source == SOURCE
    assert message.retain is False
    assert monitor.counters["published"] == 1


def test_no_archive_publishes_a_lower_bound_marked_uncertain(monitor, bus, backup_dir, db_session):
    """Never a reassuring zero: the age is at least the platform's own age."""
    db_session.add(
        ConfigurationRevision(
            target_type="registry",
            target_id="design-package",
            changed_by="loader",
            applied_at=T0 - hours(72),
        )
    )
    db_session.commit()
    monitor.start()
    try:
        reading = monitor.last_reading
    finally:
        monitor.stop()
    assert reading is not None
    assert reading.archive is None
    assert reading.quality == "uncertain"
    assert reading.age_h == 72.0
    assert "first configuration revision" in reading.basis
    envelope = parse_telemetry(bus.last(topics.telemetry_topic(POSTGRES, POINT_NAME, "chaos")).payload)
    assert envelope.quality == "uncertain"
    assert envelope.value == 72.0


def test_no_archive_and_no_revision_counts_from_the_monitor_start(monitor, backup_dir):
    monitor.start()
    try:
        first = monitor.last_reading
        later = monitor.tick(T0 + hours(50))
    finally:
        monitor.stop()
    assert first.age_h == 0.0 and first.quality == "uncertain"
    assert later.age_h == 50.0
    assert "this monitor started" in later.basis
    assert monitor.counters["no_archive"] == 2


def test_the_newest_archive_wins_and_the_age_is_never_negative(monitor, backup_dir):
    write_archive(backup_dir, "old.tar.gz", created_at=T0 - hours(100))
    write_archive(backup_dir, "future.tar.gz", created_at=T0 + hours(1))  # clock skew on another node
    reading = monitor.tick(T0)
    assert reading.archive.path.name == "future.tar.gz"
    assert reading.age_h == 0.0


def test_interval_stays_inside_the_stale_timeout(session_factory, bus, backup_dir):
    settings = Settings(database_url="sqlite://", mqtt_enabled=False, backup_directory=str(backup_dir))
    assert BackupAgeService(session_factory, bus, settings).interval_s == 30.0
    tight = Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        backup_directory=str(backup_dir),
        default_stale_after_s=10,
    )
    assert BackupAgeService(session_factory, bus, tight).interval_s == 5.0


def test_default_directory_is_where_chaos_backup_writes(session_factory, bus):
    settings = Settings(database_url="sqlite://", mqtt_enabled=False)
    service = BackupAgeService(session_factory, bus, settings)
    assert service.directory == settings.resolved_backup_directory
    assert service.directory.name == "backups" and service.directory.parent.name == "var"


def test_status_reports_what_was_published(monitor, backup_dir):
    write_archive(backup_dir, "a.tar.gz", created_at=T0 - hours(3))
    monitor.tick(T0)
    status = monitor.status()
    assert status["asset_id"] == POSTGRES
    assert status["last_reading"]["age_h"] == 3.0
    assert status["last_reading"]["archive"].endswith("a.tar.gz")
    assert status["counters"]["published"] == 1


# ---------------------------------------------------------------------------
# Role and registration
# ---------------------------------------------------------------------------


def test_the_monitor_runs_on_the_primary_only_and_needs_the_bus(session_factory, bus, backup_dir):
    def names(**overrides) -> set[str]:
        settings = Settings(
            database_url="sqlite://",
            ems_enabled=False,
            alarm_engine_enabled=False,
            backup_directory=str(backup_dir),
            **overrides,
        )
        return {service.name for service in build_services(settings, session_factory, bus).services}

    assert "backup-age" in names(mqtt_enabled=True, node_role="primary")
    assert "backup-age" not in names(mqtt_enabled=True, node_role="secondary")
    assert "backup-age" not in names(mqtt_enabled=False, node_role="primary")


# ---------------------------------------------------------------------------
# End to end: the shipped alarm fires
# ---------------------------------------------------------------------------


def test_backup_overdue_activates_from_a_stale_archive(
    session_factory, db_session, bus, loaded_registry, backup_dir
):
    """Monitor -> bus -> ingest -> current state -> alarm engine -> backup_overdue."""
    from chaos.alarms.definitions import sync_definitions
    from chaos.alarms.evaluator import AlarmEvaluator
    from chaos.models.alarms import Alarm

    sync_definitions(db_session, strict=True)
    db_session.commit()

    settings = Settings(
        database_url="sqlite://",
        mqtt_enabled=False,
        ems_enabled=False,
        alarm_engine_enabled=False,
        backup_directory=str(backup_dir),
    )
    ingest = IngestService(session_factory, bus, settings, auto_sweep=False, clock=lambda: T0)
    ingest.start()
    monitor = BackupAgeService(session_factory, bus, settings, clock=lambda: T0)
    write_archive(backup_dir, "last-week.tar.gz", created_at=T0 - hours(72))

    monitor.tick(T0)
    assert ingest.counters["messages_written"] == 1, ingest.counters
    db_session.expire_all()
    state = db_session.get(CurrentState, f"{POSTGRES}/{POINT_NAME}")
    assert state is not None and state.value_numeric == 72.0 and state.quality == "calculated"

    evaluator = AlarmEvaluator(db_session, bus, settings)
    evaluator.evaluate(T0)
    detected = db_session.query(Alarm).filter_by(alarm_key="backup_overdue").all()
    assert detected and detected[0].state == "detected"  # 300 s on-delay: seen, not yet active

    evaluator.evaluate(T0 + dt.timedelta(seconds=301))
    db_session.expire_all()
    alarm = db_session.query(Alarm).filter_by(alarm_key="backup_overdue").one()
    assert alarm.state == "active"
    assert alarm.asset_id == POSTGRES

    # A fresh backup clears it: the monitor republishes, the engine resets at <= 24 h.
    write_archive(backup_dir, "today.tar.gz", created_at=T0)
    monitor.tick(T0 + hours(1))
    evaluator.evaluate(T0 + hours(1))
    evaluator.evaluate(T0 + hours(1) + dt.timedelta(seconds=301))
    db_session.expire_all()
    assert db_session.get(Alarm, alarm.id).state == "cleared"
