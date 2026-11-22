"""0005_proposal_case_id_nullable: allow case_id to be nullable in action_proposals

Revision ID: 0005_proposal_case_id_nullable
Revises: 0004_backfill_legacy_amounts
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_proposal_case_id_nullable"
down_revision: str | None = "0004_backfill_legacy_amounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "action_proposals",
        "case_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.alter_column(
        "verifications",
        "case_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "verifications",
        "case_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.alter_column(
        "action_proposals",
        "case_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
