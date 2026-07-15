"""work_order write slice #4b-1: state machine + downtime + parts/stock ledger

Revision ID: 0007_work_order_write
Revises: 0006_contacts
Create Date: 2026-06-22

對應 docs/domain-model/02-work-orders.md §3/§8(#4b)。手寫。本 migration = 4b-1 核心引擎:
- wo_status 擴 canonical 狀態機屬性(rank/is_terminal/is_downtime);新 wo_hold_reason lookup。
- work_order 加狀態機時間軸(opened_at/closed_at,timestamptz)+ downtime_estimated + hold_reason。
- work_order_status_history(轉移記錄,downtime 來源)、work_order_part(領料)。
- stock_txn_kind lookup + stock_transaction(庫存異動帳,ADR-005;領料連動扣 on_hand)。
gated write 外部層(pending_proposal + on-box 欄)留 4b-2(migration 0008)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_work_order_write"
down_revision: str | None = "0006_contacts"
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
    # wo_status:擴 canonical 狀態機屬性(server_default 使既有列/空表皆有效)
    op.add_column(
        "wo_status", sa.Column("rank", sa.Integer(), server_default=sa.text("0"), nullable=False)
    )
    op.add_column(
        "wo_status",
        sa.Column("is_terminal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "wo_status",
        sa.Column("is_downtime", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    op.create_table(
        "wo_hold_reason",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("is_downtime", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )

    # work_order:狀態機時間軸 + downtime 精算旗標 + 暫停原因
    op.add_column("work_order", sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("work_order", sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "work_order",
        sa.Column(
            "downtime_estimated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.add_column("work_order", sa.Column("hold_reason", sa.String(), nullable=True))
    op.create_foreign_key(
        "fk_work_order_hold_reason", "work_order", "wo_hold_reason", ["hold_reason"], ["code"]
    )

    op.create_table(
        "work_order_status_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "work_order_no",
            sa.BigInteger(),
            sa.ForeignKey("work_order.work_order_no"),
            nullable=False,
            index=True,
        ),
        sa.Column("from_status", sa.String(), nullable=True),
        sa.Column("to_status", sa.String(), sa.ForeignKey("wo_status.code"), nullable=False),
        sa.Column("hold_reason", sa.String(), sa.ForeignKey("wo_hold_reason.code"), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        *_audit_columns(),
    )

    op.create_table(
        "work_order_part",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "work_order_no",
            sa.BigInteger(),
            sa.ForeignKey("work_order.work_order_no"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "item_code", sa.String(), sa.ForeignKey("inventory_item.item_code"), nullable=False
        ),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        *_audit_columns(),
    )

    op.create_table(
        "stock_txn_kind",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )

    op.create_table(
        "stock_transaction",
        sa.Column("txn_id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "item_code",
            sa.String(),
            sa.ForeignKey("inventory_item.item_code"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "work_order_no",
            sa.BigInteger(),
            sa.ForeignKey("work_order.work_order_no"),
            nullable=True,
            index=True,
        ),
        sa.Column("qty_delta", sa.Numeric(12, 3), nullable=False),
        sa.Column("kind", sa.String(), sa.ForeignKey("stock_txn_kind.code"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), unique=True, nullable=True),
        *_audit_columns(),
    )


def downgrade() -> None:
    op.drop_table("stock_transaction")
    op.drop_table("stock_txn_kind")
    op.drop_table("work_order_part")
    op.drop_table("work_order_status_history")
    op.drop_constraint("fk_work_order_hold_reason", "work_order", type_="foreignkey")
    op.drop_column("work_order", "hold_reason")
    op.drop_column("work_order", "downtime_estimated")
    op.drop_column("work_order", "closed_at")
    op.drop_column("work_order", "opened_at")
    op.drop_table("wo_hold_reason")
    op.drop_column("wo_status", "is_downtime")
    op.drop_column("wo_status", "is_terminal")
    op.drop_column("wo_status", "rank")
