"""Load-derived EMS values (SDD 30.6): restoration energy, backlog, absorb
capacity, curtailment risk and the configurable thermal derate curves.

Every quantity here is built from :class:`LoadSnapshot` records, so the tests
state exactly which load is held off, for how long and with what estimate, and
check that a load without an estimate is named rather than guessed.
"""

from __future__ import annotations

import pytest
from test_ems_shedding import ALL_LOADS, COMPUTE, IRRIGATION, SPA, TOOL_CHARGING, low_reserve_inputs, shed
from test_ems_state_machine import T0, at, make_derived, make_inputs

from chaos.ems import RecordingCommandPort
from chaos.ems.config import EmsConfig
from chaos.ems.derived import (
    DEFERRABLE_TIER_MIN,
    LoadSnapshot,
    deferrable_absorb_capacity_kw,
    deferrable_backlog_energy,
    describe_curve,
    piecewise_derate,
    scheduled_restoration_energy,
)
from chaos.ems.inputs import load_input_key
from chaos.ems.loader import load_schedule
from chaos.ems.shedding import ShedController, current_load_states, load_snapshots
from chaos.models.registry import Asset


@pytest.fixture()
def config() -> EmsConfig:
    return EmsConfig()


@pytest.fixture()
def loads(db_session):
    """The twelve load assets and the shipped schedule."""
    for asset_id in ALL_LOADS:
        db_session.add(
            Asset(
                asset_id=asset_id,
                domain="energy",
                asset_class="load",
                name=asset_id,
                status="planned",
                criticality="discretionary",
                control_authority="supervisory",
            )
        )
    db_session.flush()
    load_schedule(db_session)
    db_session.flush()


@pytest.fixture()
def controller(config, settings, bus) -> ShedController:
    return ShedController(config, RecordingCommandPort(), settings=settings, bus=bus)


def snap(asset_id: str, tier: int, **kwargs) -> LoadSnapshot:
    return LoadSnapshot(asset_id=asset_id, tier=tier, **kwargs)


# ---------------------------------------------------------------------------
# Restoration energy and backlog
# ---------------------------------------------------------------------------


def test_restoration_energy_runs_each_restorable_load_to_the_end_of_the_horizon():
    loads = [
        snap("a", 3, estimated_power_kw=2.0, shed=True, shed_for_h=1.0, restorable_in_h=0.0),
        snap("b", 2, estimated_power_kw=1.0, shed=True, shed_for_h=0.5, restorable_in_h=4.0),
        # Attended equipment: the EMS will not restart it, so no energy is scheduled.
        snap("c", 3, estimated_power_kw=5.0, shed=True, shed_for_h=2.0, restorable_in_h=None),
        # Not shed: nothing to restore.
        snap("d", 2, estimated_power_kw=3.0, shed=False),
    ]
    estimate = scheduled_restoration_energy(loads, 24.0)
    assert estimate.value == pytest.approx(2.0 * 24.0 + 1.0 * 20.0)
    assert estimate.counted == ("a", "b")
    assert estimate.complete


def test_restoration_energy_names_loads_without_an_estimate():
    loads = [snap("known", 3, estimated_power_kw=2.0, shed=True, shed_for_h=1.0, restorable_in_h=0.0)]
    loads.append(snap("unknown", 3, shed=True, shed_for_h=1.0, restorable_in_h=0.0))
    estimate = scheduled_restoration_energy(loads, 24.0)
    assert estimate.value == pytest.approx(48.0)
    assert estimate.unestimated == ("unknown",)
    assert not estimate.complete
    assert "unknown" in " ".join(estimate.assumptions("restoration energy"))


def test_backlog_counts_deferrable_tiers_only_and_caps_at_the_horizon():
    loads = [
        snap("tier1", 1, estimated_power_kw=4.0, shed=True, shed_for_h=5.0, restorable_in_h=0.0),
        snap("tier3", 3, estimated_power_kw=2.0, shed=True, shed_for_h=5.0, restorable_in_h=0.0),
        snap("week", 4, estimated_power_kw=1.0, shed=True, shed_for_h=168.0, restorable_in_h=0.0),
    ]
    estimate = deferrable_backlog_energy(loads, 24.0)
    assert DEFERRABLE_TIER_MIN == 2
    assert estimate.counted == ("tier3", "week")
    assert estimate.value == pytest.approx(2.0 * 5.0 + 1.0 * 24.0)


