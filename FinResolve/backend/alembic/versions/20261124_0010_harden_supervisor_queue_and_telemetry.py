"""0010_harden_supervisor_queue_and_telemetry: worker heartbeats, fencing tokens, dedupe, telemetry

Revision ID: 0010_harden_supervisor_queue
Revises: 0009_supervisor_and_challenges
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_harden_supervisor_queue"
down_revision: str | None = "0009_supervisor_and_challenges"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.create_table(
        "supervisor_workers",
        sa.Column("worker_id", sa.String(length=64), nullable=False),
        sa.Column("worker_mode", sa.String(length=32), nullable=False),
        sa.Column("hostname", sa.String(length=128), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("active_job_id", sa.String(length=64), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("worker_id"),
    )
    op.create_index(
        "ix_supervisor_workers_last_heartbeat_at",
        "supervisor_workers",
        ["last_heartbeat_at"],
        unique=False,
    )

    op.add_column(
        "supervisor_states",
        sa.Column("activity_seq", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_attempts_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_succeeded_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_failed_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_timeout_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_fallback_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "supervisor_states",
        sa.Column("llm_tokens_unknown_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("supervisor_states", "activity_seq", server_default=None)
    op.alter_column("supervisor_states", "llm_attempts_count", server_default=None)
    op.alter_column("supervisor_states", "llm_succeeded_count", server_default=None)
    op.alter_column("supervisor_states", "llm_failed_count", server_default=None)
    op.alter_column("supervisor_states", "llm_timeout_count", server_default=None)
    op.alter_column("supervisor_states", "llm_fallback_count", server_default=None)
    op.alter_column("supervisor_states", "llm_tokens_unknown_count", server_default=None)

    op.add_column(
        "supervisor_jobs",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "supervisor_jobs",
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "supervisor_jobs",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_supervisor_jobs_next_retry_at",
        "supervisor_jobs",
        ["next_retry_at"],
        unique=False,
    )
    op.create_index(
        "uq_supervisor_jobs_active_dedupe",
        "supervisor_jobs",
        ["session_id", "world_epoch", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status IN ('QUEUED', 'RUNNING', 'CANCELLING')"),
    )

    op.drop_index("ix_supervisor_activities_session_seq", table_name="supervisor_activities")
    op.create_index(
        "ix_supervisor_activities_session_seq",
        "supervisor_activities",
        ["session_id", "seq"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_supervisor_activities_session_seq", table_name="supervisor_activities")
    op.create_index(
        "ix_supervisor_activities_session_seq",
        "supervisor_activities",
        ["session_id", "seq"],
        unique=False,
    )

    op.drop_index("uq_supervisor_jobs_active_dedupe", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_next_retry_at", table_name="supervisor_jobs")
    op.drop_column("supervisor_jobs", "next_retry_at")
    op.drop_column("supervisor_jobs", "cancel_requested_at")
    op.drop_column("supervisor_jobs", "claim_token")

    op.drop_column("supervisor_states", "llm_tokens_unknown_count")
    op.drop_column("supervisor_states", "llm_fallback_count")
    op.drop_column("supervisor_states", "llm_timeout_count")
    op.drop_column("supervisor_states", "llm_failed_count")
    op.drop_column("supervisor_states", "llm_succeeded_count")
    op.drop_column("supervisor_states", "llm_attempts_count")
    op.drop_column("supervisor_states", "activity_seq")

    op.drop_index("ix_supervisor_workers_last_heartbeat_at", table_name="supervisor_workers")
    op.drop_table("supervisor_workers")
