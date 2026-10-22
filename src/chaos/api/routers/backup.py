"""Backup from the console (SDD section 16.1 mitigation 3).

Two endpoints over :mod:`chaos.backup`, which is the same code ``chaos backup``
runs: ``POST /backup`` writes one archive into this node's backup directory
and ``GET /backup`` lists what is already there, each with the manifest it
carries. Nothing here invents a second archive format, and nothing here
restores -- a restore replaces the database under a running platform and stays
a command-line procedure done against a stopped node.

Writing an archive is a maintainer action and is audited like one: the actor,
the archive path and the row counts land in the audit log, so "who took the
backup the night before the container flooded" has an answer.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from chaos.api.deps import AppSettings, DbSession, MaintainerPrincipal
from chaos.backup import backup_directory, list_archives, run_backup
from chaos.cli import CommandError
from chaos.models.base import utcnow
from chaos.models.commands import AuditLogEntry

router = APIRouter(tags=["backup"])


class BackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_history: bool = Field(
        default=False,
        description=(
            "Also dump telemetry samples and ingest dead letters. Off by default because "
            "history dominates archive size; the registry and the audit trail are the part "
            "that must survive the container."
        ),
    )


@router.get("/backup", summary="Archives in this node's backup directory")
def list_backups(settings: AppSettings) -> dict[str, Any]:
    """Newest first, each with its manifest summary -- or the reason it has none."""
    directory = backup_directory(settings)
    archives = list_archives(directory)
    return {
        "directory": str(directory),
        "exists": directory.is_dir(),
        "count": len(archives),
        "archives": archives,
        "note": (
            "An archive on this node is one copy in the same failure domain as the database. "
            "Keep a copy on the secondary node and one offline, off the property (SDD 15.8, 16.1)."
        ),
    }


@router.post("/backup", status_code=status.HTTP_201_CREATED, summary="Write a backup archive now")
def create_backup(
    body: BackupRequest,
    session: DbSession,
    settings: AppSettings,
    principal: MaintainerPrincipal,
) -> dict[str, Any]:
    """Run ``chaos backup`` through the API and describe the archive it wrote."""
    try:
        result = run_backup(settings, include_history=body.include_history, session=session)
    except CommandError as exc:
        session.rollback()
        detail = str(exc) if not exc.hint else f"{exc} ({exc.hint})"
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, detail) from exc

    payload = result.as_dict()
    session.add(
        AuditLogEntry(
            actor=principal.name,
            actor_role=principal.role,
            action="backup.create",
            target_type="backup",
            target_id=result.path.name,
            outcome="written",
            reason=None,
            detail={
                "path": payload["path"],
                "size_bytes": payload["size_bytes"],
                "sha256": payload["sha256"],
                "tables": payload["tables"],
                "rows": payload["rows"],
                "includes_history": payload["includes_history"],
            },
            occurred_at=utcnow(),
        )
    )
    session.commit()
    payload["written_by"] = principal.name
    payload["note"] = "Copy this archive off the property and keep one offline copy (SDD 15.8, 16.1)."
    return payload
