"""EMS tunables (SDD sections 30.2, 30.8, 30.9, 32-35).

Every number in this file is a **commissioning parameter**, not a fact about
the homestead. SDD 30.2 is explicit: the design portfolio still holds two
incompatible energy baselines (roughly 12 kW PV / 40 kWh usable versus roughly
45 kWdc / 800 kWh nominal / 640 kWh usable planning capacity), so the control
narrative is kept capacity-agnostic and works in percentages, reserve energy
and measured limits. The defaults below are documented starting points that
must be re-commissioned against the selected battery chemistry, inverter
capability, generator, climate and the actual site loads.

No threshold may be written at a call site. Every module takes an
:class:`EmsConfig` so that a threshold change is one auditable configuration
revision (SDD 39 test ``EMS-T015``). :func:`load_ems_config` is where that
happens: it builds the effective configuration -- the defaults below,
overridden by the file the ``CHAOS_EMS_CONFIG_FILE`` environment variable
names -- and, given a database session, appends a ``configuration_revisions``
row whenever the effective configuration differs from the last one recorded
for the site. The environment variable is read by this module from the process
environment, not through :class:`chaos.config.Settings`, so the EMS owns its
own tunables without widening the platform settings surface.
"""

from __future__ import annotations

import json
import os
import types
import typing
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.config import Settings, get_settings
from chaos.models.registry import ConfigurationRevision
from chaos.topics import DEFAULT_BASE, telemetry_topic

#: Canonical point name the EMS publishes the site energy state on.
#:
#: Defined in ``data/point_dictionary.yaml`` as a platform-owned ``CALC`` point
#: (``dictionary_status: reconciled_v0_4``) whose enum is exactly
#: :data:`chaos.models.energy.ENERGY_STATES`, and attached to the site asset
#: through the ``ems_site`` point profile. Ingest therefore resolves
#: ``chaos/site/primary/site_01/energy_state`` to
#: ``site.site.primary.01/energy_state`` instead of dead-lettering it
#: (integration finding F-003, resolved).
ENERGY_STATE_POINT = "energy_state"

#: Canonical point name for the per-load budget published by the EMS. This one
#: *is* in the point dictionary (class AO, unit kW) and is part of the
#: ``ems_load`` point profile.
POWER_BUDGET_POINT = "power_budget_kw"

#: Point the EMS writes when it wants the generator to run. SDD 34 / FR-104:
#: the platform *requests* a start through the equipment-native interface.
GENERATOR_START_REQUEST_POINT = "generator_start_request"


def energy_state_topic(settings: Settings) -> str:
    """``chaos/site/primary/site_01/energy_state``.

    One retained message carrying the site energy state, so that every
    subsystem can translate it into its own bounded operating profile
    (SDD 13) instead of the EMS switching each load itself.
    """
    return telemetry_topic(
        settings.site_id, ENERGY_STATE_POINT, base=settings.mqtt_base_topic or DEFAULT_BASE
    )


def load_budget_topic(settings: Settings, asset_id: str) -> str:
    """``chaos/energy/site/load_spa_01/power_budget_kw`` for one load."""
    return telemetry_topic(asset_id, POWER_BUDGET_POINT, base=settings.mqtt_base_topic or DEFAULT_BASE)


#: The two temperature inputs a thermal derate curve exists for.
THERMAL_DERATE_KINDS = ("battery", "container")


def derate_curve_between(warning_c: float, emergency_c: float) -> tuple[tuple[float, float], ...]:
    """The default thermal derate curve: 0% at ``warning_c``, 100% at ``emergency_c``."""
    return ((float(warning_c), 0.0), (float(emergency_c), 100.0))


def normalise_curve(curve: Iterable[Iterable[float]]) -> tuple[tuple[float, float], ...]:
    """Coerce a configured curve (tuples, or lists from a file) into float pairs."""
    points: list[tuple[float, float]] = []
    for point in curve:
        values = tuple(point)
        if len(values) != 2:
            raise ValueError(f"a thermal derate curve point is (temperature_c, derate_pct); got {values!r}")
        temperature, derate = (float(values[0]), float(values[1]))
        if not 0.0 <= derate <= 100.0:
            raise ValueError(f"thermal derate {derate:g}% at {temperature:g} C is outside 0..100%")
        points.append((temperature, derate))
    if len(points) < 2:
        raise ValueError("a thermal derate curve needs at least two (temperature_c, derate_pct) points")
    return tuple(points)


