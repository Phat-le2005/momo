"""0013_monitor_hardening: connector registry, pagination checkpoints, scan coverage, recurrence

Revision ID: 0013_monitor_hardening
Revises: 0012_continuous_monitor
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_monitor_hardening"
down_revision: str | None = "0012_continuous_monitor"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.add_column(
        "commerce_source_bindings",
        sa.Column(
            "connector_id",
            sa.String(length=64),
            server_default="default_ministore",
            nullable=False,
        ),
    )
    op.add_column(
        "commerce_source_bindings",
        sa.Column(
            "world_reset_required",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "commerce_source_bindings",
        sa.Column(
            "events_scan_complete",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
    )
    op.add_column(
        "commerce_source_bindings",
        sa.Column("last_orders_cursor", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "commerce_source_bindings",
        sa.Column("last_payments_cursor", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "commerce_source_bindings",
        sa.Column(
            "last_scan_coverage_json",
            sa.JSON(),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "anomaly_observations",
        sa.Column("occurrence_count", sa.Integer(), server_default="1", nullable=False),
    )
    op.add_column(
        "anomaly_observations",
        sa.Column("verification_verdict", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("anomaly_observations", "verification_verdict")
    op.drop_column("anomaly_observations", "occurrence_count")
    op.drop_column("commerce_source_bindings", "last_scan_coverage_json")
    op.drop_column("commerce_source_bindings", "last_payments_cursor")
    op.drop_column("commerce_source_bindings", "last_orders_cursor")
    op.drop_column("commerce_source_bindings", "events_scan_complete")
    op.drop_column("commerce_source_bindings", "world_reset_required")
    op.drop_column("commerce_source_bindings", "connector_id")
