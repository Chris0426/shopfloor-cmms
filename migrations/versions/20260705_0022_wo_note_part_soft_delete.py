"""work_order_note / work_order_part 軟刪欄位(工單 UX 批 W1:#1 日誌可刪 + #9 領料取消)

Revision ID: 0022_wo_note_part_soft_delete
Revises: 0021_ai_candidate_note_type
Create Date: 2026-07-05

Jordan 2026-07-05 回饋:① 工作日誌記錯要能刪(裁決:刪除,軟刪保留稽核)
⑨ 工單領料改量/取消(取消 = RETURN 回庫 + work_order_part 軟刪)。比照 0020 全系統
軟刪慣例(task_step/task_part/work_order_external_link):`deleted_at`/`deleted_by`,
讀取面過濾。ledger(stock_transaction)為 append-only,取消不刪帳、只補 RETURN 補償帳;
work_order_part 是摘要列,軟刪讓它從時間線/領料清單消失,誰/何時可查。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_wo_note_part_soft_delete"
down_revision: str | None = "0021_ai_candidate_note_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_order_note", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("work_order_note", sa.Column("deleted_by", sa.String(), nullable=True))
    op.add_column(
        "work_order_part", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("work_order_part", sa.Column("deleted_by", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("work_order_part", "deleted_by")
    op.drop_column("work_order_part", "deleted_at")
    op.drop_column("work_order_note", "deleted_by")
    op.drop_column("work_order_note", "deleted_at")
