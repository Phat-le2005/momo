"""0008_simulator_scenario_runs: add sim_scenario_runs table for Prompt 06

Revision ID: 0008_simulator_scenario_runs
Revises: 0007_incident_taskforce
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_simulator_scenario_runs"
down_revision: str | None = "0007_incident_taskforce"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.create_table(
        "sim_scenario_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("scenario_id", sa.String(length=64), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("progress_pct", sa.Integer(), nullable=False),
        sa.Column("orders_generated", sa.Integer(), nullable=False),
        sa.Column("case_ids_json", sa.JSON(), nullable=False),
        sa.Column("incident_ids_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("oracle_truth_json", sa.JSON(), nullable=False),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_sim_scenario_runs_scenario_id"),
        "sim_scenario_runs",
        ["scenario_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sim_scenario_runs_session_id"),
        "sim_scenario_runs",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_sim_scenario_runs_status"),
        "sim_scenario_runs",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_sim_scenario_runs_status"), table_name="sim_scenario_runs")
    op.drop_index(op.f("ix_sim_scenario_runs_session_id"), table_name="sim_scenario_runs")
    op.drop_index(op.f("ix_sim_scenario_runs_scenario_id"), table_name="sim_scenario_runs")
    op.drop_table("sim_scenario_runs")
