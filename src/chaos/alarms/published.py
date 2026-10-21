"""Alarms raised by publication (SDD 10.1 alarm topics, 32.3, 34.7, 37.1).

The EMS does not own the alarm store. When a shed does not take effect, a
restore is rejected, a generator start fails or a reserve decision cannot be
verified, it publishes an :class:`~chaos.envelope.EventEnvelope` on the asset's
alarm topic (``<base>/.../alarm/<alarm_key>``) with ``source="ems"``. Ingest
recognises the platform's own producers and hands those envelopes here, where
they become ordinary :class:`~chaos.models.alarms.Alarm` rows -- so
correlation, notification, escalation, the annunciator and alarm-triggered
maintenance treat an EMS alarm exactly as they treat an evaluated one.

The rules, each of which is a deliberate choice:

* **A definition is required.** ``Alarm.alarm_key`` is a foreign key to
  ``alarm_definitions``, and an alarm nobody defined is not an alarm: it has
  no severity the operator agreed to, no procedure, no escalation path. A
  publication whose key has no definition is reported as *ignored* and ingest
  records it as a platform event. It is never dead-lettered and never raised
  under a guessed definition.
* **A published alarm activates on assertion.** On-delay exists so a sensor
  twitch does not become an alarm; the EMS has already applied its own
  confirmation logic (a measured non-reduction after the confirmation window,
  an exhausted attempt policy) before it publishes. The definition's severity
  is used, because that is the contract the operator agreed to; the severity
  the publisher claimed is kept on the row for the record.
* **Maintenance mode and declared suppression conditions still apply**
  (SDD 11). A load under local manual control that fails to shed is recorded
  and suppressed, not silenced and not shouted about.
* **Re-assertion is cheap.** The EMS may publish the same alarm every tick
  while the condition persists. The open row's ``trigger_value`` records the
  latest assertion and a count; no lifecycle event is appended, so the audit
  trail stays readable.
* **Clearing is explicit.** A publication carrying ``detail.state`` of
  ``cleared`` clears the open alarm. Nothing clears it on the publisher's
  silence: an EMS that stops re-asserting ``load_shed_failed`` may simply have
  moved on to the next group (SDD 32.3) while the load is still drawing power.
  Until the publisher or an operator clears it, the alarm stands.

Published alarms carry ``point_id=None`` and a ``published_by`` marker in
``trigger_value``, which is how the evaluator tells them apart from alarms it
drives itself: it never evaluates a reset condition for them and never closes
one for want of a point in the current-state cache.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.alarms.evaluator import OPEN_STATES, PUBLISHED_BY, AlarmEvaluator, as_utc
from chaos.config import Settings, get_settings
from chaos.envelope import EventEnvelope
from chaos.models.alarms import Alarm
from chaos.models.base import utcnow
from chaos.models.registry import Asset
from chaos.mqtt import MessageBus

logger = logging.getLogger(__name__)

#: ``detail.state`` values that clear an open published alarm.
CLEARING_STATES = frozenset({"cleared", "clear", "reset", "inactive", "normal"})

ACTION_RAISED = "raised"
ACTION_REASSERTED = "reasserted"
ACTION_CLEARED = "cleared"
ACTION_IGNORED = "ignored"


@dataclass(frozen=True)
class PublishedAlarmOutcome:
    """What one publication did to the alarm store, for ingest's counters."""

    action: str
    reason: str
    alarm_key: str
    asset_id: str
    alarm_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "alarm_key": self.alarm_key,
            "asset_id": self.asset_id,
            "alarm_id": self.alarm_id,
        }


