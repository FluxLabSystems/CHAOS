"""The Maintenance screen (SDD 18).

The maintenance router has carried plans, work orders, inspections,
calibrations and spare parts since it landed, and the console never showed
any of it. These checks read ``views/maintenance.js`` statically, in the manner
of ``test_topology.py``, and pin one property above the rest: every write is
gated on the role the router enforces -- operator for work orders and
inspections, maintainer for generating work, calibrations and spare parts --
rather than on the role table the documentation used to print.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path("src/chaos/web")


def _view() -> str:
    return (UI / "views/maintenance.js").read_text(encoding="utf-8")


def _api() -> str:
    return (UI / "api.js").read_text(encoding="utf-8")


def _block(text: str, start: str, end: str) -> str:
    return text[text.index(start) : text.index(end, text.index(start))]


def test_view_exists_is_self_contained_and_uses_the_one_request_layer():
    text = _view()
    assert not re.search(r"https?://(?!www\.w3\.org)", text)
    for forbidden in ("fetch(", "XMLHttpRequest", "import(", "eval("):
        assert forbidden not in text, forbidden
    assert "from '../api.js'" in text
    assert "resultProblem" in text


def test_the_view_is_registered_and_in_the_navigation():
    app = (UI / "app.js").read_text(encoding="utf-8")
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert "view: 'maintenance'" in app
    assert 'data-nav="maintenance"' in index
    assert 'id="i-maintenance"' in index


def test_every_list_the_router_serves_is_read_and_named():
    text = _view()
    api = _api()
    for call, path in (
        ("api.maintenanceDue(", "/maintenance/due"),
        ("api.maintenancePlans(", "/maintenance/plans"),
        ("api.workOrders(", "/work-orders"),
        ("api.inspections(", "/maintenance/inspections"),
        ("api.calibrations(", "/maintenance/calibrations"),
        ("api.spareParts(", "/maintenance/spare-parts"),
    ):
        assert call in text, call
        assert path in api, path
        assert f"GET {path}" in text, f"the list must name its source {path}"


def test_writes_are_gated_on_the_routers_roles():
    text = _view()
    # Operator writes (routers/maintenance.py uses OperatorPrincipal).
    for start, end, call in (
        ("function completeForm(", "function workOrdersCard(", "api.completeWorkOrder("),
        ("// Raise a work order (operator).", "return card({ title: 'Work orders'", "api.createWorkOrder("),
        ("function inspectionsCard(", "function calibrationsCard(", "api.recordInspection("),
    ):
        block = _block(text, start, end)
        assert "requireRole('operator')" in block, call
        assert "requireRole('maintainer')" not in block, call
        assert block.index("requireRole('operator')") < block.index(call), call
    # Maintainer writes (MaintainerPrincipal).
    for start, end, call in (
        ("function dueCard(", "function plansCard(", "api.generateWorkOrders("),
        ("function calibrationsCard(", "function sparePartsCard(", "api.recordCalibration("),
        (
            "function sparePartsCard(",
            "/* ---------------------------------------------------------------- page",
            "api.createSparePart(",
        ),
    ):
        block = _block(text, start, end)
        assert "requireRole('maintainer')" in block, call
        assert block.index("requireRole('maintainer')") < block.index(call), call


def test_the_two_writes_without_a_reason_are_confirmed_first():
    text = _view()
    due = _block(text, "function dueCard(", "function plansCard(")
    assert due.index("confirmAction(") < due.index("api.generateWorkOrders(")
    complete = _block(text, "function completeForm(", "function workOrdersCard(")
    assert complete.index("confirmAction(") < complete.index("api.completeWorkOrder(")


def test_request_bodies_mirror_the_router_models():
    text = _view()
    orders = _block(text, "// Raise a work order (operator).", "return card({ title: 'Work orders'")
    for field in ("asset_id", "title", "description", "priority", "due_at", "assigned_to"):
        assert field in orders, field
    complete = _block(text, "function completeForm(", "function workOrdersCard(")
    assert "parts_used" in complete and "note" in complete
    inspections = _block(text, "function inspectionsCard(", "function calibrationsCard(")
    for field in ("asset_id", "result", "findings", "measurements", "work_order_id"):
        assert field in inspections, field
    assert "'pass', 'fail', 'advisory'" in inspections
    calibrations = _block(text, "function calibrationsCard(", "function sparePartsCard(")
    for field in (
        "point_id",
        "method",
        "reference_standard",
        "pre_error",
        "post_error",
        "next_due_at",
        "valid",
    ):
        assert field in calibrations, field
    spares = _block(
        text,
        "function sparePartsCard(",
        "/* ---------------------------------------------------------------- page",
    )
    for field in (
        "part_number",
        "description",
        "quantity_on_hand",
        "minimum_quantity",
        "location",
        "linked_assets",
        "supplier",
        "unit_cost",
    ):
        assert field in spares, field
    assert "'urgent', 'high', 'normal', 'low'" in orders


def test_empty_lists_are_drawn_as_empty_not_as_healthy():
    text = _view()
    assert "'No plans'" in text
    assert "'No inspections recorded'" in text
    assert "'No calibrations recorded'" in text
    assert "'No spare parts recorded'" in text
    assert "statusChip('ok', FILTERS.workState === 'open' ? 'No open work orders' : 'No work orders')" in text


def test_the_screen_does_not_poll_under_a_half_filled_form():
    text = _view()
    assert "async refresh() {}" in text
