"""0014_blind_challenge_runs: isolated Judge-only blind challenge lifecycle & Oracle evaluations

Revision ID: 0014_blind_challenge_runs
Revises: 0013_monitor_hardening
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_blind_challenge_runs"
down_revision: str | None = "0013_monitor_hardening"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.create_table(
        "blind_challenge_runs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("world_epoch", sa.Integer(), server_default="1", nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_session_id", sa.String(length=64), nullable=False),
        sa.Column("challenge_family", sa.String(length=32), nullable=False),
        sa.Column("seed", sa.Integer(), server_default="42", nullable=False),
        sa.Column("state", sa.String(length=24), server_default="CREATED", nullable=False),
        sa.Column("injection_config_json", sa.JSON(), nullable=False),
        sa.Column("affected_entities_json", sa.JSON(), nullable=False),
        sa.Column("ground_truth_json", sa.JSON(), nullable=False),
        sa.Column("expected_constraints_json", sa.JSON(), nullable=False),
        sa.Column("evaluation_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_blind_challenge_runs_session_id", "blind_challenge_runs", ["session_id"], unique=False
    )
    op.create_index(
        "ix_blind_challenge_runs_merchant_id", "blind_challenge_runs", ["merchant_id"], unique=False
    )
    op.create_index(
        "ix_blind_challenge_runs_challenge_family",
        "blind_challenge_runs",
        ["challenge_family"],
        unique=False,
    )
    op.create_index("ix_blind_challenge_runs_state", "blind_challenge_runs", ["state"], unique=False)


def downgrade() -> None:
    op.drop_table("blind_challenge_runs")