def test_absorb_capacity_distinguishes_held_off_running_and_unobservable_loads():
    inputs = make_inputs()
    inputs.readings[load_input_key("running", "power_kw")] = inputs.readings["pv_power_kw"]
    loads = [
        snap("held", 3, estimated_power_kw=2.0, shed=True),
        snap("running", 3, estimated_power_kw=8.0),  # measured 6 kW (the PV reading reused)
        snap("dark", 4, estimated_power_kw=1.5),  # neither held off nor measured
        snap("blank", 3),  # no estimate at all
        snap("essential", 1, estimated_power_kw=9.0, shed=True),  # not deferrable
    ]
    estimate = deferrable_absorb_capacity_kw(loads, inputs)
    assert estimate.value == pytest.approx(2.0 + 2.0)
    assert estimate.counted == ("held", "running")
    assert estimate.unobservable == ("dark",)
    assert estimate.unestimated == ("blank",)
    notes = " ".join(estimate.assumptions("absorb capacity"))
    assert "dark" in notes and "blank" in notes


# ---------------------------------------------------------------------------
# compute_derived with load snapshots
# ---------------------------------------------------------------------------


def test_derived_values_default_to_nothing_held_off(config):
    derived = make_derived(make_inputs(), config)
    scheduled = derived.get("scheduled_load_kwh")
    backlog = derived.get("deferrable_backlog_kwh")
    assert scheduled is not None and scheduled.valid and scheduled.value == 0.0
    assert backlog is not None and backlog.valid and backlog.value == 0.0
    assert "no task scheduler" in " ".join(backlog.assumptions)
    # With no loads in play the 24 h forecast is the rolling projection alone.
    assert derived.value("forecast_load_next_24h_kwh") == pytest.approx(2.5 * config.forecast_day_horizon_h)


def test_held_off_loads_feed_the_forecast_and_the_backlog(config):
    loads = [
        snap(COMPUTE, 4, estimated_power_kw=1.0, shed=True, shed_for_h=2.0, restorable_in_h=0.5),
        snap(SPA, 3, estimated_power_kw=3.0, shed=True, shed_for_h=1.0, restorable_in_h=None),
    ]
    derived = make_derived(make_inputs(), config, loads=loads)
    scheduled = derived.get("scheduled_load_kwh")
    assert scheduled.valid
    assert scheduled.value == pytest.approx(1.0 * 23.5)
    assert derived.value("forecast_load_next_24h_kwh") == pytest.approx(2.5 * 24.0 + 23.5)
    assert "a restored load is assumed to run for the rest of the horizon" in scheduled.assumptions
    backlog = derived.get("deferrable_backlog_kwh")
    assert backlog.valid
    assert backlog.value == pytest.approx(1.0 * 2.0 + 3.0 * 1.0)


def test_supplied_scheduler_values_take_precedence(config):
    loads = [snap(COMPUTE, 4, estimated_power_kw=1.0, shed=True, shed_for_h=2.0, restorable_in_h=0.0)]
    derived = make_derived(
        make_inputs(), config, loads=loads, scheduled_load_kwh=7.0, deferrable_backlog_kwh=9.0
    )
    assert derived.value("scheduled_load_kwh") == 7.0
    assert derived.value("deferrable_backlog_kwh") == 9.0
    assert "supplied by the caller" in derived.get("deferrable_backlog_kwh").basis
    assert derived.value("forecast_load_next_24h_kwh") == pytest.approx(2.5 * 24.0 + 7.0)


def test_a_load_without_an_estimate_is_named_not_guessed(config):
    loads = [snap(IRRIGATION, 2, shed=True, shed_for_h=1.0, restorable_in_h=0.0)]
    derived = make_derived(make_inputs(), config, loads=loads)
    scheduled = derived.get("scheduled_load_kwh")
    assert not scheduled.valid
    assert scheduled.value == 0.0
    assert IRRIGATION in " ".join(scheduled.assumptions)
    assert IRRIGATION in " ".join(derived.get("forecast_load_next_24h_kwh").assumptions)
    assert not derived.get("deferrable_backlog_kwh").valid


# ---------------------------------------------------------------------------
# Curtailment risk
# ---------------------------------------------------------------------------


def test_curtailment_risk_is_surplus_the_deferrable_loads_cannot_absorb(config):
    inputs = make_inputs(pv_power_kw=20.0, battery_charge_limit_kw=5.0)
    loads = [
        snap(COMPUTE, 4, estimated_power_kw=3.0, shed=True),
        snap(TOOL_CHARGING, 3, estimated_power_kw=2.0, shed=True),
    ]
    derived = make_derived(inputs, config, loads=loads)
    # PV 20 - load 2.5 - charge acceptance 5 = 12.5 kW surplus.
    assert derived.value("surplus_power_kw") == pytest.approx(12.5)
    assert derived.value("deferrable_absorb_kw") == pytest.approx(5.0)
    assert derived.value("curtailment_risk_kw") == pytest.approx(7.5)
    assert "available_charge_kw" in derived.get("curtailment_risk_kw").basis


