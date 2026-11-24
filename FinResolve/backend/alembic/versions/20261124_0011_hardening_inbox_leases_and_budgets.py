"""0011_hardening_inbox_leases_and_budgets: durable event inbox, LLM concurrency leases, atomic budget reservations

Revision ID: 0011_hardening_inbox_leases
Revises: 0010_harden_supervisor_queue
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_hardening_inbox_leases"
down_revision: str | None = "0010_harden_supervisor_queue"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.add_column(
        "supervisor_states",
        sa.Column("llm_dispatched_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_reserved_calls", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_reserved_tokens", sa.Integer(), server_default="0", nullable=False),
    )

    op.create_table(
        "normalized_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("verified_source", sa.String(length=64), nullable=False),
        sa.Column("claimed_source", sa.String(length=64), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("payment_id", sa.String(length=64), nullable=True),
        sa.Column("correlation_id", sa.String(length=128), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("triggered_investigation", sa.Boolean(), nullable=False),
        sa.Column("linked_case_id", sa.String(length=64), nullable=True),
        sa.Column("linked_job_id", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_normalized_events_session_id"),
        "normalized_events",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_normalized_events_session_type",
        "normalized_events",
        ["session_id", "event_type"],
        unique=False,
    )
    op.create_index(
        "uq_normalized_events_scope",
        "normalized_events",
        ["session_id", "world_epoch", "merchant_id", "event_id"],
        unique=True,
    )

    op.create_table(
        "llm_concurrency_leases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("worker_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_llm_concurrency_leases_session_id"),
        "llm_concurrency_leases",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_llm_concurrency_leases_expires_at"),
        "llm_concurrency_leases",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_llm_concurrency_leases_expires_at"), table_name="llm_concurrency_leases")
    op.drop_index(op.f("ix_llm_concurrency_leases_session_id"), table_name="llm_concurrency_leases")
    op.drop_table("llm_concurrency_leases")

    op.drop_index("uq_normalized_events_scope", table_name="normalized_events")
    op.drop_index("ix_normalized_events_session_type", table_name="normalized_events")
    op.drop_index(op.f("ix_normalized_events_session_id"), table_name="normalized_events")
    op.drop_table("normalized_events")

    op.drop_column("supervisor_states", "llm_reserved_tokens")
    op.drop_column("supervisor_states", "llm_reserved_calls")
    op.drop_column("supervisor_states", "llm_dispatched_count")
