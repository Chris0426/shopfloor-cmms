"""task_step / task_part / work_order_external_link 軟刪欄位(review f14cf8d 修正批)

Revision ID: 0020_soft_delete_columns
Revises: 0019_hold_reason_machine_time
Create Date: 2026-07-04

review f14cf8d 發現 delete_task_step / remove_task_part 是全 codebase 僅有的硬刪,
違反護欄 #4(全稽核):刪除後無人/無時/無因可查,而 900 筆 eMaint 匯入步驟是歷史
PM 工單「當時執行了什麼」的對帳依據。比照全系統慣例(WO 走 CANCELLED/VOIDED、
attachment 走 soft delete)改為軟刪:`deleted_at`/`deleted_by`,讀取面過濾。
`work_order_external_link` 同批補 `removed_at`/`removed_by`(修 review 發現的
「MRQ 連結打錯字永遠拿不掉」:先前無任何 unlink 路徑,gateway 上線後會同步到錯的 MRQ)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_soft_delete_columns"
down_revision: str | None = "0019_hold_reason_machine_time"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("task_step", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_step", sa.Column("deleted_by", sa.String(), nullable=True))
    op.add_column("task_part", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("task_part", sa.Column("deleted_by", sa.String(), nullable=True))
    op.add_column(
        "work_order_external_link",
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("work_order_external_link", sa.Column("removed_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_order_external_link", "removed_by")
    op.drop_column("work_order_external_link", "removed_at")
    op.drop_column("task_part", "deleted_by")
    op.drop_column("task_part", "deleted_at")
    op.drop_column("task_step", "deleted_by")
    op.drop_column("task_step", "deleted_at")
