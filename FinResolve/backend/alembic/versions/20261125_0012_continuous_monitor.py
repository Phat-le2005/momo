"""0012_continuous_monitor: commerce_source_bindings, anomaly_observations, and case source routing

Revision ID: 0012_continuous_monitor
Revises: 0011_hardening_inbox_leases
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_continuous_monitor"
down_revision: str | None = "0011_hardening_inbox_leases"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.add_column(
        "cases",
        sa.Column(
            "source_system",
            sa.String(length=32),
            server_default="SIMULATOR",
            nullable=False,
        ),
    )
    op.add_column(
        "cases",
        sa.Column("source_binding_id", sa.String(length=64), nullable=True),
    )
    op.create_index("ix_cases_source_system", "cases", ["source_system"], unique=False)
    op.create_index("ix_cases_source_binding_id", "cases", ["source_binding_id"], unique=False)

    op.create_table(
        "commerce_source_bindings",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_session_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_world_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "connectivity_status",
            sa.String(length=32),
            server_default="UNCONFIGURED",
            nullable=False,
        ),
        sa.Column("last_event_cursor", sa.String(length=128), nullable=True),
        sa.Column("last_event_occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_successful_scan_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("worker_id", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.String(length=64), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("events_observed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "merchant_id",
            "commerce_session_id",
            name="uq_commerce_source_binding_scope",
        ),
    )
    op.create_index(
        "ix_commerce_source_bindings_session_id",
        "commerce_source_bindings",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_commerce_source_bindings_merchant_id",
        "commerce_source_bindings",
        ["merchant_id"],
        unique=False,
    )
    op.create_index(
        "ix_commerce_source_bindings_is_enabled",
        "commerce_source_bindings",
        ["is_enabled"],
        unique=False,
    )
    op.create_index(
        "ix_commerce_source_bindings_locked_until",
        "commerce_source_bindings",
        ["locked_until"],
        unique=False,
    )

    op.create_table(
        "anomaly_observations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("source_binding_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("detector_code", sa.String(length=16), nullable=False),
        sa.Column("anomaly_type", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), server_default="HIGH", nullable=False),
        sa.Column(
            "lifecycle_state", sa.String(length=32), server_default="OBSERVED", nullable=False
        ),
        sa.Column("affected_order_id", sa.String(length=64), nullable=True),
        sa.Column("affected_payment_id", sa.String(length=64), nullable=True),
        sa.Column("affected_entities_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs_json", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), server_default="", nullable=False),
        sa.Column("observation_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("first_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("linked_case_id", sa.String(length=64), nullable=True),
        sa.Column("linked_job_id", sa.String(length=64), nullable=True),
        sa.Column("recovery_source", sa.String(length=64), nullable=True),
        sa.Column("verified_recovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_binding_id"], ["commerce_source_bindings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "world_epoch",
            "fingerprint",
            name="uq_anomaly_observation_fingerprint",
        ),
    )
    op.create_index(
        "ix_anomaly_observations_fingerprint", "anomaly_observations", ["fingerprint"], unique=False
    )
    op.create_index(
        "ix_anomaly_observations_session_id", "anomaly_observations", ["session_id"], unique=False
    )
    op.create_index(
        "ix_anomaly_observations_source_binding_id",
        "anomaly_observations",
        ["source_binding_id"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_observations_merchant_id",
        "anomaly_observations",
        ["merchant_id"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_observations_detector_code",
        "anomaly_observations",
        ["detector_code"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_observations_lifecycle_state",
        "anomaly_observations",
        ["lifecycle_state"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_observations_affected_order_id",
        "anomaly_observations",
        ["affected_order_id"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_observations_affected_payment_id",
        "anomaly_observations",
        ["affected_payment_id"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_observations_linked_case_id",
        "anomaly_observations",
        ["linked_case_id"],
        unique=False,
    )
    op.create_index(
        "ix_anomaly_obs_session_state",
        "anomaly_observations",
        ["session_id", "lifecycle_state"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("anomaly_observations")
    op.drop_table("commerce_source_bindings")
    op.drop_index("ix_cases_source_binding_id", table_name="cases")
    op.drop_index("ix_cases_source_system", table_name="cases")
    op.drop_column("cases", "source_binding_id")
    op.drop_column("cases", "source_system")
