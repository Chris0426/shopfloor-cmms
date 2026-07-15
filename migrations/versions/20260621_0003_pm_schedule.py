"""pm_schedule slice #3: PM schedule (ScheduledActivity) + freq_unit/vendor lookups

Revision ID: 0003_pm_schedule
Revises: 0002_tasks
Create Date: 2026-06-21

對應 docs/domain-model/03-scheduled-activity.md §8。手寫(本機無 DB 可 autogenerate)。
反正規化欄(line_no/comp_desc/task_desc/pm_type)依 §5.1 不建;calendar_freq_type 存 text
(S1 值域未定);consumables N:M 無資料,整個延後。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_pm_schedule"
down_revision: str | None = "0002_tasks"
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
    # lookups(先建,pm_schedule 會 FK 它們)
    _code_label("freq_unit")  # Months / Weeks / Days
    _code_label("vendor")  # CMA / CMB(WorkOrder 切片共用)

    op.create_table(
        "pm_schedule",
        sa.Column("pm_id", sa.String(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("asset.asset_id"), nullable=False),
        sa.Column("task_id", sa.String(), sa.ForeignKey("task.task_no"), nullable=False),
        sa.Column("frequency_interval", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("frequency_unit", sa.String(), sa.ForeignKey("freq_unit.code"), nullable=True),
        sa.Column("calendar_freq_type", sa.String(), nullable=True),  # [UI] S1 待定
        sa.Column("skip_weekends_holidays", sa.Boolean(), nullable=True),  # [UI]
        sa.Column("next_due_date", sa.Date(), nullable=True),
        sa.Column("last_pm_date", sa.Date(), nullable=True),
        sa.Column("last_work_order_no", sa.BigInteger(), nullable=True),  # soft ref(WO 切片)
        sa.Column("completion_window_days", sa.Numeric(4, 1), nullable=True),
        sa.Column("standard_hours", sa.Numeric(6, 2), nullable=True),
        sa.Column("estimated_labor_hours", sa.Numeric(6, 2), nullable=True),
        sa.Column("assigned_vendor", sa.String(), sa.ForeignKey("vendor.code"), nullable=True),
        sa.Column("assigned_person", sa.String(), nullable=True),  # FK→person 延到 Contacts 切片
        sa.Column("pm_group", sa.String(), nullable=True),  # [UI]
        sa.Column("is_suppressed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_audit_columns(),
        sa.UniqueConstraint("asset_id", "task_id"),  # 天然唯一鍵
    )


def downgrade() -> None:
    op.drop_table("pm_schedule")
    op.drop_table("vendor")
    op.drop_table("freq_unit")