@dataclass(frozen=True)
class EmsConfig:
    """Commissioning parameters for the whole energy manager."""

    # ------------------------------------------------------------------
    # Battery planning basis (SDD 30.2)
    # ------------------------------------------------------------------
    #: Usable planning capacity. Used only to convert SOC% into kWh when the
    #: BMS does not publish ``energy_available_kwh``. Derived values computed
    #: this way are flagged with the assumption, never presented as measured.
    planning_usable_capacity_kwh: float = 640.0
    #: Fraction of usable capacity held back for physical protection and
    #: emergency communications (SDD 30.3 objective 4).
    emergency_reserve_pct: float = 20.0
    #: Planning figure for the critical baseload when the critical panel meter
    #: is unavailable. Never substituted silently: autonomy computed from it is
    #: marked with the assumption.
    planning_critical_load_kw: float = 1.5

    # ------------------------------------------------------------------
    # State entry thresholds (SDD 30.8) and recovery thresholds (SDD 30.9)
    # ------------------------------------------------------------------
    surplus_soc_pct: float = 85.0
    #: Recovery threshold is separate from the entry threshold so a passing
    #: cloud cannot walk the site in and out of SURPLUS (SDD 30.9).
    surplus_exit_soc_pct: float = 75.0
    #: PV must exceed demand by this margin before SURPLUS qualifies.
    surplus_pv_headroom_kw: float = 1.0
    #: Minimum useful run window for a surplus load (SDD 36: do not start a
    #: large thermal process for a five-minute irradiance spike).
    surplus_minimum_window_min: int = 30

    normal_reserve_soc_pct: float = 50.0
    conserve_recovery_soc_pct: float = 60.0
    conserve_recovery_margin_kwh: float = 10.0

    #: Energy above the emergency reserve below which CRITICAL_RESERVE qualifies.
    critical_margin_kwh: float = 20.0
    critical_recovery_margin_kwh: float = 40.0
    #: SDD 30.8: "estimated critical-load autonomy is below the configured
    #: response horizon".
    response_horizon_h: float = 12.0
    critical_autonomy_recovery_h: float = 24.0
    #: Fraction of present load the battery discharge limit must be able to
    #: carry; below it the battery power limit cannot support the load safely.
    discharge_limit_margin: float = 1.1

    # Emergency entry (SDD 30.8). These are protection-adjacent and have no
    # qualification delay.
    battery_temp_emergency_c: float = 55.0
    #: Below the warning threshold no thermal derate applies: the default derate
    #: curve starts here and must stay below the emergency threshold.
    battery_temp_warning_c: float = 45.0
    container_temp_emergency_c: float = 50.0
    container_temp_warning_c: float = 40.0
    #: Thermal derate curves (SDD 30.6 ``thermal_derate_pct``): piecewise-linear
    #: ``(temperature_c, derate_pct)`` points, flat beyond the end points.
    #: ``None`` -- the default -- derives the curve from the two thresholds
    #: above, 0% at the warning threshold rising to 100% at the emergency
    #: threshold, so a threshold change moves the curve with it. A curve given
    #: explicitly (the battery manufacturer's curve, the container HVAC capacity
    #: at commissioning) wins over the derivation. Either way the attribute
    #: reads as the curve in force once the config is built, and
    #: :meth:`thermal_derate_curve_basis` says which of the two it is.
    thermal_derate_battery_curve: tuple[tuple[float, float], ...] | None = None
    thermal_derate_container_curve: tuple[tuple[float, float], ...] | None = None
    #: Fault codes that classify as shutdown-class inverter faults.
    inverter_shutdown_fault_codes: tuple[str, ...] = (
        "shutdown",
        "fault_shutdown",
        "critical",
        "trip",
    )
    inverter_fault_states: tuple[str, ...] = ("fault", "shutdown", "tripped")
    #: ``alarm_summary`` values from a monitored zone that request power
    #: isolation (SDD 30.8: fire/smoke/emergency-stop logic).
    emergency_alarm_summaries: tuple[str, ...] = ("critical",)
    #: Load above which "AC loads remain" is true for the discharge-inhibited
    #: emergency test.
    residual_load_kw: float = 0.1

    # ------------------------------------------------------------------
    # Dwell / qualification delays (SDD 30.9)
    # ------------------------------------------------------------------
    #: Every automatic transition has a qualification delay: a candidate state
    #: must persist this long before it is adopted.
    dwell_default_s: int = 300
    dwell_surplus_s: int = 900
    dwell_normal_s: int = 600
    dwell_conserve_s: int = 300
    dwell_critical_reserve_s: int = 120
    dwell_generator_support_s: int = 60
    dwell_degraded_sensor_s: int = 60
    dwell_black_start_s: int = 0
    #: Emergency is a protection response and is adopted immediately.
    dwell_emergency_s: int = 0

    #: Operator freeze (SDD 30.9). Bounded, and it never suppresses EMERGENCY.
    freeze_default_s: int = 900
    freeze_max_s: int = 3600

    # ------------------------------------------------------------------
    # Input validity (SDD 30.7 DEGRADED_SENSOR)
    # ------------------------------------------------------------------
    #: Fallback staleness when the point's own ``stale_after_s`` is unset.
    input_max_age_s: int = 60
    #: Qualities the EMS will act on. Anything else is invalid: SDD 30.5/30.7
    #: forbid substituting a default for a missing safety-relevant input.
    valid_qualities: tuple[str, ...] = ("good", "calculated")
    #: Time the inputs must stay healthy before leaving DEGRADED_SENSOR.
    degraded_recovery_s: int = 120

    # ------------------------------------------------------------------
    # Load shedding (SDD 32)
    # ------------------------------------------------------------------
    #: States in which automatic shedding is permitted at all.
    shed_states: tuple[str, ...] = ("CONSERVE", "CRITICAL_RESERVE", "EMERGENCY", "GENERATOR_SUPPORT")
    #: Highest tier the EMS may shed. Tier 0 is physical protection and control
    #: survival and is refused independently of the load schedule (SDD 31.2).
    protected_tier: int = 0
    #: Groups are shed one at a time with a confirmation window between them.
    shed_group_interval_s: int = 60
    #: How long to wait for measured load reduction before calling a shed failed.
    shed_confirm_delay_s: int = 60
    #: A load is considered still running above this fraction of its expected
    #: draw (SDD 32.2: measured power confirms the result).
    shed_confirm_power_kw: float = 0.05
    #: Do not re-issue a shed to the same asset faster than this
    #: (SDD 32.3: "do not repeatedly issue rapid on/off commands").
    shed_reissue_interval_s: int = 300
    #: Attempts before the asset is locked out for operator inspection.
    shed_attempt_limit: int = 2

    #: The deepest group the EMS will drive automatically. S7 is a controlled IT
    #: shutdown and S8 is emergency isolation through equipment-native controls;
    #: neither is unlocked until commissioning says so (SDD 32.2, 39).
    max_automatic_shed_group: str = "S6"

    # ------------------------------------------------------------------
    # Load restoration (SDD 33, FR-103)
    # ------------------------------------------------------------------
    #: The energy state must stay improved this long before restoration starts.
    restore_qualification_s: int = 900
    #: Forecast margin required to qualify restoration (SDD 33.1).
    restore_margin_kwh: float = 20.0
    restore_soc_pct: float = 55.0
    #: Wait between restoration groups so each group can be verified (SDD 33.2 rule 5).
    restore_group_interval_s: int = 120
    #: Anti-simultaneous-restart stagger by inrush class (FR-103, SDD 33.2 rule 3).
    restore_stagger_s: dict[str, int] = field(
        default_factory=lambda: {"none": 5, "low": 15, "medium": 60, "high": 180}
    )
    #: How many loads may be started in one restoration step.
    restore_max_per_step: int = 1
    #: Headroom kept free while restoring so a restart cannot overload the bus.
    restore_headroom_reserve_kw: float = 1.0

    # ------------------------------------------------------------------
    # Generator (SDD 34)
    # ------------------------------------------------------------------
    generator_asset_id: str = "energy.generator.site.01"
    #: Start when critical autonomy falls below this (SDD 34.1).
    generator_start_autonomy_h: float = 8.0
    #: ... or when energy above the emergency reserve falls below this.
    generator_start_margin_kwh: float = 15.0
    #: ... or when the forecast margin stays materially negative.
    generator_start_forecast_margin_kwh: float = -20.0
    #: Fuel below this blocks a start (SDD 34.2 permissive).
    generator_min_fuel_pct: float = 20.0
    #: Stop targets (SDD 34.5).
    generator_stop_soc_pct: float = 80.0
    generator_stop_margin_kwh: float = 60.0
    #: Timers (SDD 30.9, 34.4).
    generator_min_run_s: int = 1800
    generator_max_continuous_run_s: int = 28800
    generator_cooldown_s: int = 300
    #: Quiet period after a stop before a new start request may be made.
    generator_restart_inhibit_s: int = 900
    #: How long to wait for the native controller to report a running state.
    generator_start_timeout_s: int = 120
    #: Repeated cranking is the generator controller's business; the EMS stops
    #: asking after this many failed sequences and requires a reset (SDD 34.7).
    generator_start_attempt_limit: int = 3
    generator_running_states: tuple[str, ...] = ("running", "online", "loaded", "run")
    generator_stopped_states: tuple[str, ...] = ("stopped", "off", "standby", "ready")

    # ------------------------------------------------------------------
    # Black start (SDD 35)
    # ------------------------------------------------------------------
    blackstart_min_soc_pct: float = 15.0
    blackstart_battery_temp_min_c: float = -10.0
    blackstart_battery_temp_max_c: float = 45.0
    blackstart_step_timeout_s: int = 300
    #: Points not updated within this window after recovery are unknown, not
    #: assumed unchanged (SDD 35.4).
    blackstart_reconcile_stale_s: int = 300

    # ------------------------------------------------------------------
    # Power-budget leases (SDD 31.4) and dynamic priority (SDD 31.3)
    # ------------------------------------------------------------------
    lease_max_duration_s: int = 14400
    lease_default_duration_s: int = 3600
    #: Only this fraction of the computed surplus is grantable, so a lease can
    #: never consume the whole margin.
    lease_surplus_fraction: float = 0.8
    #: States in which new leases may be granted at all.
    lease_grant_states: tuple[str, ...] = ("SURPLUS", "NORMAL")
    #: States in which every revocable lease is pulled back.
    lease_revoke_states: tuple[str, ...] = (
        "CONSERVE",
        "CRITICAL_RESERVE",
        "EMERGENCY",
        "BLACK_START",
        "DEGRADED_SENSOR",
    )
    lease_min_priority: int = 0
    lease_max_priority: int = 4
    #: A dynamic tier override must expire (SDD 31.3).
    tier_override_max_s: int = 21600

    # ------------------------------------------------------------------
    # Forecast (SDD 30.6)
    # ------------------------------------------------------------------
    forecast_short_horizon_h: float = 6.0
    forecast_day_horizon_h: float = 24.0
    #: Naive persistence fallback: assume the present PV power holds for this
    #: many hours. Used only when the register cannot locate the site or rate
    #: the array (``chaos.ems.forecast``).
    forecast_persistence_window_h: float = 3.0
    #: Confidence margin applied to every PV forecast: the clear-sky curve as
    #: well as the persistence fallback. A commissioning placeholder, not a
    #: loss model; neither forecaster has a weather feed.
    forecast_persistence_derate: float = 0.7
    #: Equivalent full-load hours used by the persistence fallback when today's
    #: PV energy total is unavailable. A placeholder for a real forecast service.
    forecast_daily_equivalent_full_load_h: float = 4.0

    # ------------------------------------------------------------------
    # Rolling windows (SDD 30.6)
    # ------------------------------------------------------------------
    rolling_window_s: int = 300

    # ------------------------------------------------------------------
    # Service loop
    # ------------------------------------------------------------------
    publish_retained: bool = True
    actor: str = "ems"

    def __post_init__(self) -> None:
        for kind in THERMAL_DERATE_KINDS:
            warning = getattr(self, f"{kind}_temp_warning_c")
            emergency = getattr(self, f"{kind}_temp_emergency_c")
            if not warning < emergency:
                raise ValueError(
                    f"{kind}_temp_warning_c ({warning:g} C) must be below "
                    f"{kind}_temp_emergency_c ({emergency:g} C)"
                )
            attribute = f"thermal_derate_{kind}_curve"
            curve = getattr(self, attribute)
            resolved = derate_curve_between(warning, emergency) if curve is None else normalise_curve(curve)
            # The dataclass is frozen; the resolved curve is written exactly once, here.
            object.__setattr__(self, attribute, resolved)
        if "medium" not in self.restore_stagger_s:
            raise ValueError("restore_stagger_s must define the 'medium' inrush class, the fallback stagger")

    def thermal_derate_curve_basis(self, kind: str) -> str:
        """Where the ``kind`` (``battery`` or ``container``) derate curve in force came from."""
        if kind not in THERMAL_DERATE_KINDS:
            raise ValueError(f"unknown thermal derate kind {kind!r}; expected one of {THERMAL_DERATE_KINDS}")
        derived = derate_curve_between(
            getattr(self, f"{kind}_temp_warning_c"), getattr(self, f"{kind}_temp_emergency_c")
        )
        if getattr(self, f"thermal_derate_{kind}_curve") == derived:
            return (
                f"derived from {kind}_temp_warning_c and {kind}_temp_emergency_c "
                "(commissioning defaults, not a manufacturer curve)"
            )
        return "explicitly configured curve (overrides the threshold-derived default)"

    def dwell_s(self, state: str) -> int:
        """Qualification delay for adopting ``state`` (SDD 30.9)."""
        return {
            "SURPLUS": self.dwell_surplus_s,
            "NORMAL": self.dwell_normal_s,
            "CONSERVE": self.dwell_conserve_s,
            "CRITICAL_RESERVE": self.dwell_critical_reserve_s,
            "GENERATOR_SUPPORT": self.dwell_generator_support_s,
            "DEGRADED_SENSOR": self.dwell_degraded_sensor_s,
            "BLACK_START": self.dwell_black_start_s,
            "EMERGENCY": self.dwell_emergency_s,
        }.get(state, self.dwell_default_s)

    def stagger_s(self, inrush_class: str | None) -> int:
        """Anti-simultaneous-restart delay for an inrush class (FR-103)."""
        return self.restore_stagger_s.get(inrush_class or "medium", self.restore_stagger_s["medium"])

    def emergency_reserve_kwh(self, usable_capacity_kwh: float) -> float:
        return usable_capacity_kwh * self.emergency_reserve_pct / 100.0

    def as_dict(self) -> dict[str, Any]:
        """Flat view for the API and for configuration-revision records."""
        return {f.name: getattr(self, f.name) for f in fields(self)}


