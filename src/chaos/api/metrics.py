"""Prometheus text exposition for the platform's own counters (SDD 8.7).

``GET /metrics`` renders the application state the platform already holds --
alarm counts, dead letters, ingest and mirror counters, notification outcomes,
plugin health -- in the Prometheus text format, so the infrastructure
monitoring layer can scrape the control plane without an exporter sidecar.

Two rules keep it honest:

* **Nothing is invented.** Every number is read from the database or from a
  running service's counters. A subsystem that is not running on this node
  (the EMS on the secondary, ingest with MQTT disabled) contributes no series
  at all -- a comment line says so -- because "ingest received nothing" and
  "there is no ingest here" are different facts and a zero would conflate them.
* **The output is deterministic.** Series are emitted in a fixed order with
  sorted labels, so two scrapes of an unchanged platform are byte-identical and
  a diff of the exposition is meaningful.

The format is rendered by hand on purpose: it is a handful of text lines, and
taking a client-library dependency for it would add a package to a control
node for no capability the node needs.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from chaos import __version__
from chaos.models.alarms import SEVERITIES, Alarm, Incident, NotificationLog
from chaos.models.telemetry import IngestDeadLetter
from chaos.plugins.spec import HEALTH_STATES

logger = logging.getLogger(__name__)

#: What a Prometheus scraper expects in ``Content-Type``.
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

#: Alarm states that count as "active" on the metrics surface. Mirrors
#: :data:`chaos.alarms.evaluator.ACTIVE_STATES` without importing the engine:
#: the exposition must keep working when the alarm subsystem cannot be imported.
_ACTIVE_ALARM_STATES = ("active", "acknowledged", "mitigated")

Labels = Mapping[str, Any]


class _Family:
    """One metric family: a name, its help text, its type and its samples."""

    def __init__(self, name: str, kind: str, help_text: str) -> None:
        self.name = name
        self.kind = kind
        self.help = help_text
        self.samples: list[tuple[str, float]] = []

    def add(self, value: float, **labels: Any) -> None:
        rendered = ",".join(f'{key}="{_escape_label(str(val))}"' for key, val in sorted(labels.items()))
        self.samples.append((rendered, float(value)))

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {_escape_help(self.help)}", f"# TYPE {self.name} {self.kind}"]
        for labels, value in sorted(self.samples, key=lambda item: item[0]):
            suffix = f"{{{labels}}}" if labels else ""
            lines.append(f"{self.name}{suffix} {_format_value(value)}")
        return lines


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _format_value(value: float) -> str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------


def _collect_database(session: Session, settings: Any) -> list[_Family]:
    families: list[_Family] = []

    alarms = _Family("chaos_alarms_active", "gauge", "Open alarms in an active lifecycle state, by severity.")
    counts = dict(
        session.execute(
            select(Alarm.severity, func.count())
            .where(Alarm.state.in_(_ACTIVE_ALARM_STATES))
            .group_by(Alarm.severity)
        ).all()
    )
    for severity in SEVERITIES:
        alarms.add(counts.get(severity, 0), severity=severity)
    families.append(alarms)

    suppressed = _Family(
        "chaos_alarms_active_suppressed",
        "gauge",
        "Active alarms whose notification is withheld (maintenance, declared condition, symptom or flood).",
    )
    suppressed.add(
        session.scalar(
            select(func.count())
            .select_from(Alarm)
            .where(Alarm.state.in_(_ACTIVE_ALARM_STATES), Alarm.suppressed.is_(True))
        )
        or 0
    )
    families.append(suppressed)

    incidents = _Family("chaos_incidents_open", "gauge", "Correlated incidents still open.")
    incidents.add(
        session.scalar(select(func.count()).select_from(Incident).where(Incident.state == "open")) or 0
    )
    families.append(incidents)

    dead = _Family(
        "chaos_ingest_dead_letters",
        "gauge",
        "Rejected ingest messages kept for diagnosis (rows in ingest_dead_letters).",
    )
    dead.add(session.scalar(select(func.count()).select_from(IngestDeadLetter)) or 0)
    families.append(dead)

    notifications = _Family(
        "chaos_notifications_total",
        "counter",
        "Notification attempts recorded in the delivery log, by channel and outcome.",
    )
    rows = session.execute(
        select(NotificationLog.channel, NotificationLog.status, func.count()).group_by(
            NotificationLog.channel, NotificationLog.status
        )
    ).all()
    for channel, status, count in rows:
        notifications.add(count, channel=channel or "", status=status or "")
    families.append(notifications)

    return families


def _service_named(services: Iterable[Any], name: str) -> Any | None:
    for service in services:
        if getattr(service, "name", None) == name:
            return service
    return None


def _collect_services(services: list[Any]) -> tuple[list[_Family], list[str]]:
    """Counters from the services running on this node, and notes for the ones that are not."""
    families: list[_Family] = []
    notes: list[str] = []

    ingest = _service_named(services, "ingest")
    running = _Family(
        "chaos_ingest_running", "gauge", "1 when the MQTT ingest subscriber is running on this node."
    )
    running.add(1 if ingest is not None and getattr(ingest, "running", False) else 0)
    families.append(running)
    if ingest is None:
        notes.append("ingest: not registered on this node (CHAOS_MQTT_ENABLED=false or secondary role)")
    else:
        events = _Family(
            "chaos_ingest_events_total",
            "counter",
            "Ingest subscriber counters: messages received, written, dead-lettered, and the rest.",
        )
        for name, value in sorted(dict(getattr(ingest, "counters", {})).items()):
            events.add(value, event=name)
        families.append(events)

    alarms = _service_named(services, "alarms")
    if alarms is None:
        notes.append("alarm engine: not registered on this node (CHAOS_ALARM_ENGINE_ENABLED=false)")
    else:
        cycles = _Family("chaos_alarm_engine_cycles_total", "counter", "Completed alarm-engine cycles.")
        cycles.add(getattr(alarms, "cycles", 0))
        families.append(cycles)

    mirror = _service_named(services, "plugin-mirror")
    if mirror is None:
        notes.append("plugin mirror: no mirror source registered on this node")
    else:
        readings = _Family(
            "chaos_mirror_readings_total",
            "counter",
            "Plugin mirror readings per source: published, or dropped with the reason.",
        )
        available = _Family(
            "chaos_mirror_source_available",
            "gauge",
            "1 when the mirror source's transport reports itself configured.",
        )
        for qualified_name, state in sorted(getattr(mirror, "states", {}).items()):
            readings.add(getattr(state, "published", 0), source=qualified_name, outcome="published")
            for reason, count in sorted(dict(getattr(state, "drops", {})).items()):
                readings.add(count, source=qualified_name, outcome=f"dropped_{reason}")
            try:
                is_available = bool(state.source.available())
            except Exception:
                is_available = False
            available.add(1 if is_available else 0, source=qualified_name)
        families.extend([readings, available])

    return families, notes


def _collect_plugins(plugins: Any) -> list[_Family]:
    health = _Family(
        "chaos_plugin_health",
        "gauge",
        "1 for the plugin's current health state (ok, degraded, not_configured, failed, disabled), 0 otherwise.",
    )
    for record in sorted(getattr(plugins, "records", []), key=lambda r: r.name):
        try:
            state = record.health().state
        except Exception:
            state = "failed"
        for candidate in HEALTH_STATES:
            health.add(1 if candidate == state else 0, plugin=record.name, state=candidate)
    return [health]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def render_metrics(app: Any) -> str:
    """Render every family the platform can report for ``app``."""
    settings = app.state.settings
    session_factory = app.state.session_factory

    info = _Family("chaos_build_info", "gauge", "Platform version and node identity; always 1.")
    info.add(1, version=__version__, node_role=settings.node_role, site_id=settings.site_id)
    families: list[_Family] = [info]
    notes: list[str] = []

    with session_factory() as session:
        families.extend(_collect_database(session, settings))

    manager = getattr(app.state, "services", None)
    service_families, service_notes = _collect_services(list(manager.services) if manager is not None else [])
    families.extend(service_families)
    notes.extend(service_notes)

    plugins = getattr(app.state, "plugins", None)
    if plugins is not None:
        families.extend(_collect_plugins(plugins))

    lines: list[str] = []
    for note in notes:
        lines.append(f"# {note}")
    for family in families:
        lines.extend(family.render())
    return "\n".join(lines) + "\n"
