"""0004_payment_safety_hardening

Revision ID: d7a3c9e1f204
Revises: b4d8e2f7a190
Create Date: 2026-11-25 14:35:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7a3c9e1f204"
down_revision = "b4d8e2f7a190"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # P1.4: Separate immutable provider_create_date from provider_pay_date
    op.add_column("payments", sa.Column("provider_create_date", sa.String(length=20), nullable=True))
    op.add_column("payments", sa.Column("provider_pay_date", sa.String(length=20), nullable=True))
    # Backfill existing VNPAY rows' provider_create_date conservatively from provider_txn_date
    op.execute(
        "UPDATE payments SET provider_create_date = provider_txn_date "
        "WHERE provider = 'VNPAY_SANDBOX' AND provider_txn_date IS NOT NULL "
        "AND provider_create_date IS NULL"
    )

    # P2.1 & P2.2: Durable QueryDr job claiming, lease fencing, backoff, and starvation prevention
    op.add_column("payments", sa.Column("querydr_claim_token", sa.String(length=64), nullable=True))
    op.add_column("payments", sa.Column("querydr_claimed_by", sa.String(length=64), nullable=True))
    op.add_column(
        "payments",
        sa.Column("querydr_locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("querydr_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("querydr_exhausted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_payments_querydr_next_attempt_at", "payments", ["querydr_next_attempt_at"])
    op.create_index("ix_payments_querydr_exhausted", "payments", ["querydr_exhausted"])


def downgrade() -> None:
    op.drop_index("ix_payments_querydr_exhausted", table_name="payments")
    op.drop_index("ix_payments_querydr_next_attempt_at", table_name="payments")
    op.drop_column("payments", "querydr_exhausted")
    op.drop_column("payments", "querydr_next_attempt_at")
    op.drop_column("payments", "querydr_locked_until")
    op.drop_column("payments", "querydr_claimed_by")
    op.drop_column("payments", "querydr_claim_token")
    op.drop_column("payments", "provider_pay_date")
    op.drop_column("payments", "provider_create_date")
