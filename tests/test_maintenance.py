"""Maintenance, work-order and commissioning tests (SDD sections 18, 19)."""

from __future__ import annotations

import datetime as dt

import pytest

from chaos.maintenance import commissioning, scheduler
from chaos.models.alarms import Alarm, AlarmDefinition
from chaos.models.maintenance import Inspection, MaintenancePlan, SparePart, WorkOrder
from chaos.models.registry import Asset, AssetClass, Point, PointBinding, PointDefinition
from chaos.models.telemetry import CurrentState

NOW = dt.datetime(2026, 8, 7, 12, 0, tzinfo=dt.UTC)


@pytest.fixture()
def pump(db_session):
    """A minimal commandable asset with a runtime counter point."""
    db_session.add(
        AssetClass(name="pump", allowed_domains=["water"], default_points=[], required_properties=[])
    )
    db_session.add_all(
        [
            PointDefinition(
                name="runtime_total_h",
                default_class="COUNTER",
                allowed_classes=["COUNTER"],
                data_type="number",
                unit="h",
                control_capable=False,
            ),
            PointDefinition(
                name="mode_requested",
                default_class="DO",
                allowed_classes=["DO"],
                data_type="enum",
                control_capable=True,
                enum_values=["off", "manual", "automatic"],
            ),
        ]
    )
    asset = Asset(
        asset_id="water.pump.irrigation.01",
        domain="water",
        asset_class="pump",
        name="Main Orchard Irrigation Pump",
        status="active",
        criticality="important",
        control_authority="local_controller_with_supervisory_setpoints",
    )
    db_session.add(asset)
    db_session.flush()
    point = Point(
        point_id="water.pump.irrigation.01/runtime_total_h",
        asset_id=asset.asset_id,
        point_name="runtime_total_h",
        point_class="COUNTER",
        data_type="number",
        unit="h",
    )
    db_session.add(point)
    db_session.add(
        CurrentState(
            point_id=point.point_id,
            asset_id=asset.asset_id,
            point_name="runtime_total_h",
            value_numeric=0.0,
            quality="good",
            ts=NOW,
        )
    )
    db_session.commit()
    return asset


# --------------------------------------------------------------------------
# Trigger logic
# --------------------------------------------------------------------------


def test_calendar_plan_becomes_due_and_reschedules(db_session, pump):
    plan = MaintenancePlan(
        asset_id=pump.asset_id,
        name="Exercise valve",
        trigger_type="calendar",
        interval_days=30,
        next_due_at=NOW + dt.timedelta(days=1),
    )
    db_session.add(plan)
    db_session.commit()

    assert scheduler.plan_is_due(db_session, plan, NOW) == (False, "not_yet_due")

    later = NOW + dt.timedelta(days=2)
    assert scheduler.plan_is_due(db_session, plan, later)[0] is True

    result = scheduler.generate_work_orders(db_session, later)
    db_session.commit()
    assert result.created_count == 1
    order = result.created[0]
    assert order.asset_id == pump.asset_id
    assert order.priority == "normal"  # criticality "important" -> normal

    # Completing the work advances the schedule by one interval.
    scheduler.complete_work_order(db_session, order, later, "operator.a", note="done")
    db_session.commit()
    assert plan.last_completed_at == later
    assert plan.next_due_at == later + dt.timedelta(days=30)


def test_open_work_is_not_duplicated(db_session, pump):
    plan = MaintenancePlan(
        asset_id=pump.asset_id,
        name="Filter clean",
        trigger_type="calendar",
        interval_days=7,
        next_due_at=NOW - dt.timedelta(days=1),
    )
    db_session.add(plan)
    db_session.commit()

    assert scheduler.generate_work_orders(db_session, NOW).created_count == 1
    db_session.commit()
    second = scheduler.generate_work_orders(db_session, NOW)
    assert second.created_count == 0
    assert ("work_already_open") in [reason for _, reason in second.skipped]


