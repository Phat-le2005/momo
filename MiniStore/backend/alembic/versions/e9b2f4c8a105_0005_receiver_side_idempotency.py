"""0005_receiver_side_idempotency

Revision ID: e9b2f4c8a105
Revises: d7a3c9e1f204
Create Date: 2026-11-25 18:22:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e9b2f4c8a105"
down_revision = "d7a3c9e1f204"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "received_recovery_commands",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "merchant_id",
            sa.String(length=64),
            sa.ForeignKey("merchants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "commerce_session_id",
            sa.String(length=64),
            sa.ForeignKey("commerce_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("world_epoch", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("command", sa.String(length=64), nullable=False),
        sa.Column("target_order_id", sa.String(length=64), nullable=True),
        sa.Column("target_payment_id", sa.String(length=64), nullable=True),
        sa.Column("target_event_id", sa.String(length=128), nullable=True),
        sa.Column("expected_version", sa.Integer(), nullable=True),
        sa.Column("authorization_digest", sa.String(length=128), nullable=True),
        sa.Column("credential_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="EXECUTING"),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "merchant_id",
            "commerce_session_id",
            "idempotency_key",
            name="uq_received_recovery_cmd_scope_idemp",
        ),
    )
    op.create_index(
        "ix_received_recovery_commands_merchant_id",
        "received_recovery_commands",
        ["merchant_id"],
    )
    op.create_index(
        "ix_received_recovery_commands_commerce_session_id",
        "received_recovery_commands",
        ["commerce_session_id"],
    )
    op.create_index(
        "ix_received_recovery_cmd_status",
        "received_recovery_commands",
        ["merchant_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_received_recovery_cmd_status", table_name="received_recovery_commands")
    op.drop_index(
        "ix_received_recovery_commands_commerce_session_id",
        table_name="received_recovery_commands",
    )
    op.drop_index(
        "ix_received_recovery_commands_merchant_id",
        table_name="received_recovery_commands",
    )
    op.drop_table("received_recovery_commands")
