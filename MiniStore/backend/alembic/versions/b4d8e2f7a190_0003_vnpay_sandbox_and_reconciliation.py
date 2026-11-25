"""0003_vnpay_sandbox_and_reconciliation

Revision ID: b4d8e2f7a190
Revises: 9c4f21a8e310
Create Date: 2026-11-25 13:50:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b4d8e2f7a190"
down_revision = "9c4f21a8e310"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Extend payments with external provider provenance & observation metadata
    op.add_column(
        "payments",
        sa.Column(
            "provider_environment",
            sa.String(length=32),
            nullable=False,
            server_default="LOCAL_MOCK",
        ),
    )
    op.add_column("payments", sa.Column("provider_txn_no", sa.String(length=64), nullable=True))
    op.add_column("payments", sa.Column("provider_txn_date", sa.String(length=20), nullable=True))
    op.add_column(
        "payments", sa.Column("provider_response_code", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "payments", sa.Column("provider_transaction_status", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "payments",
        sa.Column(
            "observation_source",
            sa.String(length=32),
            nullable=False,
            server_default="INTERNAL_LEDGER",
        ),
    )
    op.add_column(
        "payments",
        sa.Column(
            "signature_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "payments", sa.Column("last_observed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("payments", sa.Column("redirect_url", sa.Text(), nullable=True))
    op.create_index("ix_payments_provider_txn_no", "payments", ["provider_txn_no"])

    # 2. Extend refund_transactions with provider request & status codes
    op.add_column(
        "refund_transactions",
        sa.Column("provider_request_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "refund_transactions",
        sa.Column("provider_response_code", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "refund_transactions",
        sa.Column("provider_transaction_status", sa.String(length=16), nullable=True),
    )
    op.create_index(
        "ix_refund_transactions_provider_request_id",
        "refund_transactions",
        ["provider_request_id"],
    )

    # 3. QueryDr durable attempt history
    op.create_table(
        "payment_query_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=32), nullable=False, server_default="querydr"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("provider_response_code", sa.String(length=16), nullable=True),
        sa.Column("provider_transaction_status", sa.String(length=16), nullable=True),
        sa.Column("signature_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mapped_payment_status", sa.String(length=32), nullable=True),
        sa.Column(
            "observation_provenance",
            sa.String(length=32),
            nullable=False,
            server_default="VNPAY_SANDBOX",
        ),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_payment_query_attempts_payment_id", "payment_query_attempts", ["payment_id"]
    )
    op.create_index("ix_payment_query_attempts_order_id", "payment_query_attempts", ["order_id"])
    op.create_index(
        "ix_payment_query_attempts_merchant_id", "payment_query_attempts", ["merchant_id"]
    )
    op.create_index(
        "ix_payment_query_attempts_request_id",
        "payment_query_attempts",
        ["request_id"],
        unique=True,
    )
    op.create_index(
        "ix_payment_query_attempts_payment_created",
        "payment_query_attempts",
        ["payment_id", "created_at"],
    )

    # 4. Evidence-based reconciliation audit trail
    op.create_table(
        "reconciliation_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.String(length=64), nullable=True),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column(
            "verified_postconditions", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reconciliation_records_order_id", "reconciliation_records", ["order_id"])
    op.create_index(
        "ix_reconciliation_records_payment_id", "reconciliation_records", ["payment_id"]
    )
    op.create_index(
        "ix_reconciliation_records_merchant_id", "reconciliation_records", ["merchant_id"]
    )
    op.create_index(
        "ix_reconciliation_records_order_created",
        "reconciliation_records",
        ["order_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("reconciliation_records")
    op.drop_table("payment_query_attempts")

    op.drop_index("ix_refund_transactions_provider_request_id", table_name="refund_transactions")
    op.drop_column("refund_transactions", "provider_transaction_status")
    op.drop_column("refund_transactions", "provider_response_code")
    op.drop_column("refund_transactions", "provider_request_id")

    op.drop_index("ix_payments_provider_txn_no", table_name="payments")
    op.drop_column("payments", "redirect_url")
    op.drop_column("payments", "last_observed_at")
    op.drop_column("payments", "signature_verified")
    op.drop_column("payments", "observation_source")
    op.drop_column("payments", "provider_transaction_status")
    op.drop_column("payments", "provider_response_code")
    op.drop_column("payments", "provider_txn_date")
    op.drop_column("payments", "provider_txn_no")
    op.drop_column("payments", "provider_environment")