def test_runtime_counter_measures_wear_since_last_service(db_session, pump):
    plan = MaintenancePlan(
        asset_id=pump.asset_id,
        name="Pump service",
        trigger_type="runtime_hours",
        interval_runtime_h=500,
        condition_point="water.pump.irrigation.01/runtime_total_h",
    )
    db_session.add(plan)
    state = db_session.get(CurrentState, "water.pump.irrigation.01/runtime_total_h")
    state.value_numeric = 400.0
    db_session.commit()
    assert scheduler.plan_is_due(db_session, plan, NOW)[0] is False

    state.value_numeric = 520.0
    db_session.commit()
    assert scheduler.plan_is_due(db_session, plan, NOW)[0] is True

    order = scheduler.generate_work_orders(db_session, NOW).created[0]
    scheduler.complete_work_order(db_session, order, NOW, "tech.b")
    db_session.commit()

    # Baseline advanced: 520 h is now "zero hours since service".
    assert plan.counter_baseline == 520.0
    assert scheduler.plan_is_due(db_session, plan, NOW)[0] is False
    state.value_numeric = 1021.0
    db_session.commit()
    assert scheduler.plan_is_due(db_session, plan, NOW)[0] is True


def test_stale_counter_does_not_read_as_no_wear(db_session, pump):
    """A frozen sensor must not silently defer maintenance forever."""
    plan = MaintenancePlan(
        asset_id=pump.asset_id,
        name="Pump service",
        trigger_type="runtime_hours",
        interval_runtime_h=100,
        condition_point="water.pump.irrigation.01/runtime_total_h",
    )
    db_session.add(plan)
    state = db_session.get(CurrentState, "water.pump.irrigation.01/runtime_total_h")
    state.value_numeric = 5000.0
    state.quality = "stale"
    db_session.commit()

    due, reason = scheduler.plan_is_due(db_session, plan, NOW)
    assert due is False
    assert reason == "counter_quality:stale"


def test_condition_plan_respects_operator(db_session, pump):
    plan = MaintenancePlan(
        asset_id=pump.asset_id,
        name="Clean filter on differential pressure",
        trigger_type="condition",
        condition_point="water.pump.irrigation.01/runtime_total_h",
        condition_operator="ge",
        condition_value=50.0,
    )
    db_session.add(plan)
    state = db_session.get(CurrentState, "water.pump.irrigation.01/runtime_total_h")
    state.value_numeric = 49.0
    db_session.commit()
    assert scheduler.plan_is_due(db_session, plan, NOW) == (False, "condition_not_met")

    state.value_numeric = 51.0
    db_session.commit()
    assert scheduler.plan_is_due(db_session, plan, NOW) == (True, "condition_met")


def test_seasonal_plan_fires_once_per_season(db_session, pump):
    plan = MaintenancePlan(
        asset_id=pump.asset_id, name="Winterize outdoor lines", trigger_type="seasonal", season="winter"
    )
    db_session.add(plan)
    db_session.commit()

    august = dt.datetime(2026, 8, 7, tzinfo=dt.UTC)
    january = dt.datetime(2026, 1, 10, tzinfo=dt.UTC)
    assert scheduler.plan_is_due(db_session, plan, august) == (False, "out_of_season")
    assert scheduler.plan_is_due(db_session, plan, january)[0] is True

    plan.last_completed_at = dt.datetime(2026, 1, 5, tzinfo=dt.UTC)
    db_session.commit()
    assert scheduler.plan_is_due(db_session, plan, january) == (False, "already_done_this_season")


# --------------------------------------------------------------------------
# Event-driven work
# --------------------------------------------------------------------------


def test_alarm_raises_corrective_work_once(db_session, pump):
    db_session.add(
        AlarmDefinition(alarm_key="pump.overcurrent", name="Pump overcurrent", severity="critical")
    )
    db_session.flush()
    alarm = Alarm(
        alarm_key="pump.overcurrent",
        asset_id=pump.asset_id,
        severity="critical",
        state="active",
        detected_at=NOW,
        message="Overcurrent trip",
    )
    db_session.add(alarm)
    db_session.flush()

    order = scheduler.raise_for_alarm(db_session, alarm, NOW)
    db_session.commit()
    assert order is not None
    assert order.priority == "urgent"
    assert scheduler.raise_for_alarm(db_session, alarm, NOW) is None


def _alarm_definition(session, key="pump.overcurrent", severity="critical"):
    if session.get(AlarmDefinition, key) is None:
        session.add(AlarmDefinition(alarm_key=key, name=key, severity=severity))
        session.flush()


def _alarm(session, pump, *, key="pump.overcurrent", severity="critical", state="active", **kwargs):
    _alarm_definition(session, key, severity)
    alarm = Alarm(
        alarm_key=key,
        asset_id=pump.asset_id,
        severity=severity,
        state=state,
        detected_at=NOW,
        activated_at=NOW if state != "detected" else None,
        message=f"{key} fired",
        **kwargs,
    )
    session.add(alarm)
    session.flush()
    return alarm


