"""Alarms raised by publication: the EMS asserting on the alarm topic (SDD 37.1).

The EMS publishes ``load_shed_failed`` and its kin on the alarm topic and owns
no alarm row. These tests pin what happens when such a publication reaches the
alarm subsystem: it becomes a real, active alarm under the definition's
contract, re-assertion is cheap, clearing is explicit, maintenance mode still
applies, and the evaluator never closes it for want of a point.

The tests run against the shipped definition set, as the evaluator tests do.
"""

from __future__ import annotations

import pytest
from test_alarm_evaluator import GENERATOR, T0, at, seed_asset, set_mode

from chaos.alarms.definitions import sync_definitions
from chaos.alarms.evaluator import PUBLISHED_BY, AlarmEvaluator
from chaos.alarms.notify import Notifier
from chaos.alarms.published import apply_published_alarm, is_published
from chaos.envelope import EventEnvelope
from chaos.models.alarms import Alarm, NotificationLog

LOAD = "energy.load.site.server_rack_01"
SITE = "site.site.primary.01"


@pytest.fixture()
def definitions(db_session):
    """The shipped definition set, loaded and cross-referenced."""
    return sync_definitions(db_session, strict=True)


@pytest.fixture()
def registry(db_session):
    seed_asset(db_session, LOAD, asset_class="load", criticality="critical")
    seed_asset(db_session, GENERATOR, asset_class="generator")
    seed_asset(db_session, SITE, asset_class="site")
    db_session.flush()


def published(asset_id=LOAD, key="load_shed_failed", severity="warning", source="ems", **detail):
    return EventEnvelope(
        ts=T0, asset_id=asset_id, event=key, detail={"severity": severity, **detail}, source=source
    )


def test_a_defined_key_raises_an_active_alarm_under_the_definitions_contract(
    db_session, settings, definitions, registry, bus
):
    outcome = apply_published_alarm(
        db_session, published(stage="dispatch", detail="rejected"), settings=settings, bus=bus, now=at(0)
    )
    assert outcome.action == "raised"
    alarm = db_session.get(Alarm, outcome.alarm_id)
    assert alarm.state == "active"
    assert alarm.activated_at == at(0)
    assert alarm.asset_id == LOAD
    # The definition's severity is the operator's contract; the claim is recorded.
    assert alarm.severity == "major"
    assert alarm.trigger_value["claimed_severity"] == "warning"
    assert alarm.trigger_value[PUBLISHED_BY] == "ems"
    assert alarm.trigger_value["detail"] == {"stage": "dispatch", "detail": "rejected"}
    assert alarm.point_id is None
    assert is_published(alarm)
    assert "published by ems" in alarm.message
    assert [event.to_state for event in alarm.events] == ["detected", "active"]
    assert {event.actor for event in alarm.events} == {"publisher:ems"}
    # The state change went out on the alarm topic as the store's own echo.
    echo = bus.last("chaos/energy/site/load_server_rack_01/alarm/load_shed_failed")
    assert echo is not None and b'"source":"alarm-engine"' in echo.payload


def test_without_a_definition_nothing_is_raised(db_session, settings, definitions, registry):
    outcome = apply_published_alarm(
        db_session, published(key="load_shed_requested", group="S3"), settings=settings, now=at(0)
    )
    assert outcome.action == "ignored"
    assert "no alarm definition" in outcome.reason
    assert db_session.query(Alarm).count() == 0


def test_a_disabled_definition_is_ignored(db_session, settings, definitions, registry):
    from chaos.models.alarms import AlarmDefinition

    db_session.get(AlarmDefinition, "load_restore_failed").enabled = False
    db_session.flush()
    outcome = apply_published_alarm(
        db_session, published(key="load_restore_failed"), settings=settings, now=at(0)
    )
    assert outcome.action == "ignored"
    assert "disabled" in outcome.reason


def test_reassertion_updates_the_row_without_new_events(db_session, settings, definitions, registry):
    first = apply_published_alarm(db_session, published(stage="dispatch"), settings=settings, now=at(0))
    second = apply_published_alarm(
        db_session, published(stage="confirmation", measured_kw=1.2), settings=settings, now=at(5)
    )
    assert second.action == "reasserted"
    assert second.alarm_id == first.alarm_id
    alarm = db_session.get(Alarm, first.alarm_id)
    assert alarm.trigger_value["assertions"] == 2
    assert alarm.trigger_value["detail"] == {"stage": "confirmation", "measured_kw": 1.2}
    assert len(alarm.events) == 2  # detected, active -- nothing appended by the re-assertion
    assert db_session.query(Alarm).count() == 1


