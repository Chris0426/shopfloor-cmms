"""工單 open/close 通知(email + Telegram)收件人詞彙 + outbox 佇列

Revision ID: 0030_notify
Revises: 0029_asset_owner
Create Date: 2026-07-11

Slice B(工單開立/結案通知,Jordan 拍板):
- REACTIVE 報修開立 → 通知機台負責人 + 工程團隊 + 線管理者(downtime-critical);PM 生成亦通知。
  兩類工單結案皆通知。中途 note 更新不通知。
- 通道 = email + Telegram(非 LINE)。管理者無 cmms 帳號 → 收件人**不綁 user_account**
  (獨立 notify_recipient 詞彙,由 admin 維護)。
- notify_recipient:name + email? + telegram_chat_id?(String,群組 id 為負數)+ assignee_name?
  (精確比對 work_order.assigned_person 供機台負責人個人通知)+ notify_on_open/close 廣播旗標
  + is_active + 標準 7 稽核欄。
- notification_outbox:每 (工單, 事件, 通道, 收件人) 一列;唯一鍵冪等 → **reopen→re-close 不重發**
  (視為可接受:唯一鍵已存在則 on_conflict_do_nothing)。逐列 flush(pending / failed<5 重試),
  未配置通道 → 該列略過(留 pending、不燒 attempts)。email 送出重用既有 EmailSender。

冪等(seed 無);downgrade = drop 兩表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030_notify"
down_revision: str | None = "0029_asset_owner"
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
        "notify_recipient",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        # 群組 chat_id 可為負數字串 → 存 String,不存 Integer
        sa.Column("telegram_chat_id", sa.String(), nullable=True),
        # 精確比對 work_order.assigned_person 供機台負責人個人通知(非 FK,legacy 確切字串)
        sa.Column("assignee_name", sa.String(), nullable=True),
        sa.Column(
            "notify_on_open", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "notify_on_close", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_audit_columns(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("work_order_no", sa.BigInteger(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),  # opened | closed
        sa.Column("channel", sa.String(), nullable=False),  # email | telegram
        sa.Column("recipient_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_msg_id", sa.String(), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["work_order_no"], ["work_order.work_order_no"]),
        sa.ForeignKeyConstraint(["recipient_id"], ["notify_recipient.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # 冪等唯一鍵:同一 (工單, 事件, 通道, 收件人) 只一列 → reopen→re-close 不重發
    op.create_index(
        "uq_notification_outbox_key",
        "notification_outbox",
        ["work_order_no", "event", "channel", "recipient_id"],
        unique=True,
    )
    op.create_index(
        "ix_notification_outbox_status", "notification_outbox", ["status"], unique=False
    )
    op.create_index(
        "ix_notification_outbox_wo", "notification_outbox", ["work_order_no"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_notification_outbox_wo", table_name="notification_outbox")
    op.drop_index("ix_notification_outbox_status", table_name="notification_outbox")
    op.drop_index("uq_notification_outbox_key", table_name="notification_outbox")
    op.drop_table("notification_outbox")
    op.drop_table("notify_recipient")