def _alarm_plan(session, pump, *, name="Inspect pump after trip", alarm_key=None, enabled=True):
    plan = MaintenancePlan(
        asset_id=pump.asset_id,
        name=name,
        trigger_type="alarm",
        condition_point=alarm_key,
        procedure="Check the contactor and the motor windings before restarting.",
        enabled=enabled,
    )
    session.add(plan)
    session.flush()
    return plan


def test_alarm_plan_raises_work_once_per_occurrence(db_session, pump):
    plan = _alarm_plan(db_session, pump)
    alarm = _alarm(db_session, pump)

    result = scheduler.raise_for_alarm_plans(db_session, NOW)
    db_session.commit()
    assert result.created_count == 1
    order = result.created[0]
    assert (order.plan_id, order.alarm_id, order.asset_id) == (plan.id, alarm.id, pump.asset_id)
    assert order.priority == "urgent"  # critical alarm beats the asset's "important"
    assert "Check the contactor" in order.description
    assert "pump.overcurrent" in order.description

    # The same occurrence on the next cycle raises nothing more ...
    assert scheduler.raise_for_alarm_plans(db_session, NOW + dt.timedelta(minutes=10)).created_count == 0
    # ... and neither does it once the work has been completed.
    scheduler.complete_work_order(db_session, order, NOW + dt.timedelta(hours=1), "tech.a", note="done")
    db_session.commit()
    assert scheduler.raise_for_alarm_plans(db_session, NOW + dt.timedelta(hours=2)).created_count == 0

    # A new occurrence is a new job.
    _alarm(db_session, pump)
    assert scheduler.raise_for_alarm_plans(db_session, NOW + dt.timedelta(hours=3)).created_count == 1


def test_alarm_plan_can_be_bound_to_one_alarm_key(db_session, pump):
    _alarm_plan(db_session, pump, alarm_key="pump.seal_leak")
    _alarm(db_session, pump, key="pump.overcurrent")
    assert scheduler.raise_for_alarm_plans(db_session, NOW).created_count == 0
    _alarm(db_session, pump, key="pump.seal_leak", severity="warning")
    result = scheduler.raise_for_alarm_plans(db_session, NOW)
    assert result.created_count == 1
    assert result.created[0].priority == "normal"  # warning alarm, "important" asset


def test_alarm_plan_ignores_candidates_suppressed_alarms_and_disabled_plans(db_session, pump):
    plan = _alarm_plan(db_session, pump)
    _alarm(db_session, pump, state="detected")  # still inside its on-delay
    _alarm(db_session, pump, suppressed=True, suppression_reason="symptom: of an incident")
    _alarm(db_session, pump, state="cleared")
    result = scheduler.raise_for_alarm_plans(db_session, NOW)
    assert result.created_count == 0
    assert (plan.id, "no_new_alarm_occurrence") in result.skipped

    plan.enabled = False
    _alarm(db_session, pump)
    result = scheduler.raise_for_alarm_plans(db_session, NOW)
    assert result.created_count == 0
    assert (plan.id, "plan_disabled") in result.skipped


def test_generate_work_orders_covers_alarm_plans_too(db_session, pump):
    """The periodic scheduler and the maintainer's generate action catch up on alarm work."""
    plan = _alarm_plan(db_session, pump)
    _alarm(db_session, pump)
    result = scheduler.generate_work_orders(db_session, NOW)
    db_session.commit()
    assert [order.plan_id for order in result.created] == [plan.id]
    # And it is still once per occurrence across both paths.
    assert scheduler.raise_for_alarm_plans(db_session, NOW).created_count == 0
    assert scheduler.generate_work_orders(db_session, NOW).created_count == 0


def test_failed_inspection_generates_follow_up(db_session, pump):
    passing = Inspection(asset_id=pump.asset_id, inspected_at=NOW, result="pass")
    failing = Inspection(asset_id=pump.asset_id, inspected_at=NOW, result="fail", findings="Seal weeping")
    db_session.add_all([passing, failing])
    db_session.flush()

    assert scheduler.raise_for_inspection(db_session, passing, NOW) is None
    follow_up = scheduler.raise_for_inspection(db_session, failing, NOW)
    assert follow_up is not None
    assert follow_up.priority == "high"


