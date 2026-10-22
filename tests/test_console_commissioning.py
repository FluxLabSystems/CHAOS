"""The Commissioning screen (SDD 19).

docs/commissioning.md said "There is no dedicated commissioning form in the
console today"; this view is that form. The console cannot be rendered here, so
the view is read statically in the manner of ``test_topology.py``, and the
contract it depends on -- the twelve steps, the four outcomes, the refusal the
binding gate returns -- is checked against the API and the commissioning module
so the form can never offer a step or a result the platform rejects.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from chaos.maintenance.commissioning import COMMISSIONING_STEPS
from chaos.models.registry import Asset, AssetClass, Point, PointBinding, PointDefinition

UI = Path("src/chaos/web")
PUMP = "water.pump.well.01"


def _view() -> str:
    return (UI / "views/commissioning.js").read_text(encoding="utf-8")


# -------------------------------------------------------------------- ui --


def test_view_exists_is_self_contained_and_uses_the_one_request_layer():
    text = _view()
    assert not re.search(r"https?://(?!www\.w3\.org)", text)
    for forbidden in ("fetch(", "XMLHttpRequest", "import(", "eval("):
        assert forbidden not in text, forbidden
    assert "from '../api.js'" in text
    assert "resultProblem" in text


def test_the_view_is_registered_and_deep_linkable():
    app = (UI / "app.js").read_text(encoding="utf-8")
    index = (UI / "index.html").read_text(encoding="utf-8")
    assert "view: 'commissioning'" in app
    assert "/commissioning\\/(.+)$/" in app and "param: 'assetId'" in app
    assert 'data-nav="commissioning"' in index
    assert 'id="i-commissioning"' in index


def test_the_form_offers_exactly_the_apis_outcomes_and_reads_the_steps_from_it():
    text = _view()
    match = re.search(r"const RESULTS = \[(.*?)\];", text)
    assert match
    assert re.findall(r"'([a-z_]+)'", match.group(1)) == ["pass", "fail", "blocked", "not_run"]
    # Steps are not hard-coded: the select is built from GET /commissioning/steps.
    assert "api.commissioningSteps()" in text
    assert "step.step_name" in text
    for name in (name for _, name in COMMISSIONING_STEPS):
        assert name not in text, f"{name} is hard-coded; the API is the source of the step list"


def test_blocked_and_not_run_are_never_drawn_as_progress():
    text = _view()
    block = text[text.index("const RESULT_TONE = {") : text.index("};", text.index("const RESULT_TONE = {"))]
    assert "pass: 'ok'" in block
    assert "fail: 'alarm'" in block
    assert "blocked: 'degraded'" in block
    assert "not_run: 'no_data'" in block


def test_recording_a_step_is_maintainer_gated_and_sends_the_test_record_fields():
    text = _view()
    api = (UI / "api.js").read_text(encoding="utf-8")
    form = text[text.index("function stepForm(") : text.index("async function bindingsCard(")]
    gate = form.index("requireRole('maintainer')")
    call = form.index("api.recordCommissioningStep(", gate)
    assert gate < call
    for field in ("preconditions", "injected_condition", "expected_sequence", "observed", "evidence"):
        assert field in form, field
    assert "post(`/commissioning/${encodeURI(assetId)}/steps`" in api
    # A pass or fail with nothing observed is refused before it is sent.
    assert "a pass with no observation is not a record" in form


def test_the_binding_action_is_gated_asks_why_and_shows_the_gates_refusal():
    text = _view()
    api = (UI / "api.js").read_text(encoding="utf-8")
    block = text[text.index("async function bindingsCard(") : text.index("async function drawAsset(")]
    gate = block.index("requireRole('maintainer')")
    reason = block.index("askReason(", gate)
    call = block.index("api.commissionBinding(", reason)
    assert gate < reason < call
    assert "allow_automatic_control" in block
    assert "response.status === 409" in block and "Refused by the gate" in block
    assert "post(`/commissioning/bindings/${encodeURI(pointId)}`" in api
    # The binding comes from the point detail route, one request per point.
    assert "api.point(asset.asset_id, point.point_name)" in block


def test_status_is_drawn_from_the_api_counts_and_names_its_source():
    text = _view()
    for field in (
        "steps_passed",
        "steps_total",
        "prerequisites_met",
        "supervisory_control_permitted",
        "fully_commissioned",
        "steps_failed",
        "outstanding",
    ):
        assert field in text, field
    assert "GET /commissioning/{asset_id}/status" in text


# ------------------------------------------------------------------- api --


@pytest.fixture()
def pump(db_session):
    db_session.add(AssetClass(name="pump", allowed_domains=["water"]))
    db_session.add(
        PointDefinition(
            name="mode_requested",
            default_class="DO",
            allowed_classes=["DO"],
            data_type="enum",
            control_capable=True,
            enum_values=["off", "automatic"],
        )
    )
    db_session.add(
        Asset(asset_id=PUMP, domain="water", asset_class="pump", name="Well pump", status="installed")
    )
    db_session.flush()
    point_id = f"{PUMP}/mode_requested"
    db_session.add(
        Point(
            point_id=point_id,
            asset_id=PUMP,
            point_name="mode_requested",
            point_class="DO",
            data_type="enum",
            control_capable=True,
        )
    )
    db_session.add(PointBinding(point_id=point_id, asset_id=PUMP, point_name="mode_requested"))
    db_session.commit()
    return point_id


@pytest.fixture()
def maintainer_headers() -> dict[str, str]:
    return {"X-Operator": "test.maintainer", "X-Operator-Role": "maintainer"}


def test_what_the_screen_reads_and_writes(client, pump, maintainer_headers, operator_headers):
    """The exact calls the view makes, in the order it makes them."""
    steps = client.get("/api/v1/commissioning/steps").json()
    assert [(s["step"], s["step_name"]) for s in steps] == list(COMMISSIONING_STEPS)

    status = client.get(f"/api/v1/commissioning/{PUMP}/status").json()
    assert status["steps_passed"] == 0 and status["supervisory_control_permitted"] is False
    assert client.get(f"/api/v1/commissioning/{PUMP}/records").json() == []

    # The binding row: point detail carries the binding the view draws.
    detail = client.get(f"/api/v1/points/{PUMP}/mode_requested").json()
    assert detail["binding"]["binding_status"] == "tbd"

    # Recording is a maintainer write; an operator is refused.
    body = {"step": 1, "result": "blocked", "observed": "No bench power available"}
    assert (
        client.post(f"/api/v1/commissioning/{PUMP}/steps", json=body, headers=operator_headers).status_code
        == 403
    )
    recorded = client.post(f"/api/v1/commissioning/{PUMP}/steps", json=body, headers=maintainer_headers)
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["status"]["steps_passed"] == 0, "blocked is not progress"
    records = client.get(f"/api/v1/commissioning/{PUMP}/records").json()
    assert len(records) == 12 and records[0]["result"] == "blocked"

    # Commissioning the binding without automatic control is allowed; with it,
    # the gate refuses and the view shows the reason.
    refused = client.post(
        f"/api/v1/commissioning/bindings/{pump}",
        json={"allow_automatic_control": True, "reason": "trying the gate"},
        headers=maintainer_headers,
    )
    assert refused.status_code == 409
    assert "commissioning steps not passed" in refused.json()["detail"]
    allowed = client.post(
        f"/api/v1/commissioning/bindings/{pump}",
        json={"allow_automatic_control": False, "reason": "address verified at the panel"},
        headers=maintainer_headers,
    )
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["binding_status"] == "commissioned"
    assert allowed.json()["automatic_control_allowed"] is False
    assert allowed.json()["commissioned_by"] == "test.maintainer"
