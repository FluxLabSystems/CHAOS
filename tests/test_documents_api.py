"""Document records (the registry's ``documents`` table) through the API.

Until this router existed the table was never written or read: the Assets
screen said "No manuals, drawings or commissioning records are linked yet" and
could never say anything else. These tests pin the contract the screen now
relies on: metadata only, linked to a real asset or to the site, written by a
maintainer and audited against that maintainer.
"""

from __future__ import annotations

import pytest

from chaos.api.routers.documents import DOCUMENT_KINDS
from chaos.models.commands import AuditLogEntry
from chaos.models.registry import Asset, AssetClass, Document

PUMP = "water.pump.well.01"


@pytest.fixture()
def maintainer_headers() -> dict[str, str]:
    return {"X-Operator": "test.maintainer", "X-Operator-Role": "maintainer"}


@pytest.fixture()
def pump(db_session):
    db_session.add(AssetClass(name="pump", allowed_domains=["water"]))
    db_session.add(
        Asset(asset_id=PUMP, domain="water", asset_class="pump", name="Well pump", status="planned")
    )
    db_session.commit()
    return PUMP


def _create(client, headers, **overrides):
    body = {
        "asset_id": PUMP,
        "doc_type": "manual",
        "title": "Grundfos SQE manual",
        "uri": "file://nas/sqe.pdf",
    }
    body.update(overrides)
    return client.post("/api/v1/documents", json=body, headers=headers)


# ------------------------------------------------------------------- reads --


def test_kinds_are_published_for_the_console_to_offer(client):
    assert client.get("/api/v1/documents/kinds").json() == list(DOCUMENT_KINDS)
    assert "commissioning_record" in DOCUMENT_KINDS


def test_an_asset_with_no_records_lists_none_not_an_error(client, pump):
    assert client.get(f"/api/v1/assets/{pump}/documents").json() == []
    page = client.get("/api/v1/documents", params={"asset_id": pump}).json()
    assert page == {"items": [], "total": 0, "limit": 100, "offset": 0}


def test_an_unknown_asset_is_a_404(client):
    assert client.get("/api/v1/assets/water.pump.none.99/documents").status_code == 404


# ------------------------------------------------------------------ writes --


def test_writes_require_a_maintainer(client, pump, operator_headers):
    assert _create(client, {}).status_code == 403
    assert _create(client, operator_headers).status_code == 403


def test_create_list_update_delete_round_trip(client, db_session, pump, maintainer_headers):
    created = _create(client, maintainer_headers, revision="rev B", notes="Binder in the container")
    assert created.status_code == 201, created.text
    record = created.json()
    assert record["asset_id"] == pump
    assert record["doc_type"] == "manual"
    assert record["title"] == "Grundfos SQE manual"
    assert record["uri"] == "file://nas/sqe.pdf"
    assert record["revision"] == "rev B"
    assert record["created_at"] and record["updated_at"]

    listed = client.get(f"/api/v1/assets/{pump}/documents").json()
    assert [d["id"] for d in listed] == [record["id"]]
    assert client.get(f"/api/v1/documents/{record['id']}").json()["title"] == "Grundfos SQE manual"

    page = client.get("/api/v1/documents", params={"doc_type": "manual", "q": "SQE"}).json()
    assert page["total"] == 1 and page["items"][0]["id"] == record["id"]
    assert client.get("/api/v1/documents", params={"doc_type": "drawing"}).json()["total"] == 0

    patched = client.patch(
        f"/api/v1/documents/{record['id']}",
        json={"revision": "rev C", "uri": "  "},
        headers=maintainer_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["revision"] == "rev C"
    assert patched.json()["uri"] is None, "a blank URI is no URI, not an empty string"
    assert patched.json()["title"] == "Grundfos SQE manual", "fields not sent are left alone"

    deleted = client.delete(f"/api/v1/documents/{record['id']}", headers=maintainer_headers)
    assert deleted.status_code == 204
    assert client.get(f"/api/v1/documents/{record['id']}").status_code == 404
    assert db_session.query(Document).count() == 0

    actions = [e.action for e in db_session.query(AuditLogEntry).order_by(AuditLogEntry.occurred_at).all()]
    assert actions == ["document.create", "document.update", "document.delete"]
    update = db_session.query(AuditLogEntry).filter(AuditLogEntry.action == "document.update").one()
    assert update.actor == "test.maintainer"
    assert update.detail["changed"] == ["revision", "uri"]
    assert update.detail["before"]["revision"] == "rev B"


def test_a_record_must_point_at_a_real_asset_or_at_nothing(client, pump, maintainer_headers):
    missing = _create(client, maintainer_headers, asset_id="water.pump.none.99")
    assert missing.status_code == 404
    site_wide = _create(
        client, maintainer_headers, asset_id=None, title="Site one-line diagram", doc_type="drawing"
    )
    assert site_wide.status_code == 201
    assert site_wide.json()["asset_id"] is None
    # Re-pointing a record at a missing asset is refused the same way.
    moved = client.patch(
        f"/api/v1/documents/{site_wide.json()['id']}",
        json={"asset_id": "water.pump.none.99"},
        headers=maintainer_headers,
    )
    assert moved.status_code == 404


def test_the_kind_vocabulary_and_the_title_are_enforced(client, pump, maintainer_headers):
    assert _create(client, maintainer_headers, doc_type="napkin").status_code == 422
    assert _create(client, maintainer_headers, title="   ").status_code == 422
    assert _create(client, maintainer_headers, storage="zfs").status_code == 422, (
        "no file storage field exists"
    )


def test_an_empty_patch_is_refused_rather_than_silently_touching_updated_at(client, pump, maintainer_headers):
    record = _create(client, maintainer_headers).json()
    response = client.patch(f"/api/v1/documents/{record['id']}", json={}, headers=maintainer_headers)
    assert response.status_code == 400


def test_listing_is_paged_and_ordered_by_kind_then_title(client, pump, maintainer_headers):
    for kind, title in (("photo", "Nameplate"), ("drawing", "Wiring"), ("drawing", "Elevation")):
        assert _create(client, maintainer_headers, doc_type=kind, title=title).status_code == 201
    page = client.get("/api/v1/documents", params={"limit": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 2
    assert [(d["doc_type"], d["title"]) for d in page["items"]] == [
        ("drawing", "Elevation"),
        ("drawing", "Wiring"),
    ]
    rest = client.get("/api/v1/documents", params={"limit": 2, "offset": 2}).json()
    assert [d["title"] for d in rest["items"]] == ["Nameplate"]