def test_curtailment_risk_never_goes_negative(config):
    inputs = make_inputs(pv_power_kw=20.0, battery_charge_limit_kw=5.0)
    loads = [snap(COMPUTE, 4, estimated_power_kw=30.0, shed=True)]
    derived = make_derived(inputs, config, loads=loads)
    assert derived.value("curtailment_risk_kw") == 0.0


def test_curtailment_risk_is_invalid_when_absorb_capacity_is_unknown(config):
    inputs = make_inputs(pv_power_kw=20.0, battery_charge_limit_kw=5.0)
    loads = [snap(COMPUTE, 4, shed=True)]
    derived = make_derived(inputs, config, loads=loads)
    risk = derived.get("curtailment_risk_kw")
    assert not risk.valid
    assert risk.value == pytest.approx(12.5)
    assert COMPUTE in " ".join(risk.assumptions)


# ---------------------------------------------------------------------------
# Thermal derate curves
# ---------------------------------------------------------------------------


def test_piecewise_derate_is_linear_between_points_and_flat_beyond():
    curve = ((45.0, 0.0), (55.0, 100.0))
    assert piecewise_derate(20.0, curve) == 0.0
    assert piecewise_derate(45.0, curve) == 0.0
    assert piecewise_derate(50.0, curve) == pytest.approx(50.0)
    assert piecewise_derate(55.0, curve) == 100.0
    assert piecewise_derate(70.0, curve) == 100.0
    knee = ((40.0, 0.0), (45.0, 10.0), (55.0, 100.0))
    assert piecewise_derate(42.5, knee) == pytest.approx(5.0)
    assert piecewise_derate(50.0, knee) == pytest.approx(55.0)
    assert piecewise_derate(30.0, ()) == 0.0


def test_default_curves_reproduce_the_warning_to_emergency_ramp(config):
    assert config.thermal_derate_battery_curve == ((45.0, 0.0), (55.0, 100.0))
    assert config.thermal_derate_container_curve == ((40.0, 0.0), (50.0, 100.0))
    derived = make_derived(make_inputs(battery_temperature_max_c=50.0, container_temperature_c=22.0), config)
    derate = derived.get("thermal_derate_pct")
    assert derate.value == pytest.approx(50.0)
    assert "commissioning defaults" in " ".join(derate.assumptions)
    assert describe_curve(config.thermal_derate_battery_curve) in " ".join(derate.assumptions)


def test_commissioned_curve_is_honoured_and_named():
    config = EmsConfig(thermal_derate_battery_curve=((40.0, 0.0), (48.0, 30.0), (55.0, 100.0)))
    derived = make_derived(make_inputs(battery_temperature_max_c=44.0, container_temperature_c=22.0), config)
    assert derived.value("thermal_derate_pct") == pytest.approx(15.0)
    assert "(48 C, 30%)" in " ".join(derived.get("thermal_derate_pct").assumptions)


def test_worst_of_battery_and_container_wins(config):
    derived = make_derived(make_inputs(battery_temperature_max_c=46.0, container_temperature_c=48.0), config)
    assert derived.value("thermal_derate_pct") == pytest.approx(80.0)
    assert derived.get("thermal_derate_pct").inputs == (
        "battery_temperature_max_c",
        "container_temperature_c",
    )


# ---------------------------------------------------------------------------
# The projection from live load states
# ---------------------------------------------------------------------------


def test_load_snapshots_reflect_what_the_ems_is_holding_off(db_session, loads, controller, config):
    inputs = low_reserve_inputs()
    shed(controller, db_session, inputs, config, T0)  # S1: compute and tool charging
    later = at(90.0)
    states = current_load_states(db_session, inputs, now=later, config=config)
    by_id = {snapshot.asset_id: snapshot for snapshot in load_snapshots(states, now=later)}

    assert by_id[COMPUTE].shed and by_id[TOOL_CHARGING].shed
    assert by_id[COMPUTE].shed_for_h == pytest.approx(90.0 / 3600.0)
    # Compute: 60 s minimum off time already satisfied; tool charging waits
    # out its 120 s restart delay.
    assert by_id[COMPUTE].restorable_in_h == 0.0
    assert by_id[TOOL_CHARGING].restorable_in_h == pytest.approx(30.0 / 3600.0)
    assert not by_id[SPA].shed and by_id[SPA].shed_for_h is None and by_id[SPA].restorable_in_h is None
    assert by_id[IRRIGATION].tier == 2