#: The built-in defaults. The service and the API take the *effective*
#: configuration from :func:`load_ems_config` instead; this constant remains for
#: callers that want the documented starting points themselves.
DEFAULT_CONFIG = EmsConfig()


# ---------------------------------------------------------------------------
# Effective configuration: file overrides and the configuration revision
# ---------------------------------------------------------------------------

#: Environment variable naming the EMS configuration file (YAML or JSON): a
#: mapping of :class:`EmsConfig` field names to values. Read from the process
#: environment by this module; a relative path resolves against the working
#: directory. Unset or empty means the built-in defaults are in force.
EMS_CONFIG_FILE_ENV = "CHAOS_EMS_CONFIG_FILE"
#: ``configuration_revisions.target_type`` for the effective EMS configuration;
#: ``target_id`` is the site.
EMS_CONFIG_TARGET_TYPE = "ems_config"
#: Keys a configuration file may carry besides field names: who changed it and
#: why. They are recorded on the revision, never applied as tunables.
CONFIG_FILE_ATTRIBUTION_KEYS = ("changed_by", "reason")
DEFAULTS_CHANGED_BY = "chaos.ems.config (built-in defaults)"


class EmsConfigError(ValueError):
    """The EMS configuration file cannot be applied. The message lists every problem."""


@dataclass(frozen=True)
class EmsConfigSource:
    """Where the effective configuration came from."""

    #: ``defaults`` or ``file``.
    kind: str
    path: str | None
    #: The fields the file set, with the values as applied.
    overrides: dict[str, Any]
    changed_by: str
    reason: str
    env_var: str = EMS_CONFIG_FILE_ENV

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "env_var": self.env_var,
            "overrides": dict(self.overrides),
            "changed_by": self.changed_by,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class EmsConfigLoad:
    """The effective configuration, its provenance and its revision."""

    config: EmsConfig
    source: EmsConfigSource
    revision_id: str | None = None
    revision: int | None = None
    #: True when this load appended the revision; False when it matched the
    #: last recorded one, or when no session was given to record against.
    recorded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": plain_config(self.config),
            "source": self.source.as_dict(),
            "revision": (
                None
                if self.revision_id is None
                else {
                    "id": self.revision_id,
                    "revision": self.revision,
                    "target_type": EMS_CONFIG_TARGET_TYPE,
                    "recorded_by_this_load": self.recorded,
                }
            ),
            # Approval is an owner decision made outside the platform (SDD 39
            # EMS-T015 names it; nothing here can grant it), so it is recorded
            # as absent rather than assumed.
            "approved_by": None,
        }


