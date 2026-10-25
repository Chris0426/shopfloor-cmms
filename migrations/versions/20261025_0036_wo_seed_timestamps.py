"""work_order += failure_started_at / production_resumed_at(合成 parity instance 用)

Revision ID: 0036_wo_seed_timestamps
Revises: 0035_help_feedback
Create Date: 2026-10-25

2026-10-25 新增,供消費 `tendline.seed.v1.1` 種子帳的 parity instance(合成 Work instance,
與產生器共用 `world_id`)使用。欄名**刻意照該契約命名**(`failure_started_at` /
`production_resumed_at`),日後私有樹發佈時可原樣 merge 回來,不必改名對版。

語意:
- `failure_started_at`:機台**實際停機**的時刻。必須**嚴格早於** `opened_at`(先停機、後報修);
  可空(未知 = null,不推算)。
- `production_resumed_at`:維修後**恢復生產**的時刻。**僅 REACTIVE** 有意義、必須晚於工單
  開始處理的時刻;可空(未回報 = null)。

兩欄皆 nullable TIMESTAMPTZ,無預設值、不回填既有工單(歷史單維持 null = 未知)。
downgrade = drop 兩欄。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036_wo_seed_timestamps"
down_revision: str | None = "0035_help_feedback"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "work_order",
        sa.Column("failure_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "work_order",
        sa.Column("production_resumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("work_order", "production_resumed_at")
    op.drop_column("work_order", "failure_started_at")
