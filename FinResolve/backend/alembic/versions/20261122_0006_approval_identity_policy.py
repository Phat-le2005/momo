"""0006_approval_identity_policy: add approver_id and policy_version to approvals

Revision ID: 0006_approval_identity_policy
Revises: 0005_proposal_case_id_nullable
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_approval_identity_policy"
down_revision: str | None = "0005_proposal_case_id_nullable"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column(
            "approver_id",
            sa.String(length=64),
            nullable=False,
            server_default="demo_manager",
        ),
    )
    op.add_column(
        "approvals",
        sa.Column(
            "policy_version",
            sa.String(length=64),
            nullable=False,
            server_default="FinResolve-Policy-v1.2",
        ),
    )


def downgrade() -> None:
    op.drop_column("approvals", "policy_version")
    op.drop_column("approvals", "approver_id")
