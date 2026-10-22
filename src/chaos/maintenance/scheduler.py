"""Work generation from maintenance plans (SDD section 18).

Each asset can generate work from a calendar interval, runtime hours, cycle
count, a condition threshold, an alarm occurrence, a seasonal procedure or an
inspection result. The scheduler is deliberately pure: every entry point takes
``now`` so seasonal and interval logic is testable without waiting a year.

The polled triggers (calendar, counters, condition, seasonal) are evaluated by
:func:`generate_work_orders`. The two event-driven triggers each have an entry
point of their own that the originating subsystem calls: the alarm engine calls
:func:`raise_for_alarm_plans` every cycle once alarms have activated and been
correlated, and the inspection endpoint calls :func:`raise_for_inspection`.
:func:`generate_work_orders` covers alarm plans as well, so the periodic
scheduler and the maintainer's "generate" action catch anything the alarm
engine did not get to; the ``(plan, alarm)`` idempotency rule means the two
paths can never raise the same work twice.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from chaos.models.alarms import Alarm
from chaos.models.maintenance import Inspection, MaintenancePlan, SparePart, WorkOrder
from chaos.models.registry import Asset
from chaos.models.telemetry import CurrentState

#: Alarm lifecycle states in which an occurrence raises work. Mirrors
#: :data:`chaos.alarms.evaluator.ACTIVE_STATES` without importing the engine,
#: so the scheduler stays importable when the alarm subsystem is not. A
#: ``detected`` candidate still inside its on-delay is not an occurrence.
ALARM_WORK_STATES = ("active", "acknowledged", "mitigated")

_PRIORITY_RANK = {"low": 0, "normal": 1, "high": 2, "urgent": 3}

#: Trigger types understood by the scheduler, in SDD section 18 order.
TRIGGER_TYPES = (
    "calendar",
    "runtime_hours",
    "cycle_count",
    "condition",
    "alarm",
    "seasonal",
    "inspection",
)

#: Northern-hemisphere season windows keyed by month, used by seasonal plans.
_SEASON_MONTHS = {
    "spring": {3, 4, 5},
    "summer": {6, 7, 8},
    "autumn": {9, 10, 11},
    "winter": {12, 1, 2},
}

_OPERATORS = {
    "lt": lambda a, b: a < b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "ge": lambda a, b: a >= b,
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
}


class MaintenanceError(RuntimeError):
    pass


@dataclass
class ScheduleResult:
    created: list[WorkOrder] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (plan_id, reason)

    @property
    def created_count(self) -> int:
        return len(self.created)


def _as_aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite round-trips datetimes without tzinfo; normalise to UTC-aware."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


def _numeric_current_value(session: Session, point_id: str) -> tuple[float | None, str]:
    state = session.get(CurrentState, point_id)
    if state is None:
        return None, "missing"
    if state.value_numeric is None:
        return None, "non_numeric"
    return state.value_numeric, state.quality


def plan_is_due(session: Session, plan: MaintenancePlan, now: dt.datetime) -> tuple[bool, str]:
    """Decide whether a plan should raise work, and explain why."""
    if not plan.enabled:
        return False, "plan_disabled"

    last = _as_aware(plan.last_completed_at)
    next_due = _as_aware(plan.next_due_at)

    if plan.trigger_type == "calendar":
        if plan.interval_days is None:
            return False, "missing_interval_days"
        due = next_due or ((last + dt.timedelta(days=plan.interval_days)) if last else now)
        return (now >= due, "calendar_due" if now >= due else "not_yet_due")

    if plan.trigger_type in {"runtime_hours", "cycle_count"}:
        if not plan.condition_point:
            return False, "missing_counter_point"
        value, quality = _numeric_current_value(session, plan.condition_point)
        if value is None:
            return False, f"counter_unavailable:{quality}"
        if quality in {"bad", "stale"}:
            # A frozen counter must not be read as "no wear accumulated".
            return False, f"counter_quality:{quality}"
        interval = plan.interval_runtime_h if plan.trigger_type == "runtime_hours" else plan.interval_cycles
        if not interval:
            return False, "missing_counter_interval"
        elapsed = value - (plan.counter_baseline or 0.0)
        return (elapsed >= interval, "counter_due" if elapsed >= interval else "not_yet_due")

    if plan.trigger_type == "condition":
        if not (plan.condition_point and plan.condition_operator):
            return False, "missing_condition"
        value, quality = _numeric_current_value(session, plan.condition_point)
        if value is None:
            return False, f"condition_unavailable:{quality}"
        if quality in {"bad", "stale"}:
            return False, f"condition_quality:{quality}"
        op = _OPERATORS.get(plan.condition_operator)
        if op is None:
            return False, f"unknown_operator:{plan.condition_operator}"
        met = op(value, plan.condition_value)
        return met, "condition_met" if met else "condition_not_met"

    if plan.trigger_type == "seasonal":
        months = _SEASON_MONTHS.get((plan.season or "").lower())
        if not months:
            return False, "unknown_season"
        if now.month not in months:
            return False, "out_of_season"
        # Once per season: suppress if already completed within this season window.
        if last and last.year == now.year and last.month in months:
            return False, "already_done_this_season"
        return True, "season_started"

    if plan.trigger_type == "alarm":
        # Event-driven: raise_for_alarm_plans decides per open alarm occurrence,
        # and generate_work_orders routes alarm plans through it.
        return False, "event_driven"

    if plan.trigger_type == "inspection":
        # Event-driven: raised by raise_for_inspection when a result is recorded.
        return False, "event_driven"

    return False, f"unknown_trigger:{plan.trigger_type}"


def _has_open_work(session: Session, plan_id: str) -> bool:
    stmt = select(WorkOrder.id).where(
        WorkOrder.plan_id == plan_id, WorkOrder.state.in_(("open", "in_progress", "blocked"))
    )
    return session.execute(stmt).first() is not None


def generate_work_orders(
    session: Session, now: dt.datetime, plan_ids: list[str] | None = None
) -> ScheduleResult:
    """Create work orders for every due plan. Idempotent while work stays open.

    Alarm-triggered plans are included: an open alarm occurrence on the plan's
    asset is what makes such a plan due, and the ``(plan, alarm)`` rule in
    :func:`raise_for_alarm_plans` keeps this path and the alarm engine's from
    raising the same work twice.
    """
    result = ScheduleResult()
    stmt = select(MaintenancePlan)
    if plan_ids:
        stmt = stmt.where(MaintenancePlan.id.in_(plan_ids))

    alarm_plans: list[MaintenancePlan] = []
    for plan in session.execute(stmt).scalars():
        if plan.trigger_type == "alarm":
            alarm_plans.append(plan)
            continue
        due, reason = plan_is_due(session, plan, now)
        if not due:
            result.skipped.append((plan.id, reason))
            continue
        if _has_open_work(session, plan.id):
            # Never stack duplicate work for the same plan.
            result.skipped.append((plan.id, "work_already_open"))
            continue

        asset = session.get(Asset, plan.asset_id)
        order = WorkOrder(
            asset_id=plan.asset_id,
            plan_id=plan.id,
            title=f"{plan.name} — {asset.name if asset else plan.asset_id}",
            description=plan.procedure,
            priority=_priority_for(asset),
            state="open",
            opened_at=now,
            due_at=_as_aware(plan.next_due_at) or now,
            parts_used=[],
        )
        session.add(order)
        result.created.append(order)

    if alarm_plans:
        _raise_for_alarm_plans(session, alarm_plans, now, result)

    session.flush()
    return result


def _priority_for(asset: Asset | None) -> str:
    if asset is None:
        return "normal"
    return {
        "life_safety": "urgent",
        "critical": "high",
        "important": "normal",
        "discretionary": "low",
    }.get(asset.criticality, "normal")


def raise_for_alarm_plans(
    session: Session,
    now: dt.datetime,
    *,
    alarms: Iterable[Alarm] | None = None,
    plan_ids: list[str] | None = None,
) -> ScheduleResult:
    """Raise work for every ``trigger_type="alarm"`` plan whose asset has an open alarm.

    SDD section 18, "alarm occurrence". The alarm engine calls this once per
    cycle after evaluation and correlation; it is also what
    :func:`generate_work_orders` runs for alarm plans.

    * **One work order per (plan, alarm occurrence), ever.** A second alarm on
      the same asset is a second occurrence and raises again; the same alarm
      seen on the next cycle does not, and neither does it after the work is
      completed. Idempotency is keyed on the alarm row, not on "is work open".
    * **Only alarms that would notify raise work.** A ``detected`` candidate
      still inside its on-delay has not occurred yet, and a suppressed alarm --
      the symptom of an incident, a flood member, an asset in maintenance --
      is either carried by its root cause or already being worked on.
    * A plan may name one alarm in ``condition_point`` (the column is otherwise
      unused for this trigger type); blank means any alarm on the asset.
    * Priority is the higher of the asset's criticality and the alarm's
      severity: a critical alarm on a discretionary asset still needs attention
      now.

    ``alarms`` restricts the pass to those rows (the engine passes what just
    activated); ``None`` considers every open alarm on an asset that has a plan.
    """
    result = ScheduleResult()
    stmt = select(MaintenancePlan).where(MaintenancePlan.trigger_type == "alarm")
    if plan_ids:
        stmt = stmt.where(MaintenancePlan.id.in_(plan_ids))
    plans = list(session.execute(stmt).scalars())
    if plans:
        _raise_for_alarm_plans(session, plans, now, result, alarms=alarms)
    session.flush()
    return result


def _raise_for_alarm_plans(
    session: Session,
    plans: list[MaintenancePlan],
    now: dt.datetime,
    result: ScheduleResult,
    *,
    alarms: Iterable[Alarm] | None = None,
) -> None:
    by_asset: dict[str, list[MaintenancePlan]] = {}
    for plan in plans:
        if not plan.enabled:
            result.skipped.append((plan.id, "plan_disabled"))
            continue
        by_asset.setdefault(plan.asset_id, []).append(plan)
    if not by_asset:
        return

    if alarms is None:
        alarms = (
            session.execute(
                select(Alarm)
                .where(Alarm.asset_id.in_(list(by_asset)), Alarm.state.in_(ALARM_WORK_STATES))
                .order_by(Alarm.detected_at)
            )
            .scalars()
            .all()
        )

    raised: set[str] = set()
    for alarm in alarms:
        if alarm.asset_id not in by_asset or alarm.state not in ALARM_WORK_STATES or alarm.suppressed:
            continue
        for plan in by_asset[alarm.asset_id]:
            wanted_key = (plan.condition_point or "").strip()
            if wanted_key and wanted_key != alarm.alarm_key:
                continue
            if _has_work_for_occurrence(session, plan.id, alarm.id):
                continue
            asset = session.get(Asset, plan.asset_id)
            order = WorkOrder(
                asset_id=plan.asset_id,
                plan_id=plan.id,
                alarm_id=alarm.id,
                title=f"{plan.name} — {asset.name if asset else plan.asset_id}",
                description=_alarm_work_description(plan, alarm),
                priority=_higher_priority(_priority_for(asset), _priority_for_severity(alarm.severity)),
                state="open",
                opened_at=now,
                due_at=now,
                parts_used=[],
            )
            session.add(order)
            result.created.append(order)
            raised.add(plan.id)

    for asset_plans in by_asset.values():
        for plan in asset_plans:
            if plan.id not in raised:
                result.skipped.append((plan.id, "no_new_alarm_occurrence"))


def _has_work_for_occurrence(session: Session, plan_id: str, alarm_id: str) -> bool:
    stmt = select(WorkOrder.id).where(WorkOrder.plan_id == plan_id, WorkOrder.alarm_id == alarm_id)
    return session.execute(stmt).first() is not None


def _alarm_work_description(plan: MaintenancePlan, alarm: Alarm) -> str:
    raised_by = f"Raised by alarm {alarm.alarm_key} ({alarm.severity}) on {alarm.asset_id}"
    if alarm.message:
        raised_by = f"{raised_by}: {alarm.message}"
    return f"{plan.procedure}\n\n{raised_by}" if plan.procedure else raised_by


def _priority_for_severity(severity: str | None) -> str:
    if severity in {"critical", "emergency"}:
        return "urgent"
    if severity == "major":
        return "high"
    return "normal"


def _higher_priority(first: str, second: str) -> str:
    return first if _PRIORITY_RANK.get(first, 1) >= _PRIORITY_RANK.get(second, 1) else second


def raise_for_alarm(session: Session, alarm: Alarm, now: dt.datetime) -> WorkOrder | None:
    """Create corrective work from one alarm, outside any plan (SDD section 18).

    The alarm engine does not call this; alarm-triggered plans go through
    :func:`raise_for_alarm_plans`. This is the ad-hoc path for an operator or
    an integration that wants corrective work for an alarm no plan covers.
    """
    existing = (
        session.execute(
            select(WorkOrder).where(
                WorkOrder.alarm_id == alarm.id, WorkOrder.state.in_(("open", "in_progress", "blocked"))
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return None

    # Priority comes from the alarm's severity here, not the asset's
    # criticality: a critical alarm on a discretionary asset still needs
    # attention now.
    order = WorkOrder(
        asset_id=alarm.asset_id,
        alarm_id=alarm.id,
        title=f"Corrective work: {alarm.alarm_key}",
        description=alarm.message,
        priority="urgent" if alarm.severity in {"critical", "emergency"} else "high",
        state="open",
        opened_at=now,
        parts_used=[],
    )
    session.add(order)
    session.flush()
    return order


def raise_for_inspection(session: Session, inspection: Inspection, now: dt.datetime) -> WorkOrder | None:
    """A failed or advisory inspection generates follow-up work."""
    if inspection.result == "pass":
        return None
    order = WorkOrder(
        asset_id=inspection.asset_id,
        title=f"Follow-up from inspection ({inspection.result})",
        description=inspection.findings,
        priority="high" if inspection.result == "fail" else "normal",
        state="open",
        opened_at=now,
        parts_used=[],
    )
    session.add(order)
    session.flush()
    return order


def complete_work_order(
    session: Session,
    order: WorkOrder,
    now: dt.datetime,
    completed_by: str,
    note: str | None = None,
    parts_used: list[dict] | None = None,
) -> WorkOrder:
    """Close work, advance its plan, and decrement any spares consumed."""
    order.state = "completed"
    order.completed_at = now
    order.assigned_to = order.assigned_to or completed_by
    order.completion_note = note
    if parts_used:
        order.parts_used = parts_used
        _consume_parts(session, parts_used)

    if order.plan_id:
        plan = session.get(MaintenancePlan, order.plan_id)
        if plan is not None:
            plan.last_completed_at = now
            if plan.trigger_type == "calendar" and plan.interval_days:
                plan.next_due_at = now + dt.timedelta(days=plan.interval_days)
            if plan.trigger_type in {"runtime_hours", "cycle_count"} and plan.condition_point:
                value, quality = _numeric_current_value(session, plan.condition_point)
                # Only rebaseline from a trustworthy reading; a bad counter would
                # otherwise push the next service arbitrarily far into the future.
                if value is not None and quality not in {"bad", "stale"}:
                    plan.counter_baseline = value
    session.flush()
    return order


def _consume_parts(session: Session, parts_used: list[dict]) -> None:
    for entry in parts_used:
        part_number = entry.get("part_number")
        quantity = int(entry.get("quantity", 0) or 0)
        if not part_number or quantity <= 0:
            continue
        part = (
            session.execute(select(SparePart).where(SparePart.part_number == part_number)).scalars().first()
        )
        if part is not None:
            part.quantity_on_hand = max(0, part.quantity_on_hand - quantity)


def parts_below_minimum(session: Session) -> list[SparePart]:
    """Spares at or below their minimum stock level (SDD section 18)."""
    stmt = select(SparePart).where(SparePart.quantity_on_hand <= SparePart.minimum_quantity)
    return list(session.execute(stmt).scalars())


def upcoming_work(session: Session, now: dt.datetime, horizon_days: int = 30) -> list[MaintenancePlan]:
    """Calendar plans falling due inside the horizon, for the operator UI."""
    horizon = now + dt.timedelta(days=horizon_days)
    plans = session.execute(select(MaintenancePlan).where(MaintenancePlan.enabled.is_(True))).scalars()
    due: list[MaintenancePlan] = []
    for plan in plans:
        next_due = _as_aware(plan.next_due_at)
        if next_due is not None and next_due <= horizon:
            due.append(plan)
    return sorted(due, key=lambda p: _as_aware(p.next_due_at) or now)
