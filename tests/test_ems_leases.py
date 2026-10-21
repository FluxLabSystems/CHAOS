"""Power-budget leases (SDD 31.4) and dynamic priority (SDD 31.3).

The property under test throughout: **a lease is an allocation, never a safety
permissive**. Granting one issues no command, revoking one issues no command,
and letting one expire issues no command. A subsystem that loses its budget
loses power *allocation*, not protection.
"""

from __future__ import annotations

import datetime as dt

import pytest
from test_ems_shedding import ALL_LOADS, COMPUTE, CONTROL_CORE, IRRIGATION, SPA, WORKSHOP
from test_ems_state_machine import T0, at, make_derived, make_inputs

from chaos.ems import RecordingCommandPort
from chaos.ems.config import EmsConfig
from chaos.ems.derived import ForecastResult
from chaos.ems.leases import LeaseManager
from chaos.ems.loader import effective_tier, load_schedule
from chaos.ems.state_machine import ensure_snapshot
from chaos.models.energy import PowerBudgetLease, PowerLoadProfile
from chaos.models.registry import Asset


@pytest.fixture()
def config() -> EmsConfig:
    return EmsConfig()


@pytest.fixture()
def manager(config) -> LeaseManager:
    return LeaseManager(config)


@pytest.fixture()
def loads(db_session):
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


def surplus_derived(config, now=T0, *, pv_kw=20.0, charge_limit_kw=5.0):
    """Inputs that leave real curtailable surplus: PV above load and charge limit."""
    inputs = make_inputs(
        now,
        pv_power_kw=pv_kw,
        battery_charge_limit_kw=charge_limit_kw,
        battery_soc_pct=90.0,
        battery_energy_available_kwh=576.0,
    )
    return inputs, make_derived(inputs, config, now)


