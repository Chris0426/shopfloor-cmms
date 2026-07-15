"""C2 失效受控詞彙兩軸 lookup(mes_failmode + equipment_failure_code)

Revision ID: 0023_failure_vocab
Revises: 0022_wo_note_part_soft_delete
Create Date: 2026-07-05

內部設計評審 W4/消費端需求 裁決:C2 共用失效詞彙 committed,cmms 持受控詞彙單一權威 lookup,
分析平台供種子。兩軸(詞彙來源方鐵則:永不合併成一張表):
- mes_failmode(mfc,product/yield 軸「料為何被判退」):自然鍵 (station, label);
  signal_id 跨站碰撞 → 不可單獨當唯一鍵,故 nullable 且不建唯一鍵。
- equipment_failure_code(efc,equipment 軸「機台為何故障」):自然鍵 code。
無資料 seed —— 載入走 CLI(load-mes-failmodes / load-efc-codes),prod 於 on-box 執行,
比照其他大型載入(relationships / part-issues / media)。additive-only:is_active 退役旗
由 admin 治理,loader 永不翻。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_failure_vocab"
down_revision: str | None = "0022_wo_note_part_soft_delete"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mes_failmode",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("station", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("signal_id", sa.String(), nullable=True),
        sa.Column("entry_kind", sa.String(), nullable=False),
        sa.Column("seg_class", sa.String(), nullable=True),
        sa.Column("mes_variable", sa.String(), nullable=True),
        sa.Column("material_class", sa.String(), nullable=True),
        sa.Column("semantic_zh", sa.Text(), nullable=True),
        sa.Column("dominant_in_chronic", sa.String(), nullable=True),
        sa.Column("source_adapter", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("source_actor", sa.String(), nullable=True),
        sa.Column("proposed_by", sa.String(), nullable=True),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_mes_failmode_station_label", "mes_failmode", ["station", "label"], unique=True
    )
    op.create_index("ix_mes_failmode_station", "mes_failmode", ["station"], unique=False)

    op.create_table(
        "equipment_failure_code",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(), nullable=False),
        sa.Column("descr", sa.Text(), nullable=True),
        sa.Column("station_hint", sa.String(), nullable=True),
        sa.Column("recency_status", sa.String(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", sa.String(), nullable=True),
        sa.Column("source_actor", sa.String(), nullable=True),
        sa.Column("proposed_by", sa.String(), nullable=True),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_equipment_failure_code_code", "equipment_failure_code", ["code"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_equipment_failure_code_code", table_name="equipment_failure_code")
    op.drop_table("equipment_failure_code")
    op.drop_index("ix_mes_failmode_station", table_name="mes_failmode")
    op.drop_index("uq_mes_failmode_station_label", table_name="mes_failmode")
    op.drop_table("mes_failmode")
