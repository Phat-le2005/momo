"""0016_proposal_provenance_and_uncertainty: exact proposal run provenance, baseline window dedupe, command uncertainty guard

Revision ID: 0016_proposal_provenance
Revises: 0015_open_world_guardian
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_proposal_provenance"
down_revision: str | None = "0015_open_world_guardian"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.add_column(
        "action_proposals",
        sa.Column("investigation_run_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_action_proposals_investigation_run_id",
        "action_proposals",
        ["investigation_run_id"],
        unique=False,
    )

    op.add_column(
        "commerce_source_bindings",
        sa.Column("last_learned_window_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "commerce_source_bindings",
        sa.Column("last_learned_window_end", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "recovery_commands",
        sa.Column(
            "execution_certainty",
            sa.String(length=32),
            server_default="UNKNOWN",
            nullable=False,
        ),
    )
    op.add_column(
        "recovery_commands",
        sa.Column("uncertainty_resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "recovery_commands",
        sa.Column("uncertainty_resolved_by", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recovery_commands", "uncertainty_resolved_by")
    op.drop_column("recovery_commands", "uncertainty_resolved_at")
    op.drop_column("recovery_commands", "execution_certainty")

    op.drop_column("commerce_source_bindings", "last_learned_window_end")
    op.drop_column("commerce_source_bindings", "last_learned_window_id")

    op.drop_index("ix_action_proposals_investigation_run_id", table_name="action_proposals")
    op.drop_column("action_proposals", "investigation_run_id")
