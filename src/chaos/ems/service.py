"""The energy manager background service.

One tick is the whole SDD 30-35 cycle:

1. gather inputs (SDD 30.5) and update the rolling windows
2. compute derived values (SDD 30.6)
3. expire and trim power-budget leases (SDD 31.4)
4. evaluate the generator sequence (SDD 34)
5. select and, if the dwell has elapsed, adopt the energy state (SDD 30.7-30.9)
6. confirm outstanding sheds, then shed or restore one group (SDD 32, 33)
7. publish the site energy state and the per-load budgets (SDD 13)

``tick()`` takes ``now`` so every hysteresis, dwell and cooldown behaviour is
testable without sleeping. The background loop is a daemon thread whose
interval is injectable; tests call ``tick()`` directly and never start it.

The configuration is re-read at the start of every tick (step 0): a threshold
changed in the file ``CHAOS_EMS_CONFIG_FILE`` names takes effect at the next
evaluation and is recorded as one configuration revision (SDD 39 EMS-T015).
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from chaos.config import Settings
from chaos.ems import CommandPort, ManagerCommandPort
from chaos.ems.blackstart import BlackStartCoordinator, BlackStartState
from chaos.ems.config import EmsConfig, EmsConfigError, EmsConfigLoad, energy_state_topic, load_ems_config
from chaos.ems.derived import (
    DerivedEnergyState,
    PvForecast,
    RollingWindow,
    compute_derived,
)
from chaos.ems.forecast import registry_forecast
from chaos.ems.generator import GeneratorCoordinator, GeneratorRuntime
from chaos.ems.inputs import EmsInputs, gather_inputs
from chaos.ems.leases import LeaseManager, LeaseSweepResult
from chaos.ems.loader import load_profiles
from chaos.ems.shedding import (
    LoadState,
    RestoreStepResult,
    ShedController,
    ShedStepResult,
    active_shed_groups,
    current_load_states,
    load_snapshots,
)
from chaos.ems.state_machine import (
    EnergyStateMachine,
    StateDecision,
    ensure_snapshot,
    publish_state,
)
from chaos.models.base import utcnow
from chaos.models.energy import EnergyStateSnapshot
from chaos.mqtt import MessageBus

logger = logging.getLogger(__name__)


@dataclass
class TickResult:
    """Everything one evaluation did, for logging, tests and the API."""

    at: dt.datetime
    state: str
    decision: StateDecision | None = None
    inputs: EmsInputs | None = None
    derived: DerivedEnergyState | None = None
    leases: LeaseSweepResult | None = None
    generator: dict[str, Any] | None = None
    shed: ShedStepResult | None = None
    restore: RestoreStepResult | None = None
    confirmations: list[dict[str, Any]] = field(default_factory=list)
    forced_restores: list[dict[str, Any]] = field(default_factory=list)
    published_topic: str | None = None
    skipped_reason: str | None = None
    #: The ``ems_config`` revision in force for this tick (None when the caller
    #: passed its own configuration) and the reason the file could not be
    #: re-read, when it could not.
    config_revision: int | None = None
    config_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "state": self.state,
            "decision": self.decision.as_dict() if self.decision else None,
            "leases": self.leases.as_dict() if self.leases else None,
            "generator": self.generator,
            "shed": self.shed.as_dict() if self.shed else None,
            "restore": self.restore.as_dict() if self.restore else None,
            "confirmations": list(self.confirmations),
            "forced_restores": list(self.forced_restores),
            "published_topic": self.published_topic,
            "skipped_reason": self.skipped_reason,
            "data_quality": self.inputs.data_quality if self.inputs else None,
            "config_revision": self.config_revision,
            "config_error": self.config_error,
        }


class EnergyManagerService:
    """``BackgroundService`` implementation for the EMS.

    Constructed by ``chaos.runtime.build_services`` as
    ``EnergyManagerService(session_factory, bus, settings)``. Without an explicit
    ``config`` the service takes the effective configuration from
    :func:`chaos.ems.config.load_ems_config` -- the defaults overridden by the
    file ``CHAOS_EMS_CONFIG_FILE`` names -- and records the configuration
    revision that load produces (SDD 39 EMS-T015). A file that cannot be applied
    stops the service from being built rather than running it on defaults.
    """

    name = "ems"

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        bus: MessageBus,
        settings: Settings,
        *,
        config: EmsConfig | None = None,
        command_port: CommandPort | None = None,
        interval_s: float | None = None,
        forecast: PvForecast | None = None,
    ) -> None:
        """``forecast`` overrides the registry-derived clear-sky forecaster
        (``chaos.ems.forecast.registry_forecast``) that each tick builds
        otherwise."""
        self.session_factory = session_factory
        self.bus = bus
        self.settings = settings
        self.config_load: EmsConfigLoad | None = None
        self.config_error: str | None = None
        if config is None:
            with session_factory() as session:
                self.config_load = load_ems_config(session, settings=settings)
                session.commit()
            config = self.config_load.config
        self.config = config
        self.interval_s = interval_s if interval_s is not None else settings.ems_tick_interval_s
        self.command_port: CommandPort = command_port or ManagerCommandPort(
            session_factory=session_factory, bus=bus, settings=settings
        )
        self.forecast = forecast

        self.rolling = RollingWindow(window_s=self.config.rolling_window_s)
        self.state_machine = EnergyStateMachine(self.config, settings)
        self.leases = LeaseManager(self.config)
        self.shedding = ShedController(self.config, self.command_port, settings=settings, bus=bus)
        self.generator = GeneratorCoordinator(self.config, self.command_port, settings=settings, bus=bus)
        self.blackstart = BlackStartCoordinator(self.config)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_tick: TickResult | None = None
        self.tick_count = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._restore_runtime_state()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ems", daemon=True)
        self._thread.start()
        logger.info("EMS started with a %.1f s tick", self.interval_s)

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=max(self.interval_s * 2, 5.0))
        logger.info("EMS stopped after %d ticks", self.tick_count)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # pragma: no cover - the loop must survive one bad tick
                logger.exception("EMS tick failed")
            self._stop.wait(self.interval_s)

    def _restore_runtime_state(self) -> None:
        """Reload generator and black-start progress after a restart."""
        with self.session_factory() as session:
            snapshot = ensure_snapshot(session, now=utcnow())
            derived_blob = snapshot.derived or {}
            self.generator.runtime = GeneratorRuntime.from_dict(derived_blob.get("generator"))
            self.blackstart.state = BlackStartState.from_dict(derived_blob.get("black_start"))
            session.commit()

    # -- the cycle -------------------------------------------------------
    def tick(self, now: dt.datetime | None = None, *, session: Session | None = None) -> TickResult:
        """Run one evaluate -> transition -> dispatch cycle."""
        now = now or utcnow()
        if session is not None:
            return self._tick(session, now)
        with self.session_factory() as owned:
            try:
                result = self._tick(owned, now)
                owned.commit()
                return result
            except Exception:
                owned.rollback()
                raise

    def _tick(self, session: Session, now: dt.datetime) -> TickResult:
        self.tick_count += 1
        snapshot = ensure_snapshot(session, now=now)

        if not self.settings.ems_enabled:
            result = TickResult(at=now, state=snapshot.state, skipped_reason="ems_enabled is false")
            self.last_tick = result
            return result

        # 0. configuration ----------------------------------------------
        self._refresh_config(session)

        profiles = load_profiles(session)
        load_asset_ids = [profile.asset_id for profile in profiles]

        # 1. inputs -----------------------------------------------------
        inputs = gather_inputs(session, now, self.config, load_asset_ids=load_asset_ids)

        # 2. derived ----------------------------------------------------
        # The live shed state feeds the derived layer: restoration energy,
        # deferrable backlog and absorb capacity all come from it.
        states: dict[str, LoadState] = current_load_states(session, inputs, now=now, config=self.config)
        derived = compute_derived(
            inputs,
            self.config,
            now=now,
            rolling=self.rolling,
            forecast=self.forecast or registry_forecast(session, site_id=self.settings.site_id),
            loads=load_snapshots(states, now=now),
        )

        # 3. leases -----------------------------------------------------
        lease_result = self.leases.sweep(session, now=now, energy_state=snapshot.state, derived=derived)

        # 4. generator --------------------------------------------------
        generator_decision = self.generator.evaluate(inputs, derived, energy_state=snapshot.state, now=now)

        # 5. state ------------------------------------------------------
        decision = self.state_machine.evaluate(
            session,
            inputs,
            derived,
            now=now,
            black_start_active=self.blackstart.state.active,
            generator_supporting=self.generator.supporting,
            shed_groups_active=active_shed_groups(states),
            generator_request=self.generator.runtime.sequence,
        )
        energy_state = decision.current_state

        # 6. shed / restore ---------------------------------------------
        confirmations = self.shedding.confirm_pending(session, states, energy_state=energy_state, now=now)
        forced = self.shedding.forced_restores(session, states, energy_state=energy_state, now=now)

        shed_result: ShedStepResult | None = None
        restore_result: RestoreStepResult | None = None
        if energy_state in self.config.shed_states:
            shed_result = self.shedding.shed_step(
                session,
                energy_state=energy_state,
                inputs=inputs,
                derived=derived,
                now=now,
                states=states,
                reason=f"{energy_state}: {decision.candidate.reason}",
            )
        elif energy_state in ("SURPLUS", "NORMAL"):
            restore_result = self.shedding.restore_step(
                session,
                energy_state=energy_state,
                state_entered_at=snapshot.entered_at,
                inputs=inputs,
                derived=derived,
                now=now,
                states=states,
            )

        # 7. persist derived scratch state and publish -------------------
        snapshot.shed_groups_active = active_shed_groups(states)
        snapshot.generator_request = self.generator.runtime.sequence
        blob = dict(snapshot.derived or {})
        blob["generator"] = self.generator.status(inputs)
        blob["black_start"] = self.blackstart.state.as_dict()
        snapshot.derived = blob
        session.flush()

        topic = None
        try:
            topic = publish_state(
                self.bus,
                self.settings,
                snapshot,
                now=now,
                reason=decision.candidate.reason,
                retain=self.config.publish_retained,
            )
            self._publish_budgets(session, states, now=now)
        except Exception:  # pragma: no cover - a broker outage must not stop control
            logger.exception("Failed to publish the EMS state")

        result = TickResult(
            at=now,
            state=energy_state,
            decision=decision,
            inputs=inputs,
            derived=derived,
            leases=lease_result,
            generator=generator_decision.as_dict(),
            shed=shed_result,
            restore=restore_result,
            confirmations=[c.as_dict() for c in confirmations],
            forced_restores=[f.as_dict() for f in forced],
            published_topic=topic,
            config_revision=self.config_load.revision if self.config_load is not None else None,
            config_error=self.config_error,
        )
        self.last_tick = result
        return result

    # -- configuration (SDD 30.2, 39 EMS-T015) ---------------------------
    def _refresh_config(self, session: Session) -> None:
        """Re-read the configuration so a threshold change takes effect at this tick.

        Only when the service owns its configuration; a caller that passed one
        keeps it. A file that cannot be applied leaves the last good
        configuration in force -- a typo must not stop supervisory control --
        and is logged once per distinct problem.
        """
        if self.config_load is None:
            return
        try:
            load = load_ems_config(session, settings=self.settings)
        except EmsConfigError as exc:
            message = str(exc)
            if message != self.config_error:
                logger.error(
                    "EMS configuration rejected; revision %s stays in force: %s",
                    self.config_load.revision,
                    message,
                )
                self.config_error = message
            return
        if self.config_error is not None:
            logger.info("EMS configuration readable again")
            self.config_error = None
        if load.config != self.config:
            self._apply_config(load.config)
            logger.warning(
                "EMS configuration changed: revision %s now in force (%s; changed by %s)",
                load.revision,
                load.source.reason,
                load.source.changed_by,
            )
        self.config_load = load

    def _apply_config(self, config: EmsConfig) -> None:
        """Swap the configuration on every coordinator.

        Their runtime state -- the generator sequence, black-start progress,
        the rolling samples and everything in the database -- is untouched;
        only the parameters change.
        """
        self.config = config
        self.rolling.window_s = config.rolling_window_s
        for component in (self.state_machine, self.leases, self.shedding, self.generator, self.blackstart):
            component.config = config

    def _publish_budgets(self, session: Session, states: dict[str, LoadState], *, now: dt.datetime) -> None:
        """Publish each load's power budget (SDD 13: state plus load budget)."""
        leased = self.leases.budgets(session, now)
        for asset_id, state in states.items():
            if state.is_shed:
                budget = 0.0
            elif asset_id in leased:
                budget = leased[asset_id]
            else:
                budget = state.profile.estimated_power_kw or state.profile.rated_power_kw or 0.0
            self.shedding.publish_budget(asset_id, budget, now=now)

    # -- introspection ---------------------------------------------------
    @property
    def state_topic(self) -> str:
        return energy_state_topic(self.settings)

    def snapshot(self, session: Session) -> EnergyStateSnapshot:
        return ensure_snapshot(session, now=utcnow())