def plain_config(config: EmsConfig) -> dict[str, Any]:
    """``as_dict`` as JSON would store it (tuples become lists), for diffs and the API."""
    return json.loads(json.dumps(config.as_dict()))


def config_file_path(environ: Mapping[str, str] | None = None) -> Path | None:
    """The configured file, or ``None`` when the defaults are in force."""
    raw = (os.environ if environ is None else environ).get(EMS_CONFIG_FILE_ENV, "").strip()
    return Path(raw) if raw else None


def read_config_file(path: str | Path) -> dict[str, Any]:
    """Parse the configuration file. Missing, unreadable or malformed is an error, not the defaults."""
    path = Path(path)
    if not path.is_file():
        raise EmsConfigError(f"{EMS_CONFIG_FILE_ENV} names {path}, which does not exist")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EmsConfigError(f"{EMS_CONFIG_FILE_ENV} names {path}, which cannot be read: {exc}") from exc
    is_json = path.suffix.lower() == ".json"
    try:
        document = json.loads(text) if is_json else yaml.safe_load(text)
    except (ValueError, yaml.YAMLError) as exc:
        raise EmsConfigError(f"{path} is not valid {'JSON' if is_json else 'YAML'}: {exc}") from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise EmsConfigError(f"{path} must hold a mapping of EmsConfig field names to values")
    return document


