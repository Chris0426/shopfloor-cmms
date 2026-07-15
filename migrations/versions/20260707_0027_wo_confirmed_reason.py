"""work_order 加 confirmed_reason_code(D6 人工確認故障真因,efc 軸)

Revision ID: 0027_wo_confirmed_reason
Revises: 0026_jira_outbox_attachments
Create Date: 2026-07-07

D6「confirmed_reason 回流」切片:工單新增選填、人工確認的故障真因碼。
- 軸 = efc 單軸(equipment_failure_code,107 碼);FK 綁其自然鍵 code。**永不鑄 canonical
  碼、永不觸 mfc 軸**。
- 語意:僅 REACTIVE 工單有意義(PM 發現故障應另開報修);null=未確認(≠ 無故障)。
- 對外契約 additive:contract_wo_detail.v1 +confirmed_reason_code(nullable),schema 不升版。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_wo_confirmed_reason"
down_revision: str | None = "0026_jira_outbox_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_order", sa.Column("confirmed_reason_code", sa.String(), nullable=True)
    )
    # FK 綁 equipment_failure_code.code(唯一鍵 uq_equipment_failure_code_code);命名比照
    # fk_work_order_hold_reason 慣例。退役碼仍可被歷史工單引用(is_active 僅治理新選單)。
    op.create_foreign_key(
        "fk_work_order_confirmed_reason",
        "work_order",
        "equipment_failure_code",
        ["confirmed_reason_code"],
        ["code"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_work_order_confirmed_reason", "work_order", type_="foreignkey"
    )
    op.drop_column("work_order", "confirmed_reason_code")
