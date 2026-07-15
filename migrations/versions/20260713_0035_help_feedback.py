"""help feedback:說明中心回饋落庫(DB 為主、email 降盡力通知)

Revision ID: 0035_help_feedback
Revises: 0034_telegram_bridge
Create Date: 2026-07-13

續-16(Jordan 拍板):`/app/help/feedback` 原本只寄 email,email 曾送達延遲 → 改 DB 為主、
email 盡力通知。回饋顯示在 `/admin/proposals` 同頁獨立區。

- `help_feedback`:`id` PK autoincrement;`user_id` FK→user_account.user_id(誰留的);
  `message` 全文;`resolved_at` / `resolved_by` nullable(admin 標記已處理即填)。audit 欄。

downgrade = drop 表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_help_feedback"
down_revision: str | None = "0034_telegram_bridge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _audit_columns() -> list[sa.Column]:
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
        "help_feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("help_feedback")
