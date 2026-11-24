"""0002_domain_schema: create tables for sessions, cases, evidence, actions, risks, and simulator

Revision ID: 0002_domain_schema
Revises: 0001_baseline
Create Date: 2026-11-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_domain_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. demo_sessions
    op.create_table(
        "demo_sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("token", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("max_cases", sa.Integer(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_demo_sessions")),
    )
    op.create_index(op.f("ix_demo_sessions_token"), "demo_sessions", ["token"], unique=True)
    op.create_index(op.f("ix_demo_sessions_expires_at"), "demo_sessions", ["expires_at"], unique=False)

    # 2. cases
    op.create_table(
        "cases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("goal", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_cases_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
    )
    op.create_index(op.f("ix_cases_session_id"), "cases", ["session_id"], unique=False)
    op.create_index(op.f("ix_cases_state"), "cases", ["state"], unique=False)
    op.create_index(op.f("ix_cases_idempotency_key"), "cases", ["idempotency_key"], unique=False)
    op.create_index(
        "ix_cases_session_idempotency",
        "cases",
        ["session_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # 3. evidence
    op.create_table(
        "evidence",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_ref", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_evidence_case_id_cases"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_evidence_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
    )
    op.create_index(op.f("ix_evidence_case_id"), "evidence", ["case_id"], unique=False)
    op.create_index(op.f("ix_evidence_session_id"), "evidence", ["session_id"], unique=False)

    # 4. case_events
    op.create_table(
        "case_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], name=op.f("fk_case_events_case_id_cases"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_case_events_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_events")),
    )
    op.create_index(op.f("ix_case_events_case_id"), "case_events", ["case_id"], unique=False)
    op.create_index(op.f("ix_case_events_session_id"), "case_events", ["session_id"], unique=False)

    # 5. action_proposals
    op.create_table(
        "action_proposals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], name=op.f("fk_action_proposals_case_id_cases"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["demo_sessions.id"],
            name=op.f("fk_action_proposals_session_id_demo_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_proposals")),
    )
    op.create_index(op.f("ix_action_proposals_case_id"), "action_proposals", ["case_id"], unique=False)
    op.create_index(op.f("ix_action_proposals_session_id"), "action_proposals", ["session_id"], unique=False)

    # 6. approvals
    op.create_table(
        "approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("approver_role", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("digest", sa.String(length=128), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"], ["action_proposals.id"], name=op.f("fk_approvals_proposal_id_action_proposals"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approvals")),
    )
    op.create_index(op.f("ix_approvals_proposal_id"), "approvals", ["proposal_id"], unique=False)

    # 7. action_executions
    op.create_table(
        "action_executions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("proposal_id", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["proposal_id"],
            ["action_proposals.id"],
            name=op.f("fk_action_executions_proposal_id_action_proposals"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_action_executions")),
    )
    op.create_index(op.f("ix_action_executions_proposal_id"), "action_executions", ["proposal_id"], unique=False)
    op.create_index(op.f("ix_action_executions_idempotency_key"), "action_executions", ["idempotency_key"], unique=True)

    # 8. verifications
    op.create_table(
        "verifications",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("action_execution_id", sa.String(length=64), nullable=True),
        sa.Column("postconditions_json", sa.JSON(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], name=op.f("fk_verifications_case_id_cases"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["action_execution_id"],
            ["action_executions.id"],
            name=op.f("fk_verifications_action_execution_id_action_executions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_verifications")),
    )
    op.create_index(op.f("ix_verifications_case_id"), "verifications", ["case_id"], unique=False)
    op.create_index(op.f("ix_verifications_action_execution_id"), "verifications", ["action_execution_id"], unique=False)

    # 9. risk_signals
    op.create_table(
        "risk_signals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("level", sa.String(length=32), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("response", sa.String(length=32), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"], ["cases.id"], name=op.f("fk_risk_signals_case_id_cases"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_risk_signals_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_signals")),
    )
    op.create_index(op.f("ix_risk_signals_case_id"), "risk_signals", ["case_id"], unique=False)
    op.create_index(op.f("ix_risk_signals_session_id"), "risk_signals", ["session_id"], unique=False)

    # 10. sim_orders
    op.create_table(
        "sim_orders",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("customer_name", sa.String(length=128), nullable=False),
        sa.Column("customer_phone", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_sim_orders_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sim_orders")),
    )
    op.create_index(op.f("ix_sim_orders_session_id"), "sim_orders", ["session_id"], unique=False)

    # 11. sim_payments
    op.create_table(
        "sim_payments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("provider_ref", sa.String(length=128), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_sim_payments_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sim_payments")),
    )
    op.create_index(op.f("ix_sim_payments_session_id"), "sim_payments", ["session_id"], unique=False)
    op.create_index(op.f("ix_sim_payments_order_id"), "sim_payments", ["order_id"], unique=False)
    op.create_index(op.f("ix_sim_payments_provider_ref"), "sim_payments", ["provider_ref"], unique=False)

    # 12. sim_refunds
    op.create_table(
        "sim_refunds",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("payment_id", sa.String(length=64), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("source_mode", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["demo_sessions.id"], name=op.f("fk_sim_refunds_session_id_demo_sessions"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sim_refunds")),
    )
    op.create_index(op.f("ix_sim_refunds_session_id"), "sim_refunds", ["session_id"], unique=False)
    op.create_index(op.f("ix_sim_refunds_order_id"), "sim_refunds", ["order_id"], unique=False)
    op.create_index(op.f("ix_sim_refunds_payment_id"), "sim_refunds", ["payment_id"], unique=False)


def downgrade() -> None:
    op.drop_table("sim_refunds")
    op.drop_table("sim_payments")
    op.drop_table("sim_orders")
    op.drop_table("risk_signals")
    op.drop_table("verifications")
    op.drop_table("action_executions")
    op.drop_table("approvals")
    op.drop_table("action_proposals")
    op.drop_table("case_events")
    op.drop_table("evidence")
    op.drop_table("cases")
    op.drop_table("demo_sessions")