def _describe(hint: Any) -> str:
    return hint.__name__ if isinstance(hint, type) else str(hint).replace("typing.", "")


def _coerce(value: Any, hint: Any, name: str) -> Any:
    """Check ``value`` against the field's annotation, converting lists to the tuples the fields hold."""
    origin = typing.get_origin(hint)
    if origin is typing.Union or origin is types.UnionType:
        members = [member for member in typing.get_args(hint) if member is not type(None)]
        if value is None and len(members) < len(typing.get_args(hint)):
            return None
        if len(members) == 1:
            # ``X | None`` with a value: the problem, if any, is X's to name.
            return _coerce(value, members[0], name)
        for member in members:
            try:
                return _coerce(value, member, name)
            except EmsConfigError:
                continue
        raise EmsConfigError(f"{name}: expected {_describe(hint)}, got {value!r} ({type(value).__name__})")
    if hint is bool:
        if isinstance(value, bool):
            return value
    elif hint is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    elif hint is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    elif hint is str:
        if isinstance(value, str):
            return value
    elif origin is tuple:
        members = typing.get_args(hint)
        if isinstance(value, (list, tuple)):
            if len(members) == 2 and members[1] is Ellipsis:
                return tuple(
                    _coerce(item, members[0], f"{name}[{index}]") for index, item in enumerate(value)
                )
            if len(value) != len(members):
                raise EmsConfigError(f"{name}: expected {len(members)} items, got {len(value)}")
            return tuple(
                _coerce(item, member, f"{name}[{index}]")
                for index, (item, member) in enumerate(zip(value, members, strict=True))
            )
    elif origin is dict:
        key_hint, value_hint = typing.get_args(hint)
        if isinstance(value, dict):
            return {
                _coerce(key, key_hint, f"{name} key"): _coerce(item, value_hint, f"{name}[{key!r}]")
                for key, item in value.items()
            }
    else:  # pragma: no cover - every EmsConfig field type is handled above
        raise EmsConfigError(f"{name}: unsupported field type {_describe(hint)}")
    raise EmsConfigError(f"{name}: expected {_describe(hint)}, got {value!r} ({type(value).__name__})")


