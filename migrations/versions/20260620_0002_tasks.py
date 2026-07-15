"""task slice #2: task reference master

Revision ID: 0002_tasks
Revises: 0001_assets
Create Date: 2026-06-20

對應 docs/domain-model/05-tasks.md §8。手寫(本機無 DB 可 autogenerate)。
Task 本體 2 欄 + 閒置旗標 + 稽核;TaskStep / standard_hours 不在本切片(見 05-tasks)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_tasks"
down_revision: str | None = "0001_assets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_columns() -> list[sa.Column]:
    # 與 cmms.audit.AuditMixin 對齊(ADR-005/016)
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("source_actor", sa.String(), nullable=True),
        sa.Column("proposed_by", sa.String(), nullable=True),
        sa.Column("confirmed_by", sa.String(), nullable=True),
    ]


def upgrade() -> None:
    op.create_table(
        "task",
        sa.Column("task_no", sa.String(), primary_key=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_audit_columns(),
    )


def downgrade() -> None:
    op.drop_table("task")