def apply_published_alarm(
    session: Session,
    envelope: EventEnvelope,
    *,
    settings: Settings | None = None,
    bus: MessageBus | None = None,
    now: dt.datetime | None = None,
) -> PublishedAlarmOutcome:
    """Raise, re-assert or clear the alarm a platform publication describes."""
    now = now or utcnow()
    settings = settings or get_settings()
    source = envelope.source or "platform"
    alarm_key = envelope.event
    asset_id = envelope.asset_id
    evaluator = AlarmEvaluator(session, bus, settings)

    definition = evaluator.definitions.get(alarm_key)
    if definition is None:
        return PublishedAlarmOutcome(
            ACTION_IGNORED,
            f"no alarm definition is named {alarm_key!r}; recorded as a platform event only",
            alarm_key,
            asset_id,
        )
    if not definition.enabled:
        return PublishedAlarmOutcome(
            ACTION_IGNORED, f"alarm definition {alarm_key!r} is disabled", alarm_key, asset_id
        )

    detail = dict(envelope.detail or {})
    requested_state = str(detail.get("state") or "").strip().lower()
    existing = _open_published_alarm(session, alarm_key, asset_id)

    if requested_state in CLEARING_STATES:
        if existing is None:
            return PublishedAlarmOutcome(
                ACTION_IGNORED, f"no open published {alarm_key!r} on {asset_id} to clear", alarm_key, asset_id
            )
        evaluator.transition(
            existing,
            "cleared",
            now,
            actor=f"publisher:{source}",
            note=f"Cleared by {source}: {_summary(detail, exclude=('state',)) or 'condition no longer asserted'}.",
            definition=definition,
        )
        return PublishedAlarmOutcome(ACTION_CLEARED, f"cleared by {source}", alarm_key, asset_id, existing.id)

    asserted_at = as_utc(envelope.ts) or now
    if existing is not None:
        payload = dict(existing.trigger_value or {})
        payload.update(
            {
                "asserted_at": asserted_at.isoformat(),
                "assertions": int(payload.get("assertions") or 1) + 1,
                "claimed_severity": detail.get("severity"),
                "detail": _plain_detail(detail),
            }
        )
        existing.trigger_value = payload
        existing.updated_at = now
        return PublishedAlarmOutcome(
            ACTION_REASSERTED,
            f"re-asserted by {source} ({payload['assertions']} assertions)",
            alarm_key,
            asset_id,
            existing.id,
        )

    severity, suppression, maintenance_note = evaluator.maintenance_decision(definition, asset_id)
    condition_suppression = evaluator._evaluate_suppression_conditions(definition, asset_id)
    suppression = suppression or condition_suppression
    registered = session.get(Asset, asset_id) is not None

    alarm = Alarm(
        alarm_key=alarm_key,
        asset_id=asset_id,
        point_id=None,
        severity=severity,
        state="detected",
        detected_at=now,
        trigger_value={
            PUBLISHED_BY: source,
            "asserted_at": asserted_at.isoformat(),
            "assertions": 1,
            "claimed_severity": detail.get("severity"),
            "detail": _plain_detail(detail),
        },
        message=f"{definition.name} on {asset_id}, published by {source}: {_summary(detail) or alarm_key}",
        suppressed=bool(suppression),
        suppression_reason=suppression,
        notified=False,
    )
    alarm.created_at = now
    alarm.updated_at = now
    session.add(alarm)
    session.flush()

    note = (
        f"Asserted by {source} on the alarm topic (claimed severity {detail.get('severity') or 'unstated'})."
    )
    if not registered:
        note = f"{note} {asset_id} is not a registered asset."
    if maintenance_note:
        note = f"{note} {maintenance_note}"
    if condition_suppression:
        note = f"{note} {condition_suppression}."
    evaluator._append_event(alarm, None, "detected", now, actor=f"publisher:{source}", note=note)
    evaluator.transition(
        alarm,
        "active",
        now,
        actor=f"publisher:{source}",
        note=(
            "Activated on assertion: a published alarm carries the publisher's own confirmation "
            "logic, so the definition's on-delay does not apply."
        ),
        definition=definition,
    )
    logger.warning("Published alarm %s raised on %s by %s", alarm_key, asset_id, source)
    return PublishedAlarmOutcome(ACTION_RAISED, f"raised by {source}", alarm_key, asset_id, alarm.id)


def is_published(alarm: Alarm) -> bool:
    """True for an alarm that was raised by publication rather than evaluation."""
    payload = alarm.trigger_value or {}
    return isinstance(payload, dict) and bool(payload.get(PUBLISHED_BY))


def _open_published_alarm(session: Session, alarm_key: str, asset_id: str) -> Alarm | None:
    statement = (
        select(Alarm)
        .where(
            Alarm.alarm_key == alarm_key,
            Alarm.asset_id == asset_id,
            Alarm.point_id.is_(None),
            Alarm.state.in_(OPEN_STATES),
        )
        .order_by(Alarm.detected_at.desc())
    )
    for alarm in session.scalars(statement).all():
        if is_published(alarm):
            return alarm
    return None


def _plain_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in detail.items() if key not in ("severity", "state")}


def _summary(detail: dict[str, Any], *, exclude: tuple[str, ...] = ("severity",), limit: int = 240) -> str:
    parts = [f"{key}={value!r}" for key, value in detail.items() if key not in exclude]
    text = ", ".join(parts)
    return text if len(text) <= limit else text[: limit - 3] + "..."
