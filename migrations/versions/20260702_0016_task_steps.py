"""task_step + task_part slice: 保養細項 + 步驟用料(PM(b);domain-model 05-tasks §8)

Revision ID: 0016_task_steps
Revises: 0015_stock_txn_charge_target
Create Date: 2026-07-02

task_steps_parts.csv(eMaint 保養細項)→ 正規化兩表。手寫。
- task_step:每列一步驟;合成 id = 穩定身分,proc_seq 純排序/溯源(可重複/可空);
  idempotency_key unique(taskstep:v1:<task_no>:<occurrence>,重跑冪等)。
- task_part:step 1:N part(一步可多料;現況 ≤1,模型天生支援多,Jordan 2026-07-02 拍板現在就正規化);
  replace_qty 可空(造冊未清點 → 保持原狀);UNIQUE(task_step_id,item_code)= 一步同料一筆。
FK:task_no→task、item_code→inventory_item(loader 對不上者跳過該 part/step + 記數,ADR-018)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_task_steps"
down_revision: str | None = "0015_stock_txn_charge_target"
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
        "task_step",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_no", sa.String(), sa.ForeignKey("task.task_no"), nullable=False),
        sa.Column("proc_seq", sa.Integer(), nullable=True),  # eMaint 原始序號(可空/重複)
        sa.Column("task_desc", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),  # 冪等錨
        *_audit_columns(),
    )
    op.create_index("ix_task_step_task", "task_step", ["task_no", "proc_seq", "id"])
    op.create_index("uq_task_step_idem", "task_step", ["idempotency_key"], unique=True)

    op.create_table(
        "task_part",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_step_id", sa.BigInteger(), sa.ForeignKey("task_step.id"), nullable=False
        ),
        sa.Column(
            "item_code", sa.String(), sa.ForeignKey("inventory_item.item_code"), nullable=False
        ),
        sa.Column("replace_qty", sa.Numeric(12, 3), nullable=True),  # 造冊未清點 → 可空
        sa.UniqueConstraint("task_step_id", "item_code", name="uq_task_part_step_item"),
        *_audit_columns(),
    )
    op.create_index("ix_task_part_step", "task_part", ["task_step_id"])


def downgrade() -> None:
    op.drop_index("ix_task_part_step", table_name="task_part")
    op.drop_table("task_part")
    op.drop_index("uq_task_step_idem", table_name="task_step")
    op.drop_index("ix_task_step_task", table_name="task_step")
    op.drop_table("task_step")
