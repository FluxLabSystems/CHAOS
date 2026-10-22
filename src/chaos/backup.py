"""Portable backup archives, shared by ``chaos backup`` and ``POST /api/v1/backup``.

SDD section 16.1 mitigation 3: critical configuration and asset data must exist
outside the combined battery/server container. One implementation produces the
archive whichever way it is asked for -- the command line on a node with no
browser, or the operator console's Backup action -- so the manifest, the table
set and the restore instructions can never drift between the two.

The table groups, the manifest constants and the row-dump helper stay in
:mod:`chaos.cli`, because ``restore`` and ``export`` share them; this module
imports them rather than keeping a second copy that a renamed table could
leave behind. ``chaos.cli`` imports nothing from here at module level, so the
dependency runs one way.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import json
import tarfile
from pathlib import Path
from typing import Any

from chaos import __version__
from chaos.cli import (
    ALARM_TABLES,
    BACKUP_FORMAT_VERSION,
    BACKUP_KIND,
    COMMAND_TABLES,
    ENERGY_TABLES,
    HISTORY_TABLES,
    MAINTENANCE_TABLES,
    REGISTRY_TABLES,
    STATE_TABLES,
    CommandError,
    _dump_table,
    _dumps,
    _metadata,
    _table_names,
    open_session,
    redact_url,
    workspace_root,
)

#: Archive file name pattern, the same whether the CLI or the API wrote it.
ARCHIVE_SUFFIX = ".tar.gz"

#: Settings keys whose values must never reach an archive.
_SECRET_TOKENS = ("password", "secret", "token", "key")


@dataclasses.dataclass
class BackupResult:
    """What one backup run produced."""

    path: Path
    size_bytes: int
    sha256: str
    created_at: dt.datetime
    node_role: str
    includes_history: bool
    row_counts: dict[str, int]
    tables_absent: list[str]

    @property
    def tables(self) -> int:
        return len(self.row_counts)

    @property
    def rows(self) -> int:
        return sum(self.row_counts.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at.isoformat(),
            "node_role": self.node_role,
            "includes_history": self.includes_history,
            "tables": self.tables,
            "rows": self.rows,
            "row_counts": dict(self.row_counts),
            "tables_absent": list(self.tables_absent),
        }


def backup_tables(include_history: bool) -> list[str]:
    """The tables an archive carries, in the order they are dumped."""
    names = [
        *REGISTRY_TABLES,
        *ALARM_TABLES,
        *COMMAND_TABLES,
        *ENERGY_TABLES,
        *MAINTENANCE_TABLES,
        *STATE_TABLES,
    ]
    if include_history:
        names.extend(HISTORY_TABLES)
    return names


def backup_directory(settings: Any) -> Path:
    """Where this node keeps its archives: ``var/backups`` under the workspace root.

    The same default ``chaos backup`` uses, so the console's listing and the
    scheduled task write to and read from one place.
    """
    return workspace_root(settings) / "var" / "backups"


def archive_name(settings: Any, now: dt.datetime) -> str:
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"homestead-{settings.node_role}-{stamp}{ARCHIVE_SUFFIX}"


def _restore_text(now: dt.datetime, node_role: str) -> str:
    return (
        "Project CHAOS backup\n"
        "=============================\n\n"
        f"Created  : {now.isoformat()}\n"
        f"Node role: {node_role}\n\n"
        "Contents\n"
        "  manifest.json      row counts and provenance\n"
        "  settings.json      redacted runtime settings (secrets are NOT included)\n"
        "  tables/*.json      one JSON array per database table\n"
        "  design-package/    data/*.yaml|json and schemas/*.json as loaded on this node\n\n"
        "Restore\n"
        "  1. chaos init-db                      # on the target node, CHAOS_DATABASE_URL set\n"
        "  2. chaos restore <this archive>       # validates manifest.json, restores every\n"
        "                                        # tables/*.json in dependency order, in one\n"
        "                                        # transaction; refuses a database that already\n"
        "                                        # holds rows unless --force replaces them\n"
        "  3. chaos status                       # confirm counts match manifest.json\n\n"
        "  chaos restore --dry-run <this archive> reports what would happen and writes nothing.\n"
        "  Alternatively 'chaos load-all' rebuilds the registry, load schedule and alarm\n"
        "  definitions from design-package/ -- registry content only: no alarm or command\n"
        "  history, no work orders, no notification log.\n\n"
        "This archive contains no MQTT, database or notification credentials. Those are\n"
        "per-device and must be re-issued, not restored (SDD section 15.2).\n\n"
        "Keep at least one copy off the property and one copy offline (SDD sections 15.8, 16.1).\n"
    )


def _redacted_settings(settings: Any) -> dict[str, Any]:
    payload = json.loads(settings.model_dump_json())
    for key in list(payload):
        if any(token in key for token in _SECRET_TOKENS):
            payload[key] = "***redacted***"
    payload["database_url"] = redact_url(settings.database_url)
    return payload


def _dump_all(session: Any, names: list[str]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    metadata = _metadata()
    present = _table_names(session.get_bind())
    dumps: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    for name in names:
        table = metadata.tables.get(name)
        if table is None or name not in present:
            missing.append(name)
            continue
        dumps[name] = _dump_table(session, table)
    return dumps, missing


def run_backup(
    settings: Any,
    output: Path | str | None = None,
    *,
    include_history: bool = False,
    session: Any = None,
    now: dt.datetime | None = None,
) -> BackupResult:
    """Write one portable archive and describe it.

    ``output`` may be an archive path, a directory (the archive gets the
    default name inside it) or ``None`` for :func:`backup_directory`.
    ``session`` lets a caller that already holds a database session -- the API
    -- reuse it; the CLI leaves it ``None`` and a session is opened from
    ``settings``. Raises :class:`chaos.cli.CommandError` when the archive cannot
    be written, with a hint the operator can act on.
    """
    now = now or dt.datetime.now(dt.UTC)
    default_name = archive_name(settings, now)
    target = Path(output) if output else backup_directory(settings) / default_name
    if target.is_dir():
        target = target / default_name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CommandError(f"Could not create {target.parent}: {exc}") from exc

    names = backup_tables(include_history)
    if session is None:
        with open_session(settings) as own_session:
            dumps, missing = _dump_all(own_session, names)
    else:
        dumps, missing = _dump_all(session, names)

    manifest = {
        "kind": BACKUP_KIND,
        "format_version": BACKUP_FORMAT_VERSION,
        "created_at": now.isoformat(),
        "platform_version": __version__,
        "node_role": settings.node_role,
        "site_id": settings.site_id,
        "database": redact_url(settings.database_url),
        "includes_history": bool(include_history),
        "row_counts": {name: len(rows) for name, rows in dumps.items()},
        "tables_absent": missing,
        "sdd_reference": "16.1 mitigation 3 (configuration replicated outside the container)",
    }

    def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = int(now.timestamp())
        info.mode = 0o600
        tar.addfile(info, io.BytesIO(data))

    try:
        with tarfile.open(target, "w:gz") as tar:
            # manifest.json goes first on purpose: a listing reads only the
            # first member, so a large history archive is never decompressed
            # whole just to say when it was taken.
            _add_bytes(tar, "manifest.json", _dumps(manifest, "json").encode("utf-8"))
            _add_bytes(tar, "RESTORE.txt", _restore_text(now, settings.node_role).encode("utf-8"))
            _add_bytes(tar, "settings.json", _dumps(_redacted_settings(settings), "json").encode("utf-8"))
            for name, rows in dumps.items():
                _add_bytes(tar, f"tables/{name}.json", _dumps(rows, "json").encode("utf-8"))

            data_dir = Path(settings.data_dir)
            if data_dir.is_dir():
                for path in sorted(data_dir.iterdir()):
                    if path.is_file():
                        tar.add(path, arcname=f"design-package/data/{path.name}")
            schema_dir = Path(settings.schema_dir)
            if schema_dir.is_dir():
                for path in sorted(schema_dir.iterdir()):
                    if path.is_file():
                        tar.add(path, arcname=f"design-package/schemas/{path.name}")
    except OSError as exc:
        raise CommandError(f"Could not write {target}: {exc}") from exc

    return BackupResult(
        path=target,
        size_bytes=target.stat().st_size,
        sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
        created_at=now,
        node_role=settings.node_role,
        includes_history=bool(include_history),
        row_counts=manifest["row_counts"],
        tables_absent=missing,
    )


# ---------------------------------------------------------------------------
# Listing what is already there
# ---------------------------------------------------------------------------


def read_manifest(path: Path) -> dict[str, Any]:
    """Read ``manifest.json`` from an archive without reading the rest of it.

    ``run_backup`` writes the manifest as the first member, so this stops after
    one entry. Raises :class:`ValueError` for anything that is not a readable
    archive with a manifest object at the top level.
    """
    try:
        with tarfile.open(path, "r:*") as tar:
            for member in tar:
                if member.name != "manifest.json" or not member.isfile():
                    continue
                handle = tar.extractfile(member)
                if handle is None:
                    break
                manifest = json.loads(handle.read().decode("utf-8"))
                if not isinstance(manifest, dict):
                    raise TypeError("manifest.json is not a JSON object")
                return manifest
    except (tarfile.TarError, OSError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise ValueError(f"not a readable backup archive: {exc}") from exc
    raise ValueError("no manifest.json in the archive")


def manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    """The part of a manifest a listing shows: provenance and size in rows."""
    row_counts = manifest.get("row_counts")
    counts = row_counts if isinstance(row_counts, dict) else {}
    return {
        "kind": manifest.get("kind"),
        "format_version": manifest.get("format_version"),
        "created_at": manifest.get("created_at"),
        "platform_version": manifest.get("platform_version"),
        "node_role": manifest.get("node_role"),
        "site_id": manifest.get("site_id"),
        "includes_history": bool(manifest.get("includes_history")),
        "tables": len(counts),
        "rows": sum(int(v) for v in counts.values() if isinstance(v, (int, float))),
        "tables_absent": list(manifest.get("tables_absent") or []),
        "restorable": manifest.get("kind") == BACKUP_KIND
        and manifest.get("format_version") == BACKUP_FORMAT_VERSION,
    }


def list_archives(directory: Path) -> list[dict[str, Any]]:
    """Every ``*.tar.gz`` in ``directory``, newest first, each with its manifest.

    An archive whose manifest cannot be read is still listed, with ``error``
    set: a file in the backup directory that is not a backup is exactly what an
    operator checking the nightly job needs to see.
    """
    if not directory.is_dir():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or not path.name.endswith(ARCHIVE_SUFFIX):
            continue
        stat = path.stat()
        entry: dict[str, Any] = {
            "name": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC).isoformat(),
            "manifest": None,
            "error": None,
        }
        try:
            entry["manifest"] = manifest_summary(read_manifest(path))
        except ValueError as exc:
            entry["error"] = str(exc)
        entries.append(entry)

    # Readable archives first, newest first by the time they say they were
    # taken; files that could not be read as backups follow, newest first by
    # modification time, so a stray file never hides the latest real archive.
    def _key(item: dict[str, Any]) -> tuple[int, str, str]:
        manifest = item["manifest"]
        if manifest:
            return (1, str(manifest.get("created_at") or item["modified_at"]), item["name"])
        return (0, item["modified_at"], item["name"])

    entries.sort(key=_key, reverse=True)
    return entries
