"""0004_backfill_legacy_refunds_and_orders: backfill legacy refunds and order amounts

Revision ID: 0004_backfill_legacy_refunds_and_orders
Revises: 0003_harden_domain_and_simulator
Create Date: 2026-11-22
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0004_backfill_legacy_amounts"
down_revision: str | None = "0003_harden_domain_and_simulator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Backfill legacy sim_orders amounts: subtotal and grand_total from amount
    op.execute(
        """
        UPDATE sim_orders
        SET subtotal = amount, grand_total = amount
        WHERE (grand_total = 0 OR grand_total IS NULL) AND amount > 0;
        """
    )

    # 2. Backfill legacy sim_refunds amounts:
    # Completed refunds: set requested, approved, and completed amounts from amount
    op.execute(
        """
        UPDATE sim_refunds
        SET requested_amount = amount, approved_amount = amount, completed_amount = amount
        WHERE (completed_amount = 0 OR completed_amount IS NULL) AND amount > 0 AND status = 'COMPLETED';
        """
    )

    # Pending or other status refunds: backfill requested_amount from amount
    op.execute(
        """
        UPDATE sim_refunds
        SET requested_amount = amount
        WHERE (requested_amount = 0 OR requested_amount IS NULL) AND amount > 0 AND status != 'COMPLETED';
        """
    )


def downgrade() -> None:
    # Additive data backfill; no schema reversal needed
    pass
