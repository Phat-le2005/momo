"""0009_supervisor_and_challenges: add supervisor_states, supervisor_jobs, challenges, supervisor_activities

Revision ID: 0009_supervisor_and_challenges
Revises: 0008_simulator_scenario_runs
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_supervisor_and_challenges"
down_revision: str | None = "0008_simulator_scenario_runs"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.create_table(
        "supervisor_states",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("autopilot_enabled", sa.Boolean(), nullable=False),
        sa.Column("world_epoch", sa.Integer(), nullable=False),
        sa.Column("active_job_id", sa.String(length=64), nullable=True),
        sa.Column("active_case_id", sa.String(length=64), nullable=True),
        sa.Column("llm_execution_state", sa.String(length=32), nullable=False),
        sa.Column("last_llm_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_llm_failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_llm_error", sa.String(length=512), nullable=True),
        sa.Column("llm_calls_count", sa.Integer(), nullable=False),
        sa.Column("llm_tokens_used", sa.Integer(), nullable=False),
        sa.Column("llm_budget_calls", sa.Integer(), nullable=False),
        sa.Column("llm_budget_tokens", sa.Integer(), nullable=False),
        sa.Column("jobs_processed_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )

    op.create_table(
        "supervisor_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), nullable=False),
        sa.Column("job_type", sa.String(length=48), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("target_case_id", sa.String(length=64), nullable=True),
        sa.Column("challenge_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=64), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.String(length=1024), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_supervisor_jobs_session_id", "supervisor_jobs", ["session_id"], unique=False)
    op.create_index(
        "ix_supervisor_jobs_target_case_id", "supervisor_jobs", ["target_case_id"], unique=False
    )
    op.create_index(
        "ix_supervisor_jobs_challenge_id", "supervisor_jobs", ["challenge_id"], unique=False
    )
    op.create_index("ix_supervisor_jobs_status", "supervisor_jobs", ["status"], unique=False)
    op.create_index(
        "ix_supervisor_jobs_locked_until", "supervisor_jobs", ["locked_until"], unique=False
    )
    op.create_index(
        "ix_supervisor_jobs_session_status",
        "supervisor_jobs",
        ["session_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_supervisor_jobs_dedupe",
        "supervisor_jobs",
        ["session_id", "dedupe_key"],
        unique=False,
    )

    op.create_table(
        "challenges",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), nullable=False),
        sa.Column("challenge_type", sa.String(length=48), nullable=False),
        sa.Column("preset_id", sa.String(length=64), nullable=False),
        sa.Column("title_vi", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("input_json", sa.JSON(), nullable=False),
        sa.Column("generated_refs_json", sa.JSON(), nullable=False),
        sa.Column("linked_case_ids_json", sa.JSON(), nullable=False),
        sa.Column("linked_incident_ids_json", sa.JSON(), nullable=False),
        sa.Column("agent_run_ids_json", sa.JSON(), nullable=False),
        sa.Column("outcome_json", sa.JSON(), nullable=False),
        sa.Column("safety_verdict", sa.String(length=64), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_challenges_session_id", "challenges", ["session_id"], unique=False)
    op.create_index("ix_challenges_challenge_type", "challenges", ["challenge_type"], unique=False)
    op.create_index("ix_challenges_status", "challenges", ["status"], unique=False)

    op.create_table(
        "supervisor_activities",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), nullable=False),
        sa.Column("challenge_id", sa.String(length=64), nullable=True),
        sa.Column("case_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("title_vi", sa.String(length=255), nullable=False),
        sa.Column("detail_vi", sa.String(length=1024), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_supervisor_activities_seq", "supervisor_activities", ["seq"], unique=False)
    op.create_index(
        "ix_supervisor_activities_session_id",
        "supervisor_activities",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_supervisor_activities_challenge_id",
        "supervisor_activities",
        ["challenge_id"],
        unique=False,
    )
    op.create_index(
        "ix_supervisor_activities_case_id",
        "supervisor_activities",
        ["case_id"],
        unique=False,
    )
    op.create_index(
        "ix_supervisor_activities_session_seq",
        "supervisor_activities",
        ["session_id", "seq"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_supervisor_activities_session_seq", table_name="supervisor_activities")
    op.drop_index("ix_supervisor_activities_case_id", table_name="supervisor_activities")
    op.drop_index("ix_supervisor_activities_challenge_id", table_name="supervisor_activities")
    op.drop_index("ix_supervisor_activities_session_id", table_name="supervisor_activities")
    op.drop_index("ix_supervisor_activities_seq", table_name="supervisor_activities")
    op.drop_table("supervisor_activities")

    op.drop_index("ix_challenges_status", table_name="challenges")
    op.drop_index("ix_challenges_challenge_type", table_name="challenges")
    op.drop_index("ix_challenges_session_id", table_name="challenges")
    op.drop_table("challenges")

    op.drop_index("ix_supervisor_jobs_dedupe", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_session_status", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_locked_until", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_status", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_challenge_id", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_target_case_id", table_name="supervisor_jobs")
    op.drop_index("ix_supervisor_jobs_session_id", table_name="supervisor_jobs")
    op.drop_table("supervisor_jobs")

    op.drop_table("supervisor_states")
