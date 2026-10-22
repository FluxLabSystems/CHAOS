"""Baseline: the schema as create_all built it when migrations were introduced.

Every table the platform declared at that point, exactly as ``Base.metadata``
described it. A database built by ``chaos init-db`` before this revision existed
is stamped here and carried forward by the revisions that follow.

Revision: 0001
Revises: (nothing)
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alarm_definitions",
        sa.Column("alarm_key", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("domain", sa.String(length=30), nullable=True),
        sa.Column("point_name", sa.String(length=120), nullable=True),
        sa.Column("asset_id", sa.String(length=160), nullable=True),
        sa.Column("asset_class", sa.String(length=80), nullable=True),
        sa.Column("trigger_operator", sa.String(length=16), nullable=True),
        sa.Column(
            "trigger_value",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("reset_operator", sa.String(length=16), nullable=True),
        sa.Column(
            "reset_value",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("trigger_expression", sa.Text(), nullable=True),
        sa.Column("on_delay_s", sa.Integer(), nullable=False),
        sa.Column("off_delay_s", sa.Integer(), nullable=False),
        sa.Column("hysteresis", sa.Float(), nullable=True),
        sa.Column(
            "affected_assets",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "probable_causes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("automatic_action", sa.Text(), nullable=True),
        sa.Column("operator_action", sa.Text(), nullable=True),
        sa.Column("procedure_ref", sa.String(length=300), nullable=True),
        sa.Column(
            "escalation_path",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "suppression_conditions",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("maintenance_mode_behaviour", sa.String(length=40), nullable=False),
        sa.Column("parent_alarm_key", sa.String(length=120), nullable=True),
        sa.Column("requires_manual_reset", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "notes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("alarm_key", name=op.f("pk_alarm_definitions")),
    )
    op.create_index(op.f("ix_alarm_definitions_domain"), "alarm_definitions", ["domain"], unique=False)

    op.create_table(
        "asset_classes",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=True),
        sa.Column(
            "allowed_domains",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "required_properties",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "default_points",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("source_section", sa.String(length=40), nullable=True),
        sa.Column("dictionary_status", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_asset_classes")),
    )
    op.create_table(
        "audit_log",
        sa.Column("actor", sa.String(length=160), nullable=False),
        sa.Column("actor_role", sa.String(length=40), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=40), nullable=True),
        sa.Column("target_id", sa.String(length=220), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "detail",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_ts", "audit_log", ["occurred_at"], unique=False)

    op.create_table(
        "calibrations",
        sa.Column("point_id", sa.String(length=220), nullable=False),
        sa.Column("calibrated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calibrated_by", sa.String(length=160), nullable=True),
        sa.Column("method", sa.String(length=200), nullable=True),
        sa.Column("reference_standard", sa.String(length=200), nullable=True),
        sa.Column(
            "curve",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("pre_error", sa.Float(), nullable=True),
        sa.Column("post_error", sa.Float(), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_calibrations")),
    )
    op.create_index(op.f("ix_calibrations_point_id"), "calibrations", ["point_id"], unique=False)

    op.create_table(
        "commissioning_records",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("step_name", sa.String(length=120), nullable=False),
        sa.Column("preconditions", sa.Text(), nullable=True),
        sa.Column("injected_condition", sa.Text(), nullable=True),
        sa.Column("expected_sequence", sa.Text(), nullable=True),
        sa.Column("observed", sa.Text(), nullable=True),
        sa.Column("result", sa.String(length=24), nullable=True),
        sa.Column("performed_by", sa.String(length=160), nullable=True),
        sa.Column("performed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "evidence",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_commissioning_records")),
    )
    op.create_index(
        "ix_commissioning_asset_step", "commissioning_records", ["asset_id", "step"], unique=False
    )

    op.create_table(
        "configuration_revisions",
        sa.Column("target_type", sa.String(length=40), nullable=False),
        sa.Column("target_id", sa.String(length=220), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("changed_by", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "diff",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_package_version", sa.String(length=40), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_configuration_revisions")),
    )
    op.create_index(
        "ix_config_revision_target", "configuration_revisions", ["target_type", "target_id"], unique=False
    )

    op.create_table(
        "energy_state_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("entered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("candidate_state", sa.String(length=30), nullable=True),
        sa.Column("candidate_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frozen_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frozen_by", sa.String(length=160), nullable=True),
        sa.Column(
            "inputs",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "derived",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("data_quality", sa.String(length=20), nullable=False),
        sa.Column(
            "shed_groups_active",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("generator_request", sa.String(length=40), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_energy_state_snapshot")),
    )
    op.create_table(
        "energy_state_transitions",
        sa.Column("from_state", sa.String(length=30), nullable=True),
        sa.Column("to_state", sa.String(length=30), nullable=False),
        sa.Column("trigger", sa.String(length=120), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "inputs_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "derived_snapshot",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=160), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_energy_state_transitions")),
    )
    op.create_index("ix_energy_transition_ts", "energy_state_transitions", ["occurred_at"], unique=False)

    op.create_table(
        "incidents",
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("root_cause_alarm_id", sa.String(length=36), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_incidents")),
    )
    op.create_index(op.f("ix_incidents_state"), "incidents", ["state"], unique=False)

    op.create_table(
        "ingest_dead_letters",
        sa.Column("topic", sa.String(length=400), nullable=False),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_dead_letters")),
    )
    op.create_table(
        "inspections",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("work_order_id", sa.String(length=36), nullable=True),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("inspector", sa.String(length=160), nullable=True),
        sa.Column("result", sa.String(length=24), nullable=False),
        sa.Column("findings", sa.Text(), nullable=True),
        sa.Column(
            "measurements",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inspections")),
    )
    op.create_index(op.f("ix_inspections_asset_id"), "inspections", ["asset_id"], unique=False)

    op.create_table(
        "load_shed_actions",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("group", sa.String(length=60), nullable=True),
        sa.Column("energy_state", sa.String(length=30), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("command_id", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_load_shed_actions")),
    )
    op.create_index("ix_shed_action_ts", "load_shed_actions", ["occurred_at"], unique=False)

    op.create_table(
        "mode_transitions",
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.String(length=160), nullable=False),
        sa.Column("from_mode", sa.String(length=30), nullable=True),
        sa.Column("to_mode", sa.String(length=30), nullable=False),
        sa.Column("changed_by", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mode_transitions")),
    )
    op.create_index(
        "ix_mode_transition_scope_ts", "mode_transitions", ["scope_id", "occurred_at"], unique=False
    )

    op.create_table(
        "notification_log",
        sa.Column("alarm_id", sa.String(length=36), nullable=True),
        sa.Column("incident_id", sa.String(length=36), nullable=True),
        sa.Column("channel", sa.String(length=24), nullable=False),
        sa.Column("recipient", sa.String(length=240), nullable=True),
        sa.Column("subject", sa.String(length=300), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_log")),
    )
    op.create_index(op.f("ix_notification_log_alarm_id"), "notification_log", ["alarm_id"], unique=False)

    op.create_table(
        "operating_modes",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_type", sa.String(length=20), nullable=False),
        sa.Column("scope_id", sa.String(length=160), nullable=False),
        sa.Column("mode", sa.String(length=30), nullable=False),
        sa.Column("previous_mode", sa.String(length=30), nullable=True),
        sa.Column("changed_by", sa.String(length=160), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto_clear_allowed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operating_modes")),
    )
    op.create_index("ix_operating_mode_scope", "operating_modes", ["scope_type", "scope_id"], unique=True)

    op.create_table(
        "point_definitions",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("default_class", sa.String(length=12), nullable=False),
        sa.Column(
            "allowed_classes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("data_type", sa.String(length=30), nullable=False),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("control_capable", sa.Boolean(), nullable=False),
        sa.Column("automatic_control_default", sa.Boolean(), nullable=False),
        sa.Column(
            "enum_values",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "applicable_asset_classes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("source_section", sa.String(length=40), nullable=True),
        sa.Column("dictionary_status", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_point_definitions")),
    )
    op.create_table(
        "point_profiles",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "point_names",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_point_profiles")),
    )
    op.create_table(
        "power_budget_leases",
        sa.Column("lease_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("granted_kw", sa.Float(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revocable", sa.Boolean(), nullable=False),
        sa.Column("requested_by", sa.String(length=160), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("lease_id", name=op.f("pk_power_budget_leases")),
    )
    op.create_index("ix_lease_asset_state", "power_budget_leases", ["asset_id", "state"], unique=False)

    op.create_table(
        "spare_parts",
        sa.Column("part_number", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("quantity_on_hand", sa.Integer(), nullable=False),
        sa.Column("minimum_quantity", sa.Integer(), nullable=False),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column(
            "linked_assets",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("supplier", sa.String(length=200), nullable=True),
        sa.Column("unit_cost", sa.Float(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_spare_parts")),
    )
    op.create_index(op.f("ix_spare_parts_part_number"), "spare_parts", ["part_number"], unique=False)

    op.create_table(
        "telemetry_samples",
        sa.Column("point_id", sa.String(length=220), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("point_name", sa.String(length=120), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column("quality", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_samples")),
    )
    op.create_index("ix_sample_point_ts", "telemetry_samples", ["point_id", "ts"], unique=False)
    op.create_index("ix_sample_ts", "telemetry_samples", ["ts"], unique=False)

    op.create_table(
        "work_orders",
        sa.Column("asset_id", sa.String(length=160), nullable=True),
        sa.Column("plan_id", sa.String(length=36), nullable=True),
        sa.Column("alarm_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assigned_to", sa.String(length=160), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completion_note", sa.Text(), nullable=True),
        sa.Column(
            "parts_used",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_orders")),
    )
    op.create_index("ix_work_order_state", "work_orders", ["state"], unique=False)
    op.create_index(op.f("ix_work_orders_asset_id"), "work_orders", ["asset_id"], unique=False)

    op.create_table(
        "alarms",
        sa.Column("alarm_key", sa.String(length=120), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=True),
        sa.Column("point_id", sa.String(length=220), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=160), nullable=True),
        sa.Column("mitigated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleared_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by", sa.String(length=160), nullable=True),
        sa.Column(
            "trigger_value",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("suppressed", sa.Boolean(), nullable=False),
        sa.Column("suppression_reason", sa.String(length=200), nullable=True),
        sa.Column("notified", sa.Boolean(), nullable=False),
        sa.Column("incident_id", sa.String(length=36), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["alarm_key"], ["alarm_definitions.alarm_key"], name=op.f("fk_alarms_alarm_key_alarm_definitions")
        ),
        sa.ForeignKeyConstraint(
            ["incident_id"],
            ["incidents.id"],
            name=op.f("fk_alarms_incident_id_incidents"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alarms")),
    )
    op.create_index("ix_alarm_asset", "alarms", ["asset_id"], unique=False)
    op.create_index("ix_alarm_key_active", "alarms", ["alarm_key", "state"], unique=False)
    op.create_index("ix_alarm_state_severity", "alarms", ["state", "severity"], unique=False)
    op.create_index(op.f("ix_alarms_incident_id"), "alarms", ["incident_id"], unique=False)

    op.create_table(
        "assets",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("domain", sa.String(length=30), nullable=False),
        sa.Column("asset_class", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("criticality", sa.String(length=30), nullable=False),
        sa.Column("control_authority", sa.String(length=60), nullable=False),
        sa.Column("functional_position", sa.Boolean(), nullable=False),
        sa.Column("parent_id", sa.String(length=160), nullable=True),
        sa.Column(
            "location",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "properties",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "network",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "power",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "dependencies",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "manual_override",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "documentation",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "maintenance_plan",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "point_profile_refs",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "source_refs",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "open_fields",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "tags",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "notes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_class"], ["asset_classes.name"], name=op.f("fk_assets_asset_class_asset_classes")
        ),
        sa.ForeignKeyConstraint(["parent_id"], ["assets.asset_id"], name=op.f("fk_assets_parent_id_assets")),
        sa.PrimaryKeyConstraint("asset_id", name=op.f("pk_assets")),
    )
    op.create_index(op.f("ix_assets_asset_class"), "assets", ["asset_class"], unique=False)
    op.create_index(op.f("ix_assets_domain"), "assets", ["domain"], unique=False)
    op.create_index(op.f("ix_assets_parent_id"), "assets", ["parent_id"], unique=False)
    op.create_index(op.f("ix_assets_status"), "assets", ["status"], unique=False)

    op.create_table(
        "alarm_events",
        sa.Column("alarm_id", sa.String(length=36), nullable=False),
        sa.Column("from_state", sa.String(length=24), nullable=True),
        sa.Column("to_state", sa.String(length=24), nullable=False),
        sa.Column("actor", sa.String(length=160), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["alarm_id"], ["alarms.id"], name=op.f("fk_alarm_events_alarm_id_alarms"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alarm_events")),
    )
    op.create_index(op.f("ix_alarm_events_alarm_id"), "alarm_events", ["alarm_id"], unique=False)

    op.create_table(
        "asset_relationships",
        sa.Column("relationship_id", sa.String(length=60), nullable=False),
        sa.Column("from_asset_id", sa.String(length=160), nullable=False),
        sa.Column("relationship_type", sa.String(length=40), nullable=False),
        sa.Column("to_asset_id", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column(
            "notes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["from_asset_id"],
            ["assets.asset_id"],
            name=op.f("fk_asset_relationships_from_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_asset_id"],
            ["assets.asset_id"],
            name=op.f("fk_asset_relationships_to_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("relationship_id", name=op.f("pk_asset_relationships")),
        sa.UniqueConstraint(
            "from_asset_id", "relationship_type", "to_asset_id", name="uq_relationship_triple"
        ),
    )
    op.create_index(
        "ix_relationship_from_type",
        "asset_relationships",
        ["from_asset_id", "relationship_type"],
        unique=False,
    )
    op.create_index(
        "ix_relationship_to_type", "asset_relationships", ["to_asset_id", "relationship_type"], unique=False
    )

    op.create_table(
        "commands",
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("point_id", sa.String(length=220), nullable=True),
        sa.Column("command", sa.String(length=120), nullable=False),
        sa.Column(
            "value",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("issued_by", sa.String(length=160), nullable=False),
        sa.Column("issued_by_kind", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("operating_mode", sa.String(length=30), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requires_ack", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=120), nullable=True),
        sa.Column("dispatch_topic", sa.String(length=300), nullable=True),
        sa.Column(
            "envelope",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "interlocks_evaluated",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.asset_id"], name=op.f("fk_commands_asset_id_assets"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("command_id", name=op.f("pk_commands")),
    )
    op.create_index("ix_command_asset_issued", "commands", ["asset_id", "issued_at"], unique=False)
    op.create_index("ix_command_idempotency", "commands", ["idempotency_key"], unique=True)
    op.create_index("ix_command_state", "commands", ["state"], unique=False)
    op.create_index(op.f("ix_commands_correlation_id"), "commands", ["correlation_id"], unique=False)

    op.create_table(
        "documents",
        sa.Column("asset_id", sa.String(length=160), nullable=True),
        sa.Column("doc_type", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("uri", sa.String(length=600), nullable=True),
        sa.Column("revision", sa.String(length=60), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.asset_id"], name=op.f("fk_documents_asset_id_assets"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(op.f("ix_documents_asset_id"), "documents", ["asset_id"], unique=False)

    op.create_table(
        "external_identifiers",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("id_type", sa.String(length=40), nullable=False),
        sa.Column("value", sa.String(length=240), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.asset_id"],
            name=op.f("fk_external_identifiers_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_identifiers")),
        sa.UniqueConstraint("asset_id", "id_type", "value", name="uq_external_identifier"),
    )
    op.create_index(
        op.f("ix_external_identifiers_asset_id"), "external_identifiers", ["asset_id"], unique=False
    )

    op.create_table(
        "locations",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("elevation_m", sa.Float(), nullable=True),
        sa.Column(
            "geometry",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("structure_id", sa.String(length=160), nullable=True),
        sa.Column("room_id", sa.String(length=160), nullable=True),
        sa.Column("rack_id", sa.String(length=160), nullable=True),
        sa.Column("rack_unit_start", sa.Integer(), nullable=True),
        sa.Column("rack_unit_height", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.asset_id"], name=op.f("fk_locations_asset_id_assets"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("asset_id", name=op.f("pk_locations")),
    )
    op.create_table(
        "maintenance_plans",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("trigger_type", sa.String(length=30), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        sa.Column("interval_runtime_h", sa.Float(), nullable=True),
        sa.Column("interval_cycles", sa.Integer(), nullable=True),
        sa.Column("condition_point", sa.String(length=220), nullable=True),
        sa.Column("condition_operator", sa.String(length=16), nullable=True),
        sa.Column("condition_value", sa.Float(), nullable=True),
        sa.Column("counter_baseline", sa.Float(), nullable=True),
        sa.Column("season", sa.String(length=30), nullable=True),
        sa.Column("procedure", sa.Text(), nullable=True),
        sa.Column(
            "required_parts",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("estimated_duration_min", sa.Integer(), nullable=True),
        sa.Column("last_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.asset_id"],
            name=op.f("fk_maintenance_plans_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_maintenance_plans")),
    )
    op.create_index("ix_maintenance_plan_asset", "maintenance_plans", ["asset_id"], unique=False)

    op.create_table(
        "points",
        sa.Column("point_id", sa.String(length=220), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("point_name", sa.String(length=120), nullable=False),
        sa.Column("point_class", sa.String(length=12), nullable=False),
        sa.Column("data_type", sa.String(length=30), nullable=False),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column(
            "enum_values",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("control_capable", sa.Boolean(), nullable=False),
        sa.Column("automatic_control_allowed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column(
            "limits",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("historian_policy", sa.String(length=40), nullable=True),
        sa.Column("stale_after_s", sa.Integer(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["assets.asset_id"], name=op.f("fk_points_asset_id_assets"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["point_name"], ["point_definitions.name"], name=op.f("fk_points_point_name_point_definitions")
        ),
        sa.PrimaryKeyConstraint("point_id", name=op.f("pk_points")),
        sa.UniqueConstraint("asset_id", "point_name", name="uq_point_asset_name"),
    )
    op.create_index("ix_point_asset", "points", ["asset_id"], unique=False)

    op.create_table(
        "power_load_profiles",
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("name", sa.String(length=240), nullable=False),
        sa.Column("base_tier", sa.Integer(), nullable=False),
        sa.Column("effective_tier", sa.Integer(), nullable=True),
        sa.Column("tier_override_reason", sa.Text(), nullable=True),
        sa.Column("tier_override_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("criticality", sa.String(length=40), nullable=False),
        sa.Column("rated_power_kw", sa.Float(), nullable=True),
        sa.Column("measured_power_point", sa.String(length=220), nullable=True),
        sa.Column("estimated_power_kw", sa.Float(), nullable=True),
        sa.Column("branch_circuit", sa.String(length=120), nullable=True),
        sa.Column("panel_asset_id", sa.String(length=160), nullable=True),
        sa.Column("control_method", sa.String(length=60), nullable=False),
        sa.Column(
            "minimum_service",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "restart",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "shed",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("restoration_group", sa.String(length=60), nullable=True),
        sa.Column("restoration_order", sa.Integer(), nullable=True),
        sa.Column("shed_group", sa.String(length=60), nullable=True),
        sa.Column("shed_order", sa.Integer(), nullable=True),
        sa.Column("minimum_on_time_s", sa.Integer(), nullable=True),
        sa.Column("minimum_off_time_s", sa.Integer(), nullable=True),
        sa.Column("inrush_class", sa.String(length=20), nullable=True),
        sa.Column("data_status", sa.String(length=30), nullable=False),
        sa.Column(
            "open_fields",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "notes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["asset_id"],
            ["assets.asset_id"],
            name=op.f("fk_power_load_profiles_asset_id_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("asset_id", name=op.f("pk_power_load_profiles")),
    )
    op.create_index("ix_load_profile_tier", "power_load_profiles", ["base_tier"], unique=False)

    op.create_table(
        "command_results",
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=24), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("reported_by", sa.String(length=160), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(
            ["command_id"],
            ["commands.command_id"],
            name=op.f("fk_command_results_command_id_commands"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_command_results")),
    )
    op.create_index(op.f("ix_command_results_command_id"), "command_results", ["command_id"], unique=False)

    op.create_table(
        "current_state",
        sa.Column("point_id", sa.String(length=220), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("point_name", sa.String(length=120), nullable=False),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column(
            "value_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column("quality", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=120), nullable=True),
        sa.Column("sequence", sa.BigInteger(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stale_after_s", sa.Integer(), nullable=True),
        sa.Column(
            "requested_value",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("alarm_state", sa.String(length=30), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["point_id"],
            ["points.point_id"],
            name=op.f("fk_current_state_point_id_points"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("point_id", name=op.f("pk_current_state")),
    )
    op.create_index("ix_current_state_asset", "current_state", ["asset_id"], unique=False)
    op.create_index(op.f("ix_current_state_quality"), "current_state", ["quality"], unique=False)
    op.create_index(op.f("ix_current_state_ts"), "current_state", ["ts"], unique=False)

    op.create_table(
        "point_bindings",
        sa.Column("point_id", sa.String(length=220), nullable=False),
        sa.Column("asset_id", sa.String(length=160), nullable=False),
        sa.Column("point_name", sa.String(length=120), nullable=False),
        sa.Column("binding_status", sa.String(length=30), nullable=False),
        sa.Column("source_protocol", sa.String(length=80), nullable=True),
        sa.Column("source_address", sa.String(length=240), nullable=True),
        sa.Column("mqtt_topic", sa.String(length=300), nullable=True),
        sa.Column("command_topic", sa.String(length=300), nullable=True),
        sa.Column("scale", sa.Float(), nullable=True),
        sa.Column("offset", sa.Float(), nullable=True),
        sa.Column("sample_interval_s", sa.Integer(), nullable=True),
        sa.Column("publish_interval_s", sa.Integer(), nullable=True),
        sa.Column("stale_after_s", sa.Integer(), nullable=True),
        sa.Column("historian_policy", sa.String(length=40), nullable=True),
        sa.Column("quality_policy", sa.String(length=40), nullable=True),
        sa.Column("automatic_control_allowed", sa.Boolean(), nullable=False),
        sa.Column("commissioned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commissioned_by", sa.String(length=120), nullable=True),
        sa.Column(
            "notes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["point_id"],
            ["points.point_id"],
            name=op.f("fk_point_bindings_point_id_points"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("point_id", name=op.f("pk_point_bindings")),
    )
    op.create_index(op.f("ix_point_bindings_asset_id"), "point_bindings", ["asset_id"], unique=False)
    op.create_index(
        op.f("ix_point_bindings_binding_status"), "point_bindings", ["binding_status"], unique=False
    )
    op.create_index(op.f("ix_point_bindings_mqtt_topic"), "point_bindings", ["mqtt_topic"], unique=False)

    op.create_table(
        "point_samples_index",
        sa.Column("point_id", sa.String(length=220), nullable=False),
        sa.Column("historian_backend", sa.String(length=40), nullable=False),
        sa.Column("series_key", sa.String(length=300), nullable=False),
        sa.Column("measurement", sa.String(length=120), nullable=True),
        sa.Column("retention_policy", sa.String(length=60), nullable=True),
        sa.Column(
            "downsample_policy",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("first_sample_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sample_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["point_id"],
            ["points.point_id"],
            name=op.f("fk_point_samples_index_point_id_points"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("point_id", name=op.f("pk_point_samples_index")),
    )


def downgrade() -> None:
    op.drop_table("point_samples_index")
    op.drop_index(op.f("ix_point_bindings_mqtt_topic"), table_name="point_bindings")
    op.drop_index(op.f("ix_point_bindings_binding_status"), table_name="point_bindings")
    op.drop_index(op.f("ix_point_bindings_asset_id"), table_name="point_bindings")

    op.drop_table("point_bindings")
    op.drop_index(op.f("ix_current_state_ts"), table_name="current_state")
    op.drop_index(op.f("ix_current_state_quality"), table_name="current_state")
    op.drop_index("ix_current_state_asset", table_name="current_state")

    op.drop_table("current_state")
    op.drop_index(op.f("ix_command_results_command_id"), table_name="command_results")

    op.drop_table("command_results")
    op.drop_index("ix_load_profile_tier", table_name="power_load_profiles")

    op.drop_table("power_load_profiles")
    op.drop_index("ix_point_asset", table_name="points")

    op.drop_table("points")
    op.drop_index("ix_maintenance_plan_asset", table_name="maintenance_plans")

    op.drop_table("maintenance_plans")
    op.drop_table("locations")
    op.drop_index(op.f("ix_external_identifiers_asset_id"), table_name="external_identifiers")

    op.drop_table("external_identifiers")
    op.drop_index(op.f("ix_documents_asset_id"), table_name="documents")

    op.drop_table("documents")
    op.drop_index(op.f("ix_commands_correlation_id"), table_name="commands")
    op.drop_index("ix_command_state", table_name="commands")
    op.drop_index("ix_command_idempotency", table_name="commands")
    op.drop_index("ix_command_asset_issued", table_name="commands")

    op.drop_table("commands")
    op.drop_index("ix_relationship_to_type", table_name="asset_relationships")
    op.drop_index("ix_relationship_from_type", table_name="asset_relationships")

    op.drop_table("asset_relationships")
    op.drop_index(op.f("ix_alarm_events_alarm_id"), table_name="alarm_events")

    op.drop_table("alarm_events")
    op.drop_index(op.f("ix_assets_status"), table_name="assets")
    op.drop_index(op.f("ix_assets_parent_id"), table_name="assets")
    op.drop_index(op.f("ix_assets_domain"), table_name="assets")
    op.drop_index(op.f("ix_assets_asset_class"), table_name="assets")

    op.drop_table("assets")
    op.drop_index(op.f("ix_alarms_incident_id"), table_name="alarms")
    op.drop_index("ix_alarm_state_severity", table_name="alarms")
    op.drop_index("ix_alarm_key_active", table_name="alarms")
    op.drop_index("ix_alarm_asset", table_name="alarms")

    op.drop_table("alarms")
    op.drop_index(op.f("ix_work_orders_asset_id"), table_name="work_orders")
    op.drop_index("ix_work_order_state", table_name="work_orders")

    op.drop_table("work_orders")
    op.drop_index("ix_sample_ts", table_name="telemetry_samples")
    op.drop_index("ix_sample_point_ts", table_name="telemetry_samples")

    op.drop_table("telemetry_samples")
    op.drop_index(op.f("ix_spare_parts_part_number"), table_name="spare_parts")

    op.drop_table("spare_parts")
    op.drop_index("ix_lease_asset_state", table_name="power_budget_leases")

    op.drop_table("power_budget_leases")
    op.drop_table("point_profiles")
    op.drop_table("point_definitions")
    op.drop_index("ix_operating_mode_scope", table_name="operating_modes")

    op.drop_table("operating_modes")
    op.drop_index(op.f("ix_notification_log_alarm_id"), table_name="notification_log")

    op.drop_table("notification_log")
    op.drop_index("ix_mode_transition_scope_ts", table_name="mode_transitions")

    op.drop_table("mode_transitions")
    op.drop_index("ix_shed_action_ts", table_name="load_shed_actions")

    op.drop_table("load_shed_actions")
    op.drop_index(op.f("ix_inspections_asset_id"), table_name="inspections")

    op.drop_table("inspections")
    op.drop_table("ingest_dead_letters")
    op.drop_index(op.f("ix_incidents_state"), table_name="incidents")

    op.drop_table("incidents")
    op.drop_index("ix_energy_transition_ts", table_name="energy_state_transitions")

    op.drop_table("energy_state_transitions")
    op.drop_table("energy_state_snapshot")
    op.drop_index("ix_config_revision_target", table_name="configuration_revisions")

    op.drop_table("configuration_revisions")
    op.drop_index("ix_commissioning_asset_step", table_name="commissioning_records")

    op.drop_table("commissioning_records")
    op.drop_index(op.f("ix_calibrations_point_id"), table_name="calibrations")

    op.drop_table("calibrations")
    op.drop_index("ix_audit_ts", table_name="audit_log")

    op.drop_table("audit_log")
    op.drop_table("asset_classes")
    op.drop_index(op.f("ix_alarm_definitions_domain"), table_name="alarm_definitions")

    op.drop_table("alarm_definitions")
