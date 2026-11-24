"""0007_incident_taskforce: add incidents, incident_cases, and agent_tasks tables

Revision ID: 0007_incident_taskforce
Revises: 0006_approval_identity_policy
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_incident_taskforce"
down_revision: str | None = "0006_approval_identity_policy"
branch_labels: str | Sequence[str | None] | None = None
depends_on: str | Sequence[str | None] | None = None


def upgrade() -> None:
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("hypothesis", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("correlation_key", sa.String(length=192), nullable=False),
        sa.Column("correlation_trace", sa.String(length=512), nullable=False),
        sa.Column("shared_factors_json", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_incidents_correlation_key"), "incidents", ["correlation_key"], unique=False
    )
    op.create_index(op.f("ix_incidents_session_id"), "incidents", ["session_id"], unique=False)
    op.create_index(op.f("ix_incidents_status"), "incidents", ["status"], unique=False)

    op.create_table(
        "incident_cases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=False),
        sa.Column("case_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("link_reason", sa.String(length=512), nullable=False),
        sa.Column("shared_evidence_refs", sa.JSON(), nullable=False),
        sa.Column("causal_factors_json", sa.JSON(), nullable=False),
        sa.Column("is_auto_resolved", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_incident_cases_case_id"), "incident_cases", ["case_id"], unique=False
    )
    op.create_index(
        "ix_incident_cases_incident_case",
        "incident_cases",
        ["incident_id", "case_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_incident_cases_incident_id"), "incident_cases", ["incident_id"], unique=False
    )
    op.create_index(
        op.f("ix_incident_cases_session_id"), "incident_cases", ["session_id"], unique=False
    )

    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("incident_id", sa.String(length=64), nullable=True),
        sa.Column("case_id", sa.String(length=64), nullable=True),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("objective", sa.String(length=512), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("allowed_tools", sa.JSON(), nullable=False),
        sa.Column("evidence_refs", sa.JSON(), nullable=False),
        sa.Column("handoff_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["cases.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["demo_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_tasks_case_id"), "agent_tasks", ["case_id"], unique=False)
    op.create_index(
        op.f("ix_agent_tasks_incident_id"), "agent_tasks", ["incident_id"], unique=False
    )
    op.create_index(op.f("ix_agent_tasks_role"), "agent_tasks", ["role"], unique=False)
    op.create_index(
        op.f("ix_agent_tasks_session_id"), "agent_tasks", ["session_id"], unique=False
    )
    op.create_index(op.f("ix_agent_tasks_state"), "agent_tasks", ["state"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_agent_tasks_state"), table_name="agent_tasks")
    op.drop_index(op.f("ix_agent_tasks_session_id"), table_name="agent_tasks")
    op.drop_index(op.f("ix_agent_tasks_role"), table_name="agent_tasks")
    op.drop_index(op.f("ix_agent_tasks_incident_id"), table_name="agent_tasks")
    op.drop_index(op.f("ix_agent_tasks_case_id"), table_name="agent_tasks")
    op.drop_table("agent_tasks")

    op.drop_index(op.f("ix_incident_cases_session_id"), table_name="incident_cases")
    op.drop_index(op.f("ix_incident_cases_incident_id"), table_name="incident_cases")
    op.drop_index("ix_incident_cases_incident_case", table_name="incident_cases")
    op.drop_index(op.f("ix_incident_cases_case_id"), table_name="incident_cases")
    op.drop_table("incident_cases")

    op.drop_index(op.f("ix_incidents_status"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_session_id"), table_name="incidents")
    op.drop_index(op.f("ix_incidents_correlation_key"), table_name="incidents")
    op.drop_table("incidents")
