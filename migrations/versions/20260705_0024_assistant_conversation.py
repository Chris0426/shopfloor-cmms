"""assistant 對話落 DB(assistant_conversation + assistant_message)

Revision ID: 0024_assistant_conversation
Revises: 0023_failure_vocab
Create Date: 2026-07-05

ADR-020 dock 助理:把「純前端 hidden history」升級為 DB-backed,修正整頁導覽對話全滅
(重大缺失)+ 支援多 session。兩表照既有 AuditMixin 慣例:
- assistant_conversation:每人多對話,closed_at null = 開啟中(user 主動結束才設)。
- assistant_message:對話逐則訊息(role = user / assistant)。
擁有權 = user_id(domain 層強制)。無 seed。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_assistant_conversation"
down_revision: str | None = "0023_failure_vocab"
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
        "assistant_conversation",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.user_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_conversation_user", "assistant_conversation", ["user_id"], unique=False
    )

    op.create_table(
        "assistant_message",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("conversation_id", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["conversation_id"], ["assistant_conversation.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_assistant_message_conversation", "assistant_message", ["conversation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_assistant_message_conversation", table_name="assistant_message")
    op.drop_table("assistant_message")
    op.drop_index("ix_assistant_conversation_user", table_name="assistant_conversation")
    op.drop_table("assistant_conversation")