def grant(manager, session, config, *, kw=4.0, asset=WORKSHOP, state="NORMAL", now=T0, **kwargs):
    _, derived = surplus_derived(config, now)
    return manager.request(
        session,
        asset_id=asset,
        requested_kw=kw,
        reason=kwargs.pop("reason", "operator reserved high-solar window"),
        requested_by=kwargs.pop("requested_by", "test.operator"),
        energy_state=state,
        derived=derived,
        now=now,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Granting
# ---------------------------------------------------------------------------


def test_grant_within_the_surplus_budget(manager, db_session, config):
    _, derived = surplus_derived(config)
    # PV 20 kW - load 2.5 kW - charge acceptance 5 kW = 12.5 kW surplus, of
    # which 80% is grantable.
    assert derived.value("surplus_power_kw") == pytest.approx(12.5)
    assert manager.grantable_kw(derived) == pytest.approx(10.0)

    decision = grant(manager, db_session, config, kw=8.0)
    assert decision.granted
    assert decision.lease.state == "active"
    assert decision.lease.granted_kw == 8.0
    assert decision.lease.expires_at > decision.lease.starts_at


def test_grant_beyond_the_surplus_is_denied_and_recorded(manager, db_session, config):
    decision = grant(manager, db_session, config, kw=25.0)
    assert not decision.granted
    assert "exceeds" in decision.reason
    stored = db_session.query(PowerBudgetLease).one()
    assert stored.state == "denied"
    assert stored.revoked_reason == decision.reason


def test_second_grant_respects_what_is_already_allocated(manager, db_session, config):
    assert grant(manager, db_session, config, kw=8.0).granted
    second = grant(manager, db_session, config, kw=4.0, asset=SPA)
    assert not second.granted
    assert "already granted" in second.reason


def test_grants_are_refused_outside_the_permitted_states(manager, db_session, config):
    for state in ("CONSERVE", "CRITICAL_RESERVE", "EMERGENCY", "DEGRADED_SENSOR", "BLACK_START"):
        decision = grant(manager, db_session, config, kw=1.0, state=state)
        assert not decision.granted
        assert state in decision.reason


def test_grant_without_observable_surplus_is_denied(manager, db_session, config):
    inputs = make_inputs(invalid=("battery_charge_limit_kw",))
    derived = make_derived(inputs, config)
    decision = manager.request(
        db_session,
        asset_id=WORKSHOP,
        requested_kw=1.0,
        reason="welding",
        requested_by="op",
        energy_state="NORMAL",
        derived=derived,
        now=T0,
    )
    assert not decision.granted
    assert "not observable" in decision.reason


def test_a_lease_always_expires(manager, db_session, config):
    decision = grant(manager, db_session, config, kw=1.0, duration_s=999_999)
    assert decision.granted
    span = (decision.lease.expires_at - decision.lease.starts_at).total_seconds()
    assert span == config.lease_max_duration_s


def test_lease_requires_a_reason(manager, db_session, config):
    _, derived = surplus_derived(config)
    with pytest.raises(ValueError):
        manager.request(
            db_session,
            asset_id=WORKSHOP,
            requested_kw=1.0,
            reason="",
            requested_by="op",
            energy_state="NORMAL",
            derived=derived,
            now=T0,
        )


# ---------------------------------------------------------------------------
# SDD 36: the surplus must be forecast to persist for the minimum run window
# ---------------------------------------------------------------------------


class TrendForecast:
    """A stub whose only statement is how PV trends over any horizon.

    ``trend`` is the mean forecast power over the horizon relative to the
    present; ``present=False`` models a forecaster that cannot say what the
    present estimate is, so persistence is not observable.
    """

    method = "stub_trend"

    def __init__(self, trend: float, *, present: bool = True) -> None:
        self.trend = trend
        self.present = present

    def forecast(self, horizon_h, *, now, inputs, config) -> ForecastResult:
        pv_kw = inputs.numeric("pv_power_kw") or 0.0
        return ForecastResult(
            kwh=pv_kw * self.trend * horizon_h,
            valid=True,
            method=self.method,
            inputs=("pv_power_kw",),
            start_kw=pv_kw if self.present else None,
        )


def trend_derived(config, trend: float, *, present: bool = True):
    inputs = make_inputs(
        pv_power_kw=20.0,
        battery_charge_limit_kw=5.0,
        battery_soc_pct=90.0,
        battery_energy_available_kwh=576.0,
    )
    return make_derived(inputs, config, forecast=TrendForecast(trend, present=present))


def request(manager, session, derived, *, kw: float, now=T0):
    return manager.request(
        session,
        asset_id=WORKSHOP,
        requested_kw=kw,
        reason="planned welding during the high-solar window",
        requested_by="test.operator",
        energy_state="NORMAL",
        derived=derived,
        now=now,
    )


def test_grant_requires_the_surplus_to_persist_for_the_minimum_window(manager, db_session, config):
    derived = trend_derived(config, 0.5)
    # 12.5 kW of surplus now, but the forecast says PV halves within the 30 min
    # window: 10 kW - 2.5 kW load - 5 kW charge acceptance leaves 2.5 kW, of
    # which 80% is grantable.
    assert derived.value("surplus_power_kw") == pytest.approx(12.5)
    assert derived.value("surplus_sustained_kw") == pytest.approx(2.5)
    assert manager.grantable_kw(derived) == pytest.approx(10.0)
    assert manager.sustained_grantable_kw(derived) == pytest.approx(2.0)

    denied = request(manager, db_session, derived, kw=4.0)
    assert not denied.granted
    assert f"{config.surplus_minimum_window_min} min minimum run window" in denied.reason
    assert "exceeds" in denied.reason
    assert denied.sustained_kw == pytest.approx(2.0)
    assert db_session.get(PowerBudgetLease, denied.lease.lease_id).state == "denied"

    granted = request(manager, db_session, derived, kw=2.0)
    assert granted.granted
    assert "forecast to persist" in granted.reason
    assert granted.as_dict()["sustained_kw"] == pytest.approx(2.0)


def test_grant_is_refused_when_persistence_is_not_forecast(manager, db_session, config):
    derived = trend_derived(config, 1.0, present=False)
    assert derived.value("surplus_power_kw") == pytest.approx(12.5)
    assert derived.value("surplus_sustained_kw") is None
    decision = request(manager, db_session, derived, kw=1.0)
    assert not decision.granted
    assert "not forecast over the" in decision.reason
    assert "minimum run window" in decision.reason


def test_present_surplus_still_bounds_the_grant_first(manager, db_session, config):
    derived = trend_derived(config, 1.0)
    decision = request(manager, db_session, derived, kw=25.0)
    assert not decision.granted
    assert "grantable surplus" in decision.reason
    assert "minimum run window" not in decision.reason


def test_sweep_trims_to_the_present_surplus_not_the_forecast(manager, db_session, config):
    """A running lease is withdrawn when the surplus is gone, not when a
    forecast says it will go: the window rule governs starts only."""
    decision = grant(manager, db_session, config, kw=4.0)
    pessimistic = trend_derived(config, 0.0)
    assert pessimistic.value("surplus_sustained_kw") == 0.0
    result = manager.sweep(db_session, now=at(60), energy_state="NORMAL", derived=pessimistic)
    assert decision.lease.lease_id not in result.revoked


# ---------------------------------------------------------------------------
# Expiry and revocation
# ---------------------------------------------------------------------------


def test_lease_expires_on_time(manager, db_session, config):
    decision = grant(manager, db_session, config, kw=2.0, duration_s=600)
    lease_id = decision.lease.lease_id

    assert manager.granted_kw(db_session, at(599)) == 2.0
    result = manager.sweep(db_session, now=at(599), energy_state="NORMAL")
    assert not result.expired

    result = manager.sweep(db_session, now=at(600), energy_state="NORMAL")
    assert result.expired == [lease_id]
    assert db_session.get(PowerBudgetLease, lease_id).state == "expired"
    assert manager.granted_kw(db_session, at(601)) == 0.0


def test_expiry_issues_no_command(manager, db_session, config):
    """A subsystem is safe when its lease expires because nothing is commanded."""
    port = RecordingCommandPort()
    grant(manager, db_session, config, kw=2.0, duration_s=600)
    manager.sweep(db_session, now=at(601), energy_state="NORMAL")
    assert not port.requests
    # And the lease manager has no way to issue one: it never took a port.
    assert not hasattr(manager, "command_port")


def test_revocation_withdraws_allocation_only(manager, db_session, config):
    decision = grant(manager, db_session, config, kw=3.0)
    lease = manager.revoke(
        db_session,
        decision.lease.lease_id,
        reason="reserve declining",
        actor="test.operator",
        now=at(60),
    )
    assert lease.state == "revoked"
    assert "test.operator" in lease.revoked_reason
    # No shed action and no command were produced by the revocation.
    from chaos.models.energy import LoadShedAction

    assert db_session.query(LoadShedAction).count() == 0


def test_non_revocable_lease_needs_force(manager, db_session, config):
    decision = grant(manager, db_session, config, kw=2.0, revocable=False)
    with pytest.raises(PermissionError):
        manager.revoke(db_session, decision.lease.lease_id, reason="x", actor="op", now=at(10))
    lease = manager.revoke(
        db_session, decision.lease.lease_id, reason="x", actor="op", now=at(10), force=True
    )
    assert lease.state == "revoked"


def test_conserve_withdraws_every_revocable_lease(manager, db_session, config):
    a = grant(manager, db_session, config, kw=4.0)
    b = grant(manager, db_session, config, kw=2.0, asset=SPA, revocable=False)
    result = manager.sweep(db_session, now=at(60), energy_state="CONSERVE")
    assert a.lease.lease_id in result.revoked
    assert b.lease.lease_id not in result.revoked
    assert db_session.get(PowerBudgetLease, b.lease.lease_id).state == "active"


def test_shrinking_surplus_revokes_the_lowest_priority_first(manager, db_session, config):
    high = grant(manager, db_session, config, kw=4.0, asset=WORKSHOP, priority=2)
    low = grant(manager, db_session, config, kw=4.0, asset=COMPUTE, priority=4)
    assert high.granted and low.granted

    # A cloud arrives: PV drops and the grantable surplus with it.
    _, thin = surplus_derived(config, at(60), pv_kw=8.5)
    assert manager.grantable_kw(thin) == pytest.approx(0.8)
    result = manager.sweep(db_session, now=at(60), energy_state="NORMAL", derived=thin)

    assert low.lease.lease_id in result.revoked
    assert high.lease.lease_id in result.revoked  # both, since almost nothing remains
    assert result.revoked.index(low.lease.lease_id) < result.revoked.index(high.lease.lease_id)


def test_budgets_are_reported_per_asset(manager, db_session, config):
    grant(manager, db_session, config, kw=3.0, asset=WORKSHOP)
    budgets = manager.budgets(db_session, at(10))
    assert budgets == {WORKSHOP: 3.0}


# ---------------------------------------------------------------------------
# Dynamic priority (SDD 31.3)
# ---------------------------------------------------------------------------


def test_tier_override_requires_a_reason(manager, db_session, loads):
    with pytest.raises(ValueError):
        manager.set_tier_override(db_session, IRRIGATION, effective_tier=1, reason="", actor="op", now=T0)


def test_tier_override_applies_and_expires(manager, db_session, loads, config):
    profile = db_session.get(PowerLoadProfile, IRRIGATION)
    assert profile.base_tier == 2

    manager.set_tier_override(
        db_session,
        IRRIGATION,
        effective_tier=1,
        reason="soil moisture at the crop-stress threshold",
        actor="agronomy.rule",
        now=T0,
        duration_s=3600,
    )
    profile = db_session.get(PowerLoadProfile, IRRIGATION)
    assert profile.effective_tier == 1
    assert profile.tier_override_expires_at == T0 + dt.timedelta(seconds=3600)
    assert effective_tier(profile, at(60)) == 1

    # After expiry the load is back at its base tier even before the sweep runs.
    assert effective_tier(profile, at(3601)) == 2

    expired = manager.expire_tier_overrides(db_session, now=at(3601))
    assert expired == [IRRIGATION]
    profile = db_session.get(PowerLoadProfile, IRRIGATION)
    assert profile.effective_tier is None
    assert profile.tier_override_reason is None


def test_tier_override_must_expire(manager, db_session, loads):
    with pytest.raises(ValueError):
        manager.set_tier_override(
            db_session, IRRIGATION, effective_tier=1, reason="x", actor="op", now=T0, duration_s=0
        )
    with pytest.raises(ValueError):
        manager.set_tier_override(
            db_session,
            IRRIGATION,
            effective_tier=1,
            reason="x",
            actor="op",
            now=T0,
            expires_at=T0 - dt.timedelta(seconds=1),
        )


def test_tier_override_is_bounded(manager, db_session, loads, config):
    with pytest.raises(ValueError):
        manager.set_tier_override(
            db_session,
            IRRIGATION,
            effective_tier=1,
            reason="x",
            actor="op",
            now=T0,
            expires_at=T0 + dt.timedelta(seconds=config.tier_override_max_s + 60),
        )


def test_tier_override_cannot_promote_into_the_protected_tier(manager, db_session, loads):
    with pytest.raises(ValueError):
        manager.set_tier_override(db_session, SPA, effective_tier=0, reason="party", actor="op", now=T0)
    # ... while a Tier 0 load stays Tier 0 with no override at all.
    assert effective_tier(db_session.get(PowerLoadProfile, CONTROL_CORE), T0) == 0


def test_sweep_expires_tier_overrides(manager, db_session, loads):
    manager.set_tier_override(
        db_session, IRRIGATION, effective_tier=1, reason="stress", actor="op", now=T0, duration_s=60
    )
    result = manager.sweep(db_session, now=at(61), energy_state="NORMAL")
    assert result.tier_overrides_expired == [IRRIGATION]


# ---------------------------------------------------------------------------
# Service integration: a lease expiring mid-tick commands nothing
# ---------------------------------------------------------------------------


def test_service_tick_with_an_expiring_lease_issues_no_commands(
    db_session, session_factory, bus, settings, config, loads, manager
):
    from chaos.ems.service import EnergyManagerService

    snapshot = ensure_snapshot(db_session, now=T0)
    snapshot.state = "NORMAL"
    snapshot.entered_at = T0
    grant(manager, db_session, config, kw=2.0, duration_s=300)
    db_session.commit()

    port = RecordingCommandPort()
    service = EnergyManagerService(
        session_factory,
        bus,
        settings.model_copy(update={"ems_enabled": True}),
        config=config,
        command_port=port,
    )
    result = service.tick(at(400))

    assert result.leases.expired
    assert not port.requests
    assert result.state == "NORMAL"
