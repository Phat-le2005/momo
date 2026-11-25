"""0015_open_world_guardian: recovery_commands, business_observations, behavioral_baselines, incident_experiences

Revision ID: 0015_open_world_guardian
Revises: 0014_blind_challenge_runs
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_open_world_guardian"
down_revision: str | None = "0014_blind_challenge_runs"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.create_table(
        "recovery_commands",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=True),
        sa.Column("anomaly_id", sa.String(length=64), nullable=True),
        sa.Column("source_binding_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("target_order_id", sa.String(length=64), nullable=True),
        sa.Column("target_payment_id", sa.String(length=64), nullable=True),
        sa.Column("target_event_id", sa.String(length=128), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("policy_decision", sa.String(length=32), server_default="PENDING", nullable=False),
        sa.Column("approval_id", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), server_default="PROPOSED", nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("verification_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_binding_id"], ["commerce_source_bindings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "idempotency_key", name="uq_recovery_cmd_session_idemp"),
    )
    op.create_index("ix_recovery_commands_session_id", "recovery_commands", ["session_id"])
    op.create_index("ix_recovery_commands_case_id", "recovery_commands", ["case_id"])
    op.create_index("ix_recovery_commands_anomaly_id", "recovery_commands", ["anomaly_id"])
    op.create_index(
        "ix_recovery_commands_source_binding_id", "recovery_commands", ["source_binding_id"]
    )
    op.create_index("ix_recovery_commands_merchant_id", "recovery_commands", ["merchant_id"])
    op.create_index("ix_recovery_commands_state", "recovery_commands", ["state"])
    op.create_index("ix_recovery_cmd_case_state", "recovery_commands", ["case_id", "state"])

    op.create_table(
        "business_observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_session_id", sa.String(length=64), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("observation_type", sa.String(length=64), nullable=False),
        sa.Column(
            "evidence_kind", sa.String(length=32), server_default="OBSERVED_FACT", nullable=False
        ),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("entity_refs_json", sa.JSON(), nullable=False),
        sa.Column("attributes_json", sa.JSON(), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column(
            "verification_status", sa.String(length=32), server_default="UNVERIFIED", nullable=False
        ),
        sa.Column("freshness_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_business_observations_session_id", "business_observations", ["session_id"])
    op.create_index(
        "ix_business_observations_merchant_id", "business_observations", ["merchant_id"]
    )
    op.create_index("ix_business_observations_category", "business_observations", ["category"])
    op.create_index(
        "ix_business_observations_correlation_id", "business_observations", ["correlation_id"]
    )
    op.create_index(
        "ix_business_observations_observed_at", "business_observations", ["observed_at"]
    )
    op.create_index(
        "ix_bus_obs_scope_cat", "business_observations", ["session_id", "world_epoch", "category"]
    )

    op.create_table(
        "behavioral_baselines",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("metric_name", sa.String(length=64), nullable=False),
        sa.Column("regime", sa.String(length=32), server_default="NORMAL", nullable=False),
        sa.Column("sample_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("min_samples", sa.Integer(), server_default="5", nullable=False),
        sa.Column("ewma", sa.Float(), server_default="0", nullable=False),
        sa.Column("median", sa.Float(), server_default="0", nullable=False),
        sa.Column("mad", sa.Float(), server_default="0", nullable=False),
        sa.Column("recent_values_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("is_frozen", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "merchant_id",
            "service_name",
            "metric_name",
            "regime",
            name="uq_behavioral_baseline_key",
        ),
    )
    op.create_index("ix_behavioral_baselines_session_id", "behavioral_baselines", ["session_id"])
    op.create_index("ix_behavioral_baselines_merchant_id", "behavioral_baselines", ["merchant_id"])

    op.create_table(
        "incident_experiences",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=True),
        sa.Column("symptom_summary", sa.Text(), nullable=False),
        sa.Column("symptom_tags_json", sa.JSON(), nullable=False),
        sa.Column("validated_root_cause", sa.String(length=255), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("actions_attempted_json", sa.JSON(), nullable=False),
        sa.Column("verified_recovery_steps_json", sa.JSON(), nullable=False),
        sa.Column("policy_version", sa.String(length=32), server_default="1.0", nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_incident_experiences_session_id", "incident_experiences", ["session_id"])
    op.create_index("ix_incident_experiences_merchant_id", "incident_experiences", ["merchant_id"])
    op.create_index("ix_incident_experiences_case_id", "incident_experiences", ["case_id"])


def downgrade() -> None:
    op.drop_table("incident_experiences")
    op.drop_table("behavioral_baselines")
    op.drop_table("business_observations")
    op.drop_table("recovery_commands")
