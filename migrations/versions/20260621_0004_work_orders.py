"""work_order slice #4 (read-only #4a): work_order + work_type/wo_status lookups

Revision ID: 0004_work_orders
Revises: 0003_pm_schedule
Create Date: 2026-06-21

對應 docs/domain-model/02-work-orders.md §8。手寫(本機無 DB 可 autogenerate)。
純讀取本體:歷史工單 + work_type/wo_status lookup;vendor 沿用 0003。狀態機 / 兩階段
寫入 / pending_proposal / downtime 結算 / W4 重分類延到 #4b。`miscreated` 不建(W1 未定)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_work_orders"
down_revision: str | None = "0003_pm_schedule"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _code_label(name: str) -> None:
    op.create_table(
        name,
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )


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
    # lookups(vendor 已於 0003 建;此處只建 work_type / wo_status)
    _code_label("work_type")
    _code_label("wo_status")

    op.create_table(
        "work_order",
        sa.Column("work_order_no", sa.BigInteger(), primary_key=True, autoincrement=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("asset.asset_id"), nullable=False),
        sa.Column("work_type", sa.String(), sa.ForeignKey("work_type.code"), nullable=False),
        sa.Column("status", sa.String(), sa.ForeignKey("wo_status.code"), nullable=False),
        sa.Column("brief_description", sa.Text(), nullable=True),
        sa.Column("diagnosis", sa.Text(), nullable=True),
        sa.Column("external_ref", sa.String(), nullable=True),
        sa.Column("opened_date", sa.Date(), nullable=False),
        sa.Column("scheduled_date", sa.Date(), nullable=True),
        sa.Column("work_start_time", sa.Time(), nullable=True),
        sa.Column("work_complete_time", sa.Time(), nullable=True),
        sa.Column("closed_date", sa.Date(), nullable=True),
        sa.Column("closed_time", sa.Time(), nullable=True),
        sa.Column("closed_by", sa.String(), nullable=True),
        sa.Column("assigned_vendor", sa.String(), sa.ForeignKey("vendor.code"), nullable=True),
        sa.Column("assigned_person", sa.String(), nullable=True),
        # [UI] 暫空
        sa.Column("priority", sa.String(), nullable=True),
        sa.Column("action_taken", sa.Text(), nullable=True),
        sa.Column("downtime_minutes", sa.Integer(), nullable=True),
        sa.Column("labor_hours", sa.Numeric(6, 2), nullable=True),
        sa.Column("cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("opened_by", sa.String(), nullable=True),
        sa.Column("pm_source_id", sa.String(), nullable=True),  # →pm_schedule(FK 延後)
        *_audit_columns(),
    )
    op.create_index("ix_work_order_asset_id", "work_order", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_work_order_asset_id", table_name="work_order")
    op.drop_table("work_order")
    op.drop_table("wo_status")
    op.drop_table("work_type")
