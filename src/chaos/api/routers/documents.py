"""Document records: the manuals, drawings and records linked to an asset.

The registry's ``documents`` table (SDD section 25, governance) holds metadata
only -- a title, a kind, a revision and where the document lives. The platform
stores no files: a URI may point at a share, a vendor portal or a drawer in the
container, and the console renders it as a link only when it is one. The
ZFS-backed document store the design document describes is not built, and
pretending otherwise here would be a liability in exactly the situation these
records exist for -- someone who was not there, trying to operate a subsystem
from the record (commissioning step 12).

Writes are maintainer actions and are audited with the actor, because a record
that says where the as-built drawing is should also say who put it there.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select

from chaos.api.deps import DbSession, MaintainerPrincipal
from chaos.models.base import utcnow
from chaos.models.commands import AuditLogEntry
from chaos.models.registry import Asset, Document

router = APIRouter(tags=["documents"])

#: The kinds a record may carry, in the order the console offers them. The
#: model's own docstring names the first five; the rest are what commissioning
#: step 12 asks people to attach.
DOCUMENT_KINDS: tuple[str, ...] = (
    "manual",
    "drawing",
    "datasheet",
    "photo",
    "warranty",
    "commissioning_record",
    "procedure",
    "certificate",
    "other",
)

DocumentKind = Literal[
    "manual",
    "drawing",
    "datasheet",
    "photo",
    "warranty",
    "commissioning_record",
    "procedure",
    "certificate",
    "other",
]


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


def _not_blank(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


class DocumentIn(BaseModel):
    """Body of ``POST /documents``."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = Field(
        default=None, description="Asset the document belongs to; blank for site-wide"
    )
    doc_type: DocumentKind = Field(description=f"One of: {', '.join(DOCUMENT_KINDS)}")
    title: str = Field(min_length=1, max_length=300)
    uri: str | None = Field(default=None, max_length=600, description="Where it lives: URL, UNC path, shelf")
    revision: str | None = Field(default=None, max_length=60)
    issued_at: dt.datetime | None = None
    notes: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()

    @field_validator("uri", "revision", "notes", "asset_id")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return _not_blank(value)


class DocumentPatch(BaseModel):
    """Body of ``PATCH /documents/{id}``: only the fields sent are changed."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str | None = None
    doc_type: DocumentKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=300)
    uri: str | None = Field(default=None, max_length=600)
    revision: str | None = Field(default=None, max_length=60)
    issued_at: dt.datetime | None = None
    notes: str | None = None

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("title must not be blank")
        return value.strip() if value else value


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    asset_id: str | None = None
    doc_type: str
    title: str
    uri: str | None = None
    revision: str | None = None
    issued_at: dt.datetime | None = None
    notes: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class DocumentPageOut(BaseModel):
    items: list[DocumentOut]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_document(session, document_id: str) -> Document:
    document = session.get(Document, document_id)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown document: {document_id}")
    return document


def _require_asset(session, asset_id: str) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown asset: {asset_id}")
    return asset


def _audit(session, principal, action: str, document: Document, detail: dict[str, Any]) -> None:
    session.add(
        AuditLogEntry(
            actor=principal.name,
            actor_role=principal.role,
            action=action,
            target_type="document",
            target_id=document.id,
            outcome="applied",
            reason=None,
            detail={"asset_id": document.asset_id, "title": document.title, **detail},
            occurred_at=utcnow(),
        )
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@router.get("/documents/kinds", summary="The document kinds a record may carry")
def list_document_kinds() -> list[str]:
    return list(DOCUMENT_KINDS)


@router.get("/documents", response_model=DocumentPageOut, summary="List document records")
def list_documents(
    session: DbSession,
    asset_id: Annotated[str | None, Query(description="Exact asset ID")] = None,
    doc_type: Annotated[str | None, Query(description="One document kind")] = None,
    q: Annotated[str | None, Query(description="Substring of the title")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentPageOut:
    statement = select(Document)
    if asset_id:
        statement = statement.where(Document.asset_id == asset_id)
    if doc_type:
        statement = statement.where(Document.doc_type == doc_type)
    if q:
        statement = statement.where(Document.title.contains(q))
    total = session.execute(select(func.count()).select_from(statement.subquery())).scalar_one()
    rows = session.execute(
        statement.order_by(Document.doc_type, Document.title, Document.id).limit(limit).offset(offset)
    ).scalars()
    return DocumentPageOut(
        items=[DocumentOut.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/assets/{asset_id}/documents",
    response_model=list[DocumentOut],
    summary="Document records linked to one asset",
)
def list_asset_documents(asset_id: str, session: DbSession) -> list[DocumentOut]:
    _require_asset(session, asset_id)
    rows = session.execute(
        select(Document)
        .where(Document.asset_id == asset_id)
        .order_by(Document.doc_type, Document.title, Document.id)
    ).scalars()
    return [DocumentOut.model_validate(row) for row in rows]


@router.get("/documents/{document_id}", response_model=DocumentOut, summary="One document record")
def get_document(document_id: str, session: DbSession) -> DocumentOut:
    return DocumentOut.model_validate(_require_document(session, document_id))


# ---------------------------------------------------------------------------
# Writes (maintainer)
# ---------------------------------------------------------------------------


@router.post(
    "/documents",
    response_model=DocumentOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a document record",
)
def create_document(payload: DocumentIn, session: DbSession, principal: MaintainerPrincipal) -> DocumentOut:
    if payload.asset_id:
        _require_asset(session, payload.asset_id)
    document = Document(**payload.model_dump())
    session.add(document)
    session.flush()
    _audit(session, principal, "document.create", document, {"doc_type": document.doc_type})
    session.commit()
    return DocumentOut.model_validate(document)


@router.patch("/documents/{document_id}", response_model=DocumentOut, summary="Update a document record")
def update_document(
    document_id: str, payload: DocumentPatch, session: DbSession, principal: MaintainerPrincipal
) -> DocumentOut:
    document = _require_document(session, document_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Nothing to change: the body names no field")
    if "asset_id" in changes:
        changes["asset_id"] = _not_blank(changes["asset_id"])
        if changes["asset_id"]:
            _require_asset(session, changes["asset_id"])
    for key in ("uri", "revision", "notes"):
        if key in changes:
            changes[key] = _not_blank(changes[key])
    before = {key: getattr(document, key) for key in changes}
    for key, value in changes.items():
        setattr(document, key, value)
    session.flush()
    _audit(
        session,
        principal,
        "document.update",
        document,
        {"changed": sorted(changes), "before": {k: _plain(v) for k, v in before.items()}},
    )
    session.commit()
    return DocumentOut.model_validate(document)


@router.delete("/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Delete a record")
def delete_document(document_id: str, session: DbSession, principal: MaintainerPrincipal) -> None:
    document = _require_document(session, document_id)
    _audit(
        session, principal, "document.delete", document, {"doc_type": document.doc_type, "uri": document.uri}
    )
    session.delete(document)
    session.commit()


def _plain(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value
