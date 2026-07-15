"""storage_bin 受控詞彙(備品儲位)+ seed 80 個實體櫃位代號

Revision ID: 0028_storage_bin
Revises: 0027_wo_confirmed_reason
Create Date: 2026-07-10

倉庫櫃位受控詞彙(80 個代號)。`inventory_item.bin_location` 原為自由文字,
現改為受控詞彙:編輯備品時只能選 active 的 storage_bin 值(或留空),admin 可低摩擦新增。
additive-only:退役由 admin 治理(is_active 旗標),不刪除既有列;現有品項若持 legacy 髒值
(CSV 位移垃圾值 / 裸 "08")不受此表約束,編輯時原樣放行(見 InventoryService)。

seed 一字不差、保留大小寫(如 "Drawer" 混大小寫、"CMA" 全大寫);不含裸 "08"。
冪等(ON CONFLICT DO NOTHING);downgrade = drop table。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_storage_bin"
down_revision: str | None = "0027_wo_confirmed_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 倉庫櫃位受控詞彙的示意種子(demo 用一組規律代號;正式環境由各廠自行維護實際櫃位清單)。
_SEED_CODES: tuple[str, ...] = tuple(
    f"{shelf:02d}{col}"
    for shelf in range(1, 21)
    for col in ("A", "B", "C", "D")
) + ("Drawer", "Staging", "Returns", "Quarantine")


def upgrade() -> None:
    op.create_table(
        "storage_bin",
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    # seed(冪等):保留大小寫、is_active=true
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "INSERT INTO storage_bin (code, is_active) VALUES (:code, true) "
            "ON CONFLICT (code) DO NOTHING"
        ),
        [{"code": c} for c in _SEED_CODES],
    )


def downgrade() -> None:
    op.drop_table("storage_bin")
