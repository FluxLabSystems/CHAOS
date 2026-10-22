"""The backup-age monitor: ``backup_age_h`` as a published calculated point.

``data/alarm_definitions.yaml`` raises ``backup_overdue`` when
``it.application_service.rack_01.postgres_01/backup_age_h`` exceeds 48 h. The
point has always existed in the registry -- the annunciator counted the tile as
serviceable and showed it *normal* -- but nothing ever published a value, so
the alarm could never fire and the tile was a reassurance nobody had earned.

This service publishes the value. Every tick it looks at the configured backup
directory (``CHAOS_BACKUP_DIRECTORY``; ``chaos backup`` writes there by
default), reads ``manifest.json`` out of the newest archive -- the manifest
only, never the tables, so a large archive costs nothing -- and publishes
*hours since that archive was created* as a ``calculated`` telemetry envelope
on the asset's ordinary telemetry topic. Ingest resolves it through the
registry like any other reading, the alarm engine evaluates it like any other
point, and the tile tells the truth.

Two honesty rules:

* **No archive is not "zero hours".** When the directory holds no readable
  archive, the true age is unknown but certainly *at least* the time since
  this platform first existed. That lower bound is published with quality
  ``uncertain`` -- measured from the first configuration revision in the
  database, or failing that from the moment this service started -- so a node
  that has never been backed up does eventually raise ``backup_overdue``
  instead of sitting quietly normal.
* **Only archives ``chaos backup`` wrote count.** A ``.tar.gz`` without a
  ``chaos-backup`` manifest is not a backup of this platform, whatever it is
  named; it is logged once and ignored.

The publish cadence is well inside the point's stale timeout, so a monitor
that stops is visible as a stale point rather than as a frozen age.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import tarfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from chaos import topics
from chaos.config import Settings
from chaos.envelope import TelemetryEnvelope
from chaos.models.base import utcnow
from chaos.models.registry import ConfigurationRevision
from chaos.mqtt import MessageBus

logger = logging.getLogger(__name__)

POINT_NAME = "backup_age_h"
SOURCE = "backup-monitor"

#: Manifest fields ``chaos backup`` writes (see ``chaos.backup.run_backup``). The
#: kind is checked by name so an unrelated tarball is never mistaken for one.
MANIFEST_KIND = "chaos-backup"
MANIFEST_MEMBER = "manifest.json"

#: Publish often enough that the point never goes stale between ticks; the
#: default stale timeout is 60 s, and the value changes by minutes per hour.
DEFAULT_INTERVAL_S = 30.0


@dataclass(frozen=True)
class BackupArchive:
    """The newest readable archive, as far as its manifest goes."""

    path: Path
    created_at: dt.datetime
    node_role: str | None
    platform_version: str | None


@dataclass(frozen=True)
class BackupAgeReading:
    """What one tick published."""

    at: dt.datetime
    age_h: float
    quality: str
    archive: BackupArchive | None
    basis: str


def read_backup_manifest(path: Path) -> dict | None:
    """``manifest.json`` from a ``chaos backup`` archive, or ``None`` if it is not one.

    Only the manifest member is read; the tables stay in the archive.
    """
    try:
        with tarfile.open(path, "r:*") as tar:
            try:
                member = tar.getmember(MANIFEST_MEMBER)
            except KeyError:
                return None
            handle = tar.extractfile(member)
            if handle is None:
                return None
            manifest = json.loads(handle.read().decode("utf-8"))
    except (tarfile.TarError, OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("kind") != MANIFEST_KIND:
        return None
    return manifest


def _parse_created_at(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def newest_backup(
    directory: Path, *, cache: dict[tuple[str, int, int], BackupArchive | None] | None = None
) -> BackupArchive | None:
    """The archive with the latest manifest ``created_at`` under ``directory``.

    ``cache`` maps ``(path, mtime_ns, size)`` to the parsed result so an archive
    is opened once, not on every tick; a changed file is re-read.
    """
    if not directory.is_dir():
        return None
    newest: BackupArchive | None = None
    for path in sorted(directory.glob("*.tar.gz")):
        try:
            stat = path.stat()
        except OSError:
            continue
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        if cache is not None and key in cache:
            archive = cache[key]
        else:
            manifest = read_backup_manifest(path)
            created_at = _parse_created_at(manifest.get("created_at")) if manifest else None
            if manifest is None or created_at is None:
                logger.warning("%s is not a chaos backup archive (no usable manifest); ignoring it", path)
                archive = None
            else:
                archive = BackupArchive(
                    path=path,
                    created_at=created_at,
                    node_role=manifest.get("node_role"),
                    platform_version=manifest.get("platform_version"),
                )
            if cache is not None:
                cache[key] = archive
        if archive is not None and (newest is None or archive.created_at > newest.created_at):
            newest = archive
    return newest


class BackupAgeService:
    """Background service: newest archive's age -> ``backup_age_h`` on the bus."""

    name = "backup-age"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus,
        settings: Settings,
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._bus = bus
        self._settings = settings
        self.clock = clock
        # Never slower than half the stale timeout, or the point would expire
        # between two ticks and the alarm engine would stop evaluating it.
        self.interval_s = max(1.0, min(interval_s, settings.default_stale_after_s / 2))
        self.asset_id = settings.backup_age_asset_id
        self.directory = settings.resolved_backup_directory
        self.topic = topics.telemetry_topic(self.asset_id, POINT_NAME, settings.mqtt_base_topic)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cache: dict[tuple[str, int, int], BackupArchive | None] = {}
        self._started_at: dt.datetime | None = None
        self.last_reading: BackupAgeReading | None = None
        self.counters: dict[str, int] = {"ticks": 0, "published": 0, "no_archive": 0, "publish_errors": 0}

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = self.clock()
        self._stop.clear()
        self.tick()
        self._thread = threading.Thread(target=self._loop, name="backup-age-monitor", daemon=True)
        self._thread.start()
        logger.info(
            "Backup-age monitor started: %s -> %s every %.0f s", self.directory, self.topic, self.interval_s
        )

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.tick()
            except Exception:  # pragma: no cover - a bad tick must not kill the monitor
                logger.exception("Backup-age tick failed")

    # -- work ------------------------------------------------------------------

    def tick(self, now: dt.datetime | None = None) -> BackupAgeReading:
        """Compute and publish one reading. Returns what was published."""
        now = now or self.clock()
        self.counters["ticks"] += 1
        archive = newest_backup(self.directory, cache=self._cache)

        if archive is not None:
            age_h = max(0.0, (now - archive.created_at).total_seconds() / 3600.0)
            reading = BackupAgeReading(
                at=now,
                age_h=round(age_h, 3),
                quality="calculated",
                archive=archive,
                basis=f"manifest created_at of {archive.path.name}",
            )
        else:
            self.counters["no_archive"] += 1
            reference, reference_basis = self._reference_instant(now)
            age_h = max(0.0, (now - reference).total_seconds() / 3600.0)
            reading = BackupAgeReading(
                at=now,
                age_h=round(age_h, 3),
                quality="uncertain",
                archive=None,
                basis=f"no archive in {self.directory}; lower bound since {reference_basis}",
            )
            if self.counters["no_archive"] == 1:
                logger.warning(
                    "No chaos backup archive in %s; publishing %s as a lower bound (quality uncertain)",
                    self.directory,
                    POINT_NAME,
                )

        envelope = TelemetryEnvelope(
            ts=now,
            asset_id=self.asset_id,
            point=POINT_NAME,
            value=reading.age_h,
            unit="h",
            quality=reading.quality,
            source=SOURCE,
        )
        try:
            self._bus.publish(self.topic, envelope.to_payload(), qos=1, retain=False)
            self.counters["published"] += 1
        except Exception:  # pragma: no cover - a broker outage must not stop the monitor
            self.counters["publish_errors"] += 1
            logger.exception("Failed to publish %s", self.topic)
        self.last_reading = reading
        return reading

    def _reference_instant(self, now: dt.datetime) -> tuple[dt.datetime, str]:
        """The earliest instant this platform is known to have existed.

        The first configuration revision is written when the registry is first
        loaded, which is the first moment there was anything worth backing up.
        """
        try:
            with self._session_factory() as session:
                first = session.execute(
                    select(func.min(ConfigurationRevision.applied_at))
                ).scalar_one_or_none()
        except Exception:
            first = None
        if first is not None:
            if first.tzinfo is None:
                first = first.replace(tzinfo=dt.UTC)
            return first, "the first configuration revision"
        started = self._started_at or now
        return started, "this monitor started"

    def status(self) -> dict:
        """For `chaos status` and diagnostics."""
        reading = self.last_reading
        return {
            "directory": str(self.directory),
            "asset_id": self.asset_id,
            "topic": self.topic,
            "interval_s": self.interval_s,
            "last_reading": None
            if reading is None
            else {
                "at": reading.at.isoformat(),
                "age_h": reading.age_h,
                "quality": reading.quality,
                "archive": None if reading.archive is None else str(reading.archive.path),
                "basis": reading.basis,
            },
            "counters": dict(self.counters),
        }
