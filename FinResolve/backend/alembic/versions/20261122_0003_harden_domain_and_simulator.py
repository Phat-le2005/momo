"""0003_harden_domain_and_simulator: harden domain schema and simulator commerce/inventory entities

Revision ID: 0003_harden_domain_and_simulator
Revises: 0002_domain_schema
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_harden_domain_and_simulator"
down_revision: str | None = "0002_domain_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Alter cases: add idempotency_payload_hash
    op.add_column("cases", sa.Column("idempotency_payload_hash", sa.String(length=64), nullable=True))

    # 2. Alter verifications: add case_version
    op.add_column("verifications", sa.Column("case_version", sa.Integer(), nullable=False, server_default="1"))

    # 3. Alter sim_orders: BigInteger amounts, currency, breakdowns, statuses
    op.alter_column("sim_orders", "amount", existing_type=sa.Integer(), type_=sa.BigInteger())
    op.add_column("sim_orders", sa.Column("currency", sa.String(length=3), nullable=False, server_default="VND"))
    op.add_column("sim_orders", sa.Column("subtotal", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_orders", sa.Column("discount", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_orders", sa.Column("tax", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_orders", sa.Column("shipping", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_orders", sa.Column("grand_total", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_orders", sa.Column("payment_status", sa.String(length=32), nullable=False, server_default="UNPAID"))
    op.add_column("sim_orders", sa.Column("fulfillment_status", sa.String(length=32), nullable=False, server_default="UNFULFILLED"))

    op.create_check_constraint("ck_sim_orders_amount_nonnegative", "sim_orders", "amount >= 0")
    op.create_check_constraint("ck_sim_orders_subtotal_nonnegative", "sim_orders", "subtotal >= 0")
    op.create_check_constraint("ck_sim_orders_discount_nonnegative", "sim_orders", "discount >= 0")
    op.create_check_constraint("ck_sim_orders_tax_nonnegative", "sim_orders", "tax >= 0")
    op.create_check_constraint("ck_sim_orders_shipping_nonnegative", "sim_orders", "shipping >= 0")
    op.create_check_constraint("ck_sim_orders_grand_total_nonnegative", "sim_orders", "grand_total >= 0")
    op.create_check_constraint("ck_sim_orders_currency", "sim_orders", "currency IN ('VND', 'USD')")

    # 4. Alter sim_payments: BigInteger, nullable order_id, merchant, request_id, etc.
    op.alter_column("sim_payments", "amount", existing_type=sa.Integer(), type_=sa.BigInteger())
    op.alter_column("sim_payments", "order_id", existing_type=sa.String(length=64), nullable=True)
    op.add_column("sim_payments", sa.Column("merchant_id", sa.String(length=64), nullable=False, server_default="MERCHANT_MOCK_01"))
    op.add_column("sim_payments", sa.Column("provider_order_id", sa.String(length=128), nullable=True))
    op.add_column("sim_payments", sa.Column("provider_transaction_id", sa.String(length=128), nullable=True))
    op.add_column("sim_payments", sa.Column("request_id", sa.String(length=128), nullable=True))
    op.add_column("sim_payments", sa.Column("result_code", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("sim_payments", sa.Column("currency", sa.String(length=3), nullable=False, server_default="VND"))
    op.add_column("sim_payments", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")))
    op.add_column("sim_payments", sa.Column("source_event_time", sa.DateTime(timezone=True), nullable=True))
    op.add_column("sim_payments", sa.Column("verification_source", sa.String(length=64), nullable=False, server_default="PROVIDER_QUERY"))

    op.create_index("ix_sim_payments_provider_order_id", "sim_payments", ["provider_order_id"], unique=False)
    op.create_index("ix_sim_payments_provider_transaction_id", "sim_payments", ["provider_transaction_id"], unique=False)
    op.create_index("ix_sim_payments_request_id", "sim_payments", ["request_id"], unique=False)
    op.create_index(
        "ix_sim_payments_session_provider_ref",
        "sim_payments",
        ["session_id", "provider", "provider_ref"],
        unique=True,
        postgresql_where=sa.text("provider_ref IS NOT NULL"),
    )
    op.create_index(
        "ix_sim_payments_session_provider_request",
        "sim_payments",
        ["session_id", "provider", "request_id"],
        unique=True,
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )
    op.create_check_constraint("ck_sim_payments_amount_nonnegative", "sim_payments", "amount >= 0")
    op.create_check_constraint("ck_sim_payments_currency", "sim_payments", "currency IN ('VND', 'USD')")

    # 5. Alter sim_refunds: BigInteger, partial refunds, approval ref, request_id
    op.alter_column("sim_refunds", "amount", existing_type=sa.Integer(), type_=sa.BigInteger())
    op.alter_column("sim_refunds", "order_id", existing_type=sa.String(length=64), nullable=True)
    op.add_column("sim_refunds", sa.Column("requested_amount", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_refunds", sa.Column("approved_amount", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_refunds", sa.Column("completed_amount", sa.BigInteger(), nullable=False, server_default="0"))
    op.add_column("sim_refunds", sa.Column("currency", sa.String(length=3), nullable=False, server_default="VND"))
    op.add_column("sim_refunds", sa.Column("request_id", sa.String(length=128), nullable=True))
    op.add_column("sim_refunds", sa.Column("approval_ref", sa.String(length=128), nullable=True))
    op.add_column("sim_refunds", sa.Column("provider_refund_id", sa.String(length=128), nullable=True))

    op.create_index("ix_sim_refunds_request_id", "sim_refunds", ["request_id"], unique=False)
    op.create_index(
        "ix_sim_refunds_session_request",
        "sim_refunds",
        ["session_id", "request_id"],
        unique=True,
        postgresql_where=sa.text("request_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_sim_refunds_payment_id_sim_payments"),
        "sim_refunds",
        "sim_payments",
        ["payment_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint("ck_sim_refunds_amount_nonnegative", "sim_refunds", "amount >= 0")
    op.create_check_constraint("ck_sim_refunds_requested_amount_nonnegative", "sim_refunds", "requested_amount >= 0")
    op.create_check_constraint("ck_sim_refunds_approved_amount_nonnegative", "sim_refunds", "approved_amount >= 0")
    op.create_check_constraint("ck_sim_refunds_completed_amount_nonnegative", "sim_refunds", "completed_amount >= 0")
    op.create_check_constraint("ck_sim_refunds_currency", "sim_refunds", "currency IN ('VND', 'USD')")

    # 6. Create products table
    op.create_table(
        "products",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("price", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="VND"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("source_mode", sa.String(length=32), nullable=False, server_default="MOCK"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("price >= 0", name=op.f("ck_products_price_nonnegative")),
        sa.CheckConstraint("currency IN ('VND', 'USD')", name=op.f("ck_products_currency")),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_products_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
    )
    op.create_index(op.f("ix_products_session_id"), "products", ["session_id"], unique=False)
    op.create_index(op.f("ix_products_sku"), "products", ["sku"], unique=False)
    op.create_index("ix_products_session_sku", "products", ["session_id", "sku"], unique=True)

    # 7. Create warehouses table
    op.create_table(
        "warehouses",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False, server_default="MOCK"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_warehouses_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_warehouses")),
    )
    op.create_index(op.f("ix_warehouses_session_id"), "warehouses", ["session_id"], unique=False)
    op.create_index(op.f("ix_warehouses_code"), "warehouses", ["code"], unique=False)
    op.create_index("ix_warehouses_session_code", "warehouses", ["session_id", "code"], unique=True)

    # 8. Create inventory_levels table
    op.create_table(
        "inventory_levels",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("warehouse_id", sa.String(length=64), nullable=False),
        sa.Column("on_hand", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("source_mode", sa.String(length=32), nullable=False, server_default="MOCK"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("on_hand >= 0", name=op.f("ck_inventory_levels_on_hand_nonnegative")),
        sa.CheckConstraint("reserved >= 0", name=op.f("ck_inventory_levels_reserved_nonnegative")),
        sa.CheckConstraint("on_hand >= reserved", name=op.f("ck_inventory_levels_available")),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_inventory_levels_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_inventory_levels_product_id_products"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name=op.f("fk_inventory_levels_warehouse_id_warehouses"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_levels")),
    )
    op.create_index(op.f("ix_inventory_levels_session_id"), "inventory_levels", ["session_id"], unique=False)
    op.create_index(op.f("ix_inventory_levels_product_id"), "inventory_levels", ["product_id"], unique=False)
    op.create_index(op.f("ix_inventory_levels_warehouse_id"), "inventory_levels", ["warehouse_id"], unique=False)
    op.create_index("ix_inventory_levels_product_warehouse", "inventory_levels", ["session_id", "product_id", "warehouse_id"], unique=True)

    # 9. Create order_items table
    op.create_table(
        "order_items",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("sku_snapshot", sa.String(length=64), nullable=False),
        sa.Column("product_name_snapshot", sa.String(length=256), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price", sa.BigInteger(), nullable=False),
        sa.Column("discount", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("tax", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("line_total", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False, server_default="VND"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_order_items_quantity_positive")),
        sa.CheckConstraint("unit_price >= 0", name=op.f("ck_order_items_unit_price_nonnegative")),
        sa.CheckConstraint("discount >= 0", name=op.f("ck_order_items_discount_nonnegative")),
        sa.CheckConstraint("tax >= 0", name=op.f("ck_order_items_tax_nonnegative")),
        sa.CheckConstraint("line_total >= 0", name=op.f("ck_order_items_line_total_nonnegative")),
        sa.CheckConstraint("currency IN ('VND', 'USD')", name=op.f("ck_order_items_currency")),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_order_items_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["sim_orders.id"], name=op.f("fk_order_items_order_id_sim_orders"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_order_items_product_id_products"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_order_items")),
    )
    op.create_index(op.f("ix_order_items_session_id"), "order_items", ["session_id"], unique=False)
    op.create_index(op.f("ix_order_items_order_id"), "order_items", ["order_id"], unique=False)
    op.create_index(op.f("ix_order_items_product_id"), "order_items", ["product_id"], unique=False)

    # 10. Create inventory_reservations table
    op.create_table(
        "inventory_reservations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column("order_item_id", sa.String(length=64), nullable=True),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("warehouse_id", sa.String(length=64), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_mode", sa.String(length=32), nullable=False, server_default="MOCK"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_inventory_reservations_quantity_positive")),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_inventory_reservations_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_id"], ["sim_orders.id"], name=op.f("fk_inventory_reservations_order_id_sim_orders"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["order_item_id"], ["order_items.id"], name=op.f("fk_inventory_reservations_order_item_id_order_items"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_inventory_reservations_product_id_products"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name=op.f("fk_inventory_reservations_warehouse_id_warehouses"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_reservations")),
    )
    op.create_index(op.f("ix_inventory_reservations_session_id"), "inventory_reservations", ["session_id"], unique=False)
    op.create_index(op.f("ix_inventory_reservations_order_id"), "inventory_reservations", ["order_id"], unique=False)
    op.create_index(op.f("ix_inventory_reservations_order_item_id"), "inventory_reservations", ["order_item_id"], unique=False)
    op.create_index(op.f("ix_inventory_reservations_product_id"), "inventory_reservations", ["product_id"], unique=False)
    op.create_index(op.f("ix_inventory_reservations_warehouse_id"), "inventory_reservations", ["warehouse_id"], unique=False)

    # 11. Create inventory_movements table
    op.create_table(
        "inventory_movements",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("product_id", sa.String(length=64), nullable=False),
        sa.Column("warehouse_id", sa.String(length=64), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False, server_default="MOCK"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("delta != 0", name=op.f("ck_inventory_movements_delta_nonzero")),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_inventory_movements_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["product_id"], ["products.id"], name=op.f("fk_inventory_movements_product_id_products"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["warehouse_id"], ["warehouses.id"], name=op.f("fk_inventory_movements_warehouse_id_warehouses"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inventory_movements")),
    )
    op.create_index(op.f("ix_inventory_movements_session_id"), "inventory_movements", ["session_id"], unique=False)
    op.create_index(op.f("ix_inventory_movements_product_id"), "inventory_movements", ["product_id"], unique=False)
    op.create_index(op.f("ix_inventory_movements_warehouse_id"), "inventory_movements", ["warehouse_id"], unique=False)

    # 12. Create sim_payment_events table
    op.create_table(
        "sim_payment_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.String(length=64), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False, server_default="MOCK"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], name=op.f("fk_sim_payment_events_session_id_demo_sessions"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["payment_id"], ["sim_payments.id"], name=op.f("fk_sim_payment_events_payment_id_sim_payments"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sim_payment_events")),
    )
    op.create_index(op.f("ix_sim_payment_events_session_id"), "sim_payment_events", ["session_id"], unique=False)
    op.create_index(op.f("ix_sim_payment_events_payment_id"), "sim_payment_events", ["payment_id"], unique=False)
    op.create_index(op.f("ix_sim_payment_events_request_id"), "sim_payment_events", ["request_id"], unique=False)


def downgrade() -> None:
    op.drop_table("sim_payment_events")
    op.drop_table("inventory_movements")
    op.drop_table("inventory_reservations")
    op.drop_table("order_items")
    op.drop_table("inventory_levels")
    op.drop_table("warehouses")
    op.drop_table("products")

    op.drop_constraint("ck_sim_refunds_currency", "sim_refunds", type_="check")
    op.drop_constraint("ck_sim_refunds_completed_amount_nonnegative", "sim_refunds", type_="check")
    op.drop_constraint("ck_sim_refunds_approved_amount_nonnegative", "sim_refunds", type_="check")
    op.drop_constraint("ck_sim_refunds_requested_amount_nonnegative", "sim_refunds", type_="check")
    op.drop_constraint("ck_sim_refunds_amount_nonnegative", "sim_refunds", type_="check")
    op.drop_constraint("fk_sim_refunds_payment_id_sim_payments", "sim_refunds", type_="foreignkey")
    op.drop_index("ix_sim_refunds_session_request", table_name="sim_refunds")
    op.drop_index("ix_sim_refunds_request_id", table_name="sim_refunds")
    op.drop_column("sim_refunds", "provider_refund_id")
    op.drop_column("sim_refunds", "approval_ref")
    op.drop_column("sim_refunds", "request_id")
    op.drop_column("sim_refunds", "currency")
    op.drop_column("sim_refunds", "completed_amount")
    op.drop_column("sim_refunds", "approved_amount")
    op.drop_column("sim_refunds", "requested_amount")
    op.alter_column("sim_refunds", "order_id", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("sim_refunds", "amount", existing_type=sa.BigInteger(), type_=sa.Integer())

    op.drop_constraint("ck_sim_payments_currency", "sim_payments", type_="check")
    op.drop_constraint("ck_sim_payments_amount_nonnegative", "sim_payments", type_="check")
    op.drop_index("ix_sim_payments_session_provider_request", table_name="sim_payments")
    op.drop_index("ix_sim_payments_session_provider_ref", table_name="sim_payments")
    op.drop_index("ix_sim_payments_request_id", table_name="sim_payments")
    op.drop_index("ix_sim_payments_provider_transaction_id", table_name="sim_payments")
    op.drop_index("ix_sim_payments_provider_order_id", table_name="sim_payments")
    op.drop_column("sim_payments", "verification_source")
    op.drop_column("sim_payments", "source_event_time")
    op.drop_column("sim_payments", "observed_at")
    op.drop_column("sim_payments", "currency")
    op.drop_column("sim_payments", "result_code")
    op.drop_column("sim_payments", "request_id")
    op.drop_column("sim_payments", "provider_transaction_id")
    op.drop_column("sim_payments", "provider_order_id")
    op.drop_column("sim_payments", "merchant_id")
    op.alter_column("sim_payments", "order_id", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("sim_payments", "amount", existing_type=sa.BigInteger(), type_=sa.Integer())

    op.drop_constraint("ck_sim_orders_currency", "sim_orders", type_="check")
    op.drop_constraint("ck_sim_orders_grand_total_nonnegative", "sim_orders", type_="check")
    op.drop_constraint("ck_sim_orders_shipping_nonnegative", "sim_orders", type_="check")
    op.drop_constraint("ck_sim_orders_tax_nonnegative", "sim_orders", type_="check")
    op.drop_constraint("ck_sim_orders_discount_nonnegative", "sim_orders", type_="check")
    op.drop_constraint("ck_sim_orders_subtotal_nonnegative", "sim_orders", type_="check")
    op.drop_constraint("ck_sim_orders_amount_nonnegative", "sim_orders", type_="check")
    op.drop_column("sim_orders", "fulfillment_status")
    op.drop_column("sim_orders", "payment_status")
    op.drop_column("sim_orders", "grand_total")
    op.drop_column("sim_orders", "shipping")
    op.drop_column("sim_orders", "tax")
    op.drop_column("sim_orders", "discount")
    op.drop_column("sim_orders", "subtotal")
    op.drop_column("sim_orders", "currency")
    op.alter_column("sim_orders", "amount", existing_type=sa.BigInteger(), type_=sa.Integer())

    op.drop_column("verifications", "case_version")
    op.drop_column("cases", "idempotency_payload_hash")
