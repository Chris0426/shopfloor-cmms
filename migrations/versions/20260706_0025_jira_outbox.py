"""jira_outbox(note→MRQ comment 自動同步佇列)+ external_link.forward_idem_key(批次防重)

Revision ID: 0025_jira_outbox
Revises: 0024_assistant_conversation
Create Date: 2026-07-06

ADR-020 決策 1 修訂(cmms 直呼 Jira REST):
- work_order_external_link 加 `forward_idem_key`(nullable):批次 forward 防重錨,重跑不重開 MRQ。
- jira_outbox:連結建立後,工單新增 note → 排一列 → 背景 flush 用連結建立者 PAT 呼 Jira 加 comment。
  唯一鍵 (note_id, external_key) = 冪等。照既有 AuditMixin 慣例。無 seed。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_jira_outbox"
down_revision: str | None = "0024_assistant_conversation"
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
    op.add_column(
        "work_order_external_link",
        sa.Column("forward_idem_key", sa.String(length=128), nullable=True),
    )

    op.create_table(
        "jira_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("note_id", sa.BigInteger(), nullable=False),
        sa.Column("work_order_no", sa.BigInteger(), nullable=False),
        sa.Column("external_key", sa.String(), nullable=False),
        sa.Column("on_behalf_user", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_comment_id", sa.String(), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["note_id"], ["work_order_note.id"]),
        sa.ForeignKeyConstraint(["work_order_no"], ["work_order.work_order_no"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jira_outbox_note_id", "jira_outbox", ["note_id"], unique=False)
    op.create_index(
        "uq_jira_outbox_note_key", "jira_outbox", ["note_id", "external_key"], unique=True
    )
    op.create_index("ix_jira_outbox_status", "jira_outbox", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jira_outbox_status", table_name="jira_outbox")
    op.drop_index("uq_jira_outbox_note_key", table_name="jira_outbox")
    op.drop_index("ix_jira_outbox_note_id", table_name="jira_outbox")
    op.drop_table("jira_outbox")
    op.drop_column("work_order_external_link", "forward_idem_key")