def test_the_publisher_can_clear_its_own_alarm(db_session, settings, definitions, registry):
    raised = apply_published_alarm(db_session, published(stage="dispatch"), settings=settings, now=at(0))
    cleared = apply_published_alarm(
        db_session, published(state="cleared", note="load confirmed off"), settings=settings, now=at(60)
    )
    assert cleared.action == "cleared" and cleared.alarm_id == raised.alarm_id
    alarm = db_session.get(Alarm, raised.alarm_id)
    assert alarm.state == "cleared"
    assert alarm.cleared_at == at(60)
    assert alarm.events[-1].actor == "publisher:ems"
    assert "load confirmed off" in alarm.events[-1].note

    again = apply_published_alarm(db_session, published(state="cleared"), settings=settings, now=at(61))
    assert again.action == "ignored" and "to clear" in again.reason

    # A fresh assertion after the clear is a new occurrence, not a reopening.
    fresh = apply_published_alarm(db_session, published(stage="dispatch"), settings=settings, now=at(90))
    assert fresh.action == "raised" and fresh.alarm_id != raised.alarm_id


def test_maintenance_mode_suppresses_but_still_records(db_session, settings, definitions, registry):
    set_mode(db_session, "asset", LOAD, "maintenance")
    outcome = apply_published_alarm(db_session, published(), settings=settings, now=at(0))
    assert outcome.action == "raised"
    alarm = db_session.get(Alarm, outcome.alarm_id)
    assert alarm.state == "active"
    assert alarm.suppressed is True
    assert alarm.suppression_reason.startswith("maintenance")


def test_declared_suppression_conditions_apply(db_session, settings, definitions, registry):
    # load_shed_failed declares manual mode on the asset as a suppression condition.
    set_mode(db_session, "asset", LOAD, "manual")
    outcome = apply_published_alarm(db_session, published(), settings=settings, now=at(0))
    alarm = db_session.get(Alarm, outcome.alarm_id)
    assert alarm.suppressed is True
    assert alarm.suppression_reason.startswith("condition")


def test_the_evaluator_never_closes_a_published_quality_alarm(
    db_session, settings, definitions, registry, bus
):
    """energy_meter_data_invalid is reconciled against the current-state cache; a published one has no point."""
    outcome = apply_published_alarm(
        db_session,
        published(
            asset_id=SITE, key="energy_meter_data_invalid", severity="major", stage="shed_precondition"
        ),
        settings=settings,
        now=at(0),
    )
    assert outcome.action == "raised"
    AlarmEvaluator(db_session, bus, settings).evaluate(at(10))
    alarm = db_session.get(Alarm, outcome.alarm_id)
    assert alarm.state == "active"


def test_an_operator_can_clear_a_published_alarm_without_force(
    db_session, settings, definitions, registry, bus
):
    outcome = apply_published_alarm(db_session, published(), settings=settings, now=at(0))
    evaluator = AlarmEvaluator(db_session, bus, settings)
    alarm = evaluator.clear(outcome.alarm_id, actor="operator.a", note="load confirmed off", now=at(30))
    assert alarm.state == "cleared"


def test_a_published_alarm_is_notified_like_any_other(db_session, settings, definitions, registry):
    outcome = apply_published_alarm(db_session, published(), settings=settings, now=at(0))
    result = Notifier(db_session, settings).dispatch_pending(at(0))
    assert result.dispatches == 1
    logs = db_session.query(NotificationLog).filter(NotificationLog.alarm_id == outcome.alarm_id).all()
    assert [log.status for log in logs if log.channel == "log"] == ["sent"]
    assert db_session.get(Alarm, outcome.alarm_id).notified is True


def test_an_unregistered_asset_still_raises_and_says_so(db_session, settings, definitions, registry):
    outcome = apply_published_alarm(
        db_session, published(asset_id="energy.load.site.unregistered_01"), settings=settings, now=at(0)
    )
    assert outcome.action == "raised"
    alarm = db_session.get(Alarm, outcome.alarm_id)
    assert "not a registered asset" in alarm.events[0].note