def test_completion_consumes_spares(db_session, pump):
    db_session.add(
        SparePart(
            part_number="FLT-100", description="Filter cartridge", quantity_on_hand=3, minimum_quantity=2
        )
    )
    order = WorkOrder(
        asset_id=pump.asset_id, title="Replace filter", state="open", opened_at=NOW, parts_used=[]
    )
    db_session.add(order)
    db_session.commit()

    scheduler.complete_work_order(
        db_session, order, NOW, "tech.b", parts_used=[{"part_number": "FLT-100", "quantity": 2}]
    )
    db_session.commit()

    part = db_session.query(SparePart).filter_by(part_number="FLT-100").one()
    assert part.quantity_on_hand == 1
    assert [p.part_number for p in scheduler.parts_below_minimum(db_session)] == ["FLT-100"]


# --------------------------------------------------------------------------
# Commissioning (SDD section 19)
# --------------------------------------------------------------------------


def test_commissioning_gate_blocks_automatic_control_until_tested(db_session, pump):
    point = Point(
        point_id="water.pump.irrigation.01/mode_requested",
        asset_id=pump.asset_id,
        point_name="mode_requested",
        point_class="DO",
        data_type="enum",
        control_capable=True,
    )
    db_session.add(point)
    db_session.flush()
    db_session.add(
        PointBinding(
            point_id=point.point_id,
            asset_id=pump.asset_id,
            point_name="mode_requested",
            binding_status="tbd",
            automatic_control_allowed=False,
        )
    )
    db_session.commit()

    permitted, reason = commissioning.may_enable_automatic_control(db_session, pump.asset_id)
    assert permitted is False
    assert "not passed" in reason

    with pytest.raises(PermissionError):
        commissioning.commission_binding(db_session, point.point_id, "tech.b", NOW, allow_automatic=True)

    # Manual (non-automatic) commissioning is still allowed.
    binding = commissioning.commission_binding(
        db_session, point.point_id, "tech.b", NOW, allow_automatic=False
    )
    assert binding.binding_status == "commissioned"
    assert binding.automatic_control_allowed is False

    # Pass every prerequisite plus the supervisory test.
    for step in (*commissioning.PREREQUISITE_STEPS, 9):
        commissioning.record_step(db_session, pump.asset_id, step, "pass", "tech.b", NOW)
    db_session.commit()

    permitted, _ = commissioning.may_enable_automatic_control(db_session, pump.asset_id)
    assert permitted is True
    binding = commissioning.commission_binding(
        db_session, point.point_id, "tech.b", NOW, allow_automatic=True
    )
    assert binding.automatic_control_allowed is True


def test_failed_step_blocks_commissioning(db_session, pump):
    for step in commissioning.PREREQUISITE_STEPS:
        commissioning.record_step(db_session, pump.asset_id, step, "pass", "tech.b", NOW)
    commissioning.record_step(db_session, pump.asset_id, 6, "fail", "tech.b", NOW)
    db_session.commit()

    permitted, reason = commissioning.may_enable_automatic_control(db_session, pump.asset_id)
    assert permitted is False
    assert "failed" in reason


def test_commissioning_records_are_idempotent(db_session, pump):
    commissioning.ensure_records(db_session, pump.asset_id)
    commissioning.ensure_records(db_session, pump.asset_id)
    db_session.commit()
    status = commissioning.commissioning_status(db_session, pump.asset_id)
    assert status["steps_total"] == 12
    assert status["steps_passed"] == 0
    assert status["fully_commissioned"] is False


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------


def test_work_order_api_round_trip(client, db_session, pump, operator_headers):
    response = client.post(
        "/api/v1/work-orders",
        json={"asset_id": pump.asset_id, "title": "Inspect pump", "priority": "high"},
        headers=operator_headers,
    )
    assert response.status_code == 201, response.text
    order_id = response.json()["id"]
    assert response.json()["asset_name"] == "Main Orchard Irrigation Pump"

    assert client.get("/api/v1/work-orders").status_code == 200
    completed = client.post(
        f"/api/v1/work-orders/{order_id}/complete",
        json={"note": "No issues found"},
        headers=operator_headers,
    )
    assert completed.status_code == 200
    assert completed.json()["state"] == "completed"

    # Completing twice is a conflict, not a silent no-op.
    again = client.post(f"/api/v1/work-orders/{order_id}/complete", json={}, headers=operator_headers)
    assert again.status_code == 409


def test_work_order_write_requires_operator_role(client, pump):
    response = client.post("/api/v1/work-orders", json={"title": "Unauthorised"})
    assert response.status_code == 403


