"""per-assignee 關注名單:notify_watch(某人關注多位負責人 → 其名下工單開/結案皆通知)

Revision ID: 0032_notify_watch
Revises: 0031_multi_owner
Create Date: 2026-07-12

Slice D:不是每個人都該收每一則廣播。改由**特定人關注特定負責人**——
例:某位工單負責人的名下工單,其主管與代班夥伴須於開單 AND 結案收到通知。
一人可關注多位負責人。規則由 admin 於 /admin/notify 維護。

- `notify_watch`:(recipient_id, assignee_name) 複合 PK —— 一收件人關注多位負責人(多列)。
  assignee_name 精確比對 work_order_assignee.person_name(legacy 確切字串,非 FK)。
- 去重保證:一人一列 notify_recipient + 既有 outbox 唯一鍵 (work_order_no, event, channel,
  recipient_id) ⇒ 無論命中幾條規則(本人定向 / 關注 / 多負責人重疊),每人每事件只收一封。
- 無 seed(規則屬資料,另由 ops 腳本灌)。downgrade = drop table。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_notify_watch"
down_revision: str | None = "0031_multi_owner"
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
        "notify_watch",
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("assignee_name", sa.String(), nullable=False),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["recipient_id"], ["notify_recipient.id"]),
        sa.PrimaryKeyConstraint("recipient_id", "assignee_name"),
    )
    op.create_index(
        "ix_notify_watch_assignee", "notify_watch", ["assignee_name"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_notify_watch_assignee", table_name="notify_watch")
    op.drop_table("notify_watch")
