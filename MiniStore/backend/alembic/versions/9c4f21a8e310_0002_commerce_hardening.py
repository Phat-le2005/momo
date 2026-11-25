"""0002_commerce_hardening

Revision ID: 9c4f21a8e310
Revises: 5aa07fbdedb4
Create Date: 2026-11-25 13:10:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "9c4f21a8e310"
down_revision = "5aa07fbdedb4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Customer session expiry & revocation
    op.add_column(
        "customers",
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "customers",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 2. Merchant accounts & revocable merchant sessions
    op.create_table(
        "merchant_accounts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="MERCHANT_OWNER"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_merchant_accounts_merchant_id", "merchant_accounts", ["merchant_id"])
    op.create_index("ix_merchant_accounts_username", "merchant_accounts", ["username"], unique=True)

    op.create_table(
        "merchant_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("merchant_account_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_session_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False, server_default="MERCHANT_OWNER"),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["merchant_account_id"], ["merchant_accounts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["commerce_session_id"], ["commerce_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_merchant_sessions_merchant_account_id", "merchant_sessions", ["merchant_account_id"]
    )
    op.create_index("ix_merchant_sessions_merchant_id", "merchant_sessions", ["merchant_id"])
    op.create_index(
        "ix_merchant_sessions_token_hash", "merchant_sessions", ["token_hash"], unique=True
    )
    op.create_index(
        "ix_merchant_sessions_merchant_active",
        "merchant_sessions",
        ["merchant_id", "revoked_at"],
    )

    # 3. Scoped service credentials
    op.create_table(
        "service_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("service_name", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("commerce_session_id", sa.String(length=64), nullable=False),
        sa.Column("allowed_scopes_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["merchant_id"], ["merchants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["commerce_session_id"], ["commerce_sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_service_credentials_key_hash", "service_credentials", ["key_hash"], unique=True)
    op.create_index("ix_service_credentials_merchant_id", "service_credentials", ["merchant_id"])

    # 4. Durable provider notification inbox
    op.create_table(
        "provider_notifications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("notification_id", sa.String(length=128), nullable=False),
        sa.Column("payment_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("payload_hash", sa.String(length=64), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "notification_id",
            name="uq_provider_notifications_provider_nid",
        ),
    )
    op.create_index(
        "ix_provider_notifications_payment_id", "provider_notifications", ["payment_id"]
    )
    op.create_index("ix_provider_notifications_order_id", "provider_notifications", ["order_id"])
    op.create_index(
        "ix_provider_notifications_merchant_id", "provider_notifications", ["merchant_id"]
    )

    # 5. InventoryReservation.live_product_id & LiveProduct.flash_sale_reserved_qty
    op.add_column(
        "inventory_reservations",
        sa.Column("live_product_id", sa.String(length=64), nullable=True),
    )
    op.create_foreign_key(
        "fk_inventory_reservations_live_product_id",
        "inventory_reservations",
        "live_products",
        ["live_product_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "live_products",
        sa.Column(
            "flash_sale_reserved_qty",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_check_constraint(
        "ck_live_products_flash_reserved_nonneg",
        "live_products",
        "flash_sale_reserved_qty >= 0",
    )

    # 6. Order fingerprint & reconciliation status
    op.add_column(
        "orders",
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "orders",
        sa.Column(
            "reconciliation_status",
            sa.String(length=32),
            nullable=False,
            server_default="NONE",
        ),
    )
    op.add_column(
        "orders",
        sa.Column("reconciliation_reason", sa.String(length=500), nullable=True),
    )
    op.create_index("ix_orders_reconciliation_status", "orders", ["reconciliation_status"])

    # 7. Payment reconciliation flags
    op.add_column(
        "payments",
        sa.Column(
            "reconciliation_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "payments",
        sa.Column("reconciliation_note", sa.String(length=500), nullable=True),
    )
    op.create_index("ix_payments_reconciliation_required", "payments", ["reconciliation_required"])

    # 8. Outbox two-phase lease & failure classification columns
    op.add_column(
        "event_outbox",
        sa.Column("failure_category", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "event_outbox",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "event_outbox",
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "event_outbox",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "event_outbox",
        sa.Column("target_finresolve_world_epoch", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_outbox", "target_finresolve_world_epoch")
    op.drop_column("event_outbox", "locked_until")
    op.drop_column("event_outbox", "claimed_by")
    op.drop_column("event_outbox", "claim_token")
    op.drop_column("event_outbox", "failure_category")

    op.drop_index("ix_payments_reconciliation_required", table_name="payments")
    op.drop_column("payments", "reconciliation_note")
    op.drop_column("payments", "reconciliation_required")

    op.drop_index("ix_orders_reconciliation_status", table_name="orders")
    op.drop_column("orders", "reconciliation_reason")
    op.drop_column("orders", "reconciliation_status")
    op.drop_column("orders", "request_fingerprint")

    op.drop_constraint("ck_live_products_flash_reserved_nonneg", "live_products", type_="check")
    op.drop_column("live_products", "flash_sale_reserved_qty")
    op.drop_constraint(
        "fk_inventory_reservations_live_product_id",
        "inventory_reservations",
        type_="foreignkey",
    )
    op.drop_column("inventory_reservations", "live_product_id")

    op.drop_table("provider_notifications")
    op.drop_table("service_credentials")
    op.drop_table("merchant_sessions")
    op.drop_table("merchant_accounts")

    op.drop_column("customers", "revoked_at")
    op.drop_column("customers", "token_expires_at")