def test_commissioning_api(client, pump, admin_headers):
    assert len(client.get("/api/v1/commissioning/steps").json()) == 12

    status = client.get(f"/api/v1/commissioning/{pump.asset_id}/status")
    assert status.status_code == 200
    assert status.json()["supervisory_control_permitted"] is False

    recorded = client.post(
        f"/api/v1/commissioning/{pump.asset_id}/steps",
        json={"step": 1, "result": "pass", "observed": "Bench test nominal"},
        headers=admin_headers,
    )
    assert recorded.status_code == 200
    assert recorded.json()["status"]["steps_passed"] == 1

    records = client.get(f"/api/v1/commissioning/{pump.asset_id}/records").json()
    assert len(records) == 12
    assert records[0]["observed"] == "Bench test nominal"


def test_maintenance_due_endpoint(client, db_session, pump, admin_headers):
    db_session.add(
        MaintenancePlan(
            asset_id=pump.asset_id,
            name="Monthly inspection",
            trigger_type="calendar",
            interval_days=30,
            next_due_at=dt.datetime(2020, 1, 1, tzinfo=dt.UTC),
        )
    )
    db_session.commit()

    due = client.get("/api/v1/maintenance/due").json()
    assert len(due["due_now"]) == 1

    generated = client.post("/api/v1/maintenance/generate", headers=admin_headers)
    assert generated.status_code == 200
    assert generated.json()["created_count"] == 1


# --------------------------------------------------------------------------
# The alarm engine raises alarm-triggered work (SDD 18 "alarm occurrence")
# --------------------------------------------------------------------------


def test_alarm_engine_cycle_raises_work_for_alarm_plans(session_factory, bus, settings, db_session):
    """Activation -> correlation -> work order, inside one engine cycle, once."""
    from test_alarm_correlation import AC_MAIN, SERVER, UPS, at, load_real_registry, set_state

    from chaos.alarms.service import AlarmEngineService

    load_real_registry(db_session)
    db_session.add(
        MaintenancePlan(
            asset_id=AC_MAIN,
            name="Inspect the container AC feed after a bus loss",
            trigger_type="alarm",
            procedure="Check the transfer switch and the feed breaker before re-energising.",
        )
    )
    # A plan on a symptom asset: the server's alarm is a member of the outage
    # incident, suppressed as a symptom, and must raise nothing.
    db_session.add(MaintenancePlan(asset_id=SERVER, name="Server post-outage check", trigger_type="alarm"))
    db_session.commit()

    service = AlarmEngineService(session_factory, bus, settings, interval_s=3600)
    service.load_definitions()
    set_state(db_session, AC_MAIN, "energized_state", False)
    set_state(db_session, UPS, "on_battery", True)
    set_state(db_session, SERVER, "availability_state", "offline")
    db_session.commit()

    first = service.evaluate_once(at(0))
    assert first.work_orders == 0  # candidates only, nothing has activated

    second = service.evaluate_once(at(600))
    assert second.evaluation.activated
    assert second.work_orders == 1
    assert second.as_dict()["work_orders"] == 1

    fresh = session_factory()
    try:
        orders = fresh.query(WorkOrder).all()
        assert len(orders) == 1
        order = orders[0]
        assert order.asset_id == AC_MAIN
        assert order.alarm_id is not None
        root = fresh.get(Alarm, order.alarm_id)
        assert root.alarm_key == "power_container_ac_bus_lost"
        assert root.suppressed is False
    finally:
        fresh.close()

    # The same occurrence on the next cycle raises nothing more.
    assert service.evaluate_once(at(1200)).work_orders == 0


def test_secondary_node_never_raises_work(session_factory, bus, settings, db_session):
    from test_alarm_correlation import AC_MAIN, UPS, at, load_real_registry, set_state

    from chaos.alarms.service import AlarmEngineService

    load_real_registry(db_session)
    db_session.add(MaintenancePlan(asset_id=AC_MAIN, name="AC feed inspection", trigger_type="alarm"))
    db_session.commit()

    secondary = settings.model_copy(update={"node_role": "secondary"})
    service = AlarmEngineService(session_factory, bus, secondary, interval_s=3600)
    service.load_definitions()
    set_state(db_session, AC_MAIN, "energized_state", False)
    set_state(db_session, UPS, "on_battery", True)
    db_session.commit()

    service.evaluate_once(at(0))
    result = service.evaluate_once(at(600))
    assert result.evaluation.activated
    assert result.work_orders == 0
    fresh = session_factory()
    try:
        assert fresh.query(WorkOrder).count() == 0
    finally:
        fresh.close()