def build_config(overrides: Mapping[str, Any], *, source: str = "EMS configuration overrides") -> EmsConfig:
    """Validate ``overrides`` against the :class:`EmsConfig` fields and build the config.

    Every field is checked against its annotation: an unknown key, a value of
    the wrong type, or a combination :class:`EmsConfig` itself refuses is an
    :class:`EmsConfigError` naming the field. Nothing is coerced beyond
    ``int`` to ``float`` and list to tuple.
    """
    hints = typing.get_type_hints(EmsConfig)
    known = {f.name for f in fields(EmsConfig)}
    values: dict[str, Any] = {}
    problems: list[str] = []
    for name, raw in overrides.items():
        if name in CONFIG_FILE_ATTRIBUTION_KEYS:
            continue
        if name not in known:
            problems.append(f"{name}: not an EmsConfig field")
            continue
        try:
            values[name] = _coerce(raw, hints[name], name)
        except EmsConfigError as exc:
            problems.append(str(exc))
    if problems:
        raise EmsConfigError(f"{source} rejected:\n  - " + "\n  - ".join(problems))
    try:
        return EmsConfig(**values)
    except (TypeError, ValueError) as exc:
        raise EmsConfigError(f"{source} rejected:\n  - {exc}") from exc


def load_ems_config(
    session: Session | None = None,
    *,
    settings: Settings | None = None,
    path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> EmsConfigLoad:
    """The effective EMS configuration: the defaults, overridden by the configured file.

    ``path`` names the file directly; otherwise ``CHAOS_EMS_CONFIG_FILE`` is
    read from ``environ`` (the process environment by default). A file that is
    missing, malformed or carries a wrong-typed value raises
    :class:`EmsConfigError`: the EMS does not fall back to the defaults the file
    was meant to override.

    With a ``session`` the result is reconciled against
    ``configuration_revisions`` (SDD 5.6, 39 EMS-T015): when the effective
    configuration differs from the last ``ems_config`` revision recorded for the
    site -- or none exists -- one revision is appended carrying the previous and
    new values, the fields that changed, the source and ``approved_by: null``.
    The caller commits. Without a session nothing is recorded.
    """
    resolved = Path(path) if path is not None else config_file_path(environ)
    if resolved is None:
        config = EmsConfig()
        source = EmsConfigSource(
            kind="defaults",
            path=None,
            overrides={},
            changed_by=DEFAULTS_CHANGED_BY,
            reason=f"built-in defaults in force: {EMS_CONFIG_FILE_ENV} is not set",
        )
    else:
        document = read_config_file(resolved)
        config = build_config(document, source=f"EMS configuration file {resolved}")
        attribution: dict[str, str] = {}
        for key in CONFIG_FILE_ATTRIBUTION_KEYS:
            if key in document and document[key] is not None:
                if not isinstance(document[key], str) or not document[key].strip():
                    raise EmsConfigError(
                        f"EMS configuration file {resolved} rejected:\n  - {key}: expected a non-empty string"
                    )
                attribution[key] = document[key].strip()
        applied = plain_config(config)
        source = EmsConfigSource(
            kind="file",
            path=str(resolved),
            overrides={name: applied[name] for name in document if name in applied},
            changed_by=attribution.get("changed_by", f"unattributed ({EMS_CONFIG_FILE_ENV})"),
            reason=attribution.get("reason", f"EMS configuration loaded from {resolved}"),
        )
    load = EmsConfigLoad(config=config, source=source)
    if session is None:
        return load
    return _reconcile_revision(session, load, (settings or get_settings()).site_id)


def _reconcile_revision(session: Session, load: EmsConfigLoad, target_id: str) -> EmsConfigLoad:
    """Append an ``ems_config`` revision when the effective configuration changed."""
    effective = plain_config(load.config)
    last = session.scalars(
        select(ConfigurationRevision)
        .where(
            ConfigurationRevision.target_type == EMS_CONFIG_TARGET_TYPE,
            ConfigurationRevision.target_id == target_id,
        )
        .order_by(ConfigurationRevision.revision.desc())
    ).first()
    previous = last.diff.get("new") if last is not None and isinstance(last.diff, dict) else None
    if last is not None and previous == effective:
        return replace(load, revision_id=last.id, revision=last.revision, recorded=False)

    # The first revision is compared with the built-in defaults, so the diff
    # shows what the file changed; later ones with the revision before.
    baseline = previous if previous is not None else plain_config(EmsConfig())
    changes = {
        name: {"old": baseline.get(name), "new": value}
        for name, value in effective.items()
        if baseline.get(name) != value
    }
    revision = ConfigurationRevision(
        target_type=EMS_CONFIG_TARGET_TYPE,
        target_id=target_id,
        revision=(last.revision if last is not None else 0) + 1,
        changed_by=load.source.changed_by[:120],
        reason=load.source.reason,
        diff={
            "old": previous,
            "new": effective,
            "changes": changes,
            "compared_with": "previous revision" if previous is not None else "built-in defaults",
            "source": load.source.as_dict(),
            "approved_by": None,
        },
        source_package_version=None,
    )
    session.add(revision)
    session.flush()
    return replace(load, revision_id=revision.id, revision=revision.revision, recorded=True)
