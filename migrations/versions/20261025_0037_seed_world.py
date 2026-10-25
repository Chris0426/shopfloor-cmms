"""seed_world:種子世界指紋 + 重播進度(`cmms seed-events`)

Revision ID: 0037_seed_world
Revises: 0036_wo_seed_timestamps
Create Date: 2026-10-25

每個重播進本 instance 的 `tendline.seed.v1.1` 世界一列(`world_id` PK)。
- 指紋 = manifest 的 `STATE_FINGERPRINT_KEYS`(schema / world_id / generator_version /
  fleet_digest / horizon_start);續跑時指紋不符 → 整份拒讀(state_mismatch)。
- 進度 = `last_seq`(已消化的最後一個 seq,續跑從 +1 起)+ `applied_until`(上次的 cut-off)。
- `applied_at` = 首次重播時刻;`updated_at` = 最近一次進度更新。

downgrade = drop 表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0037_seed_world"
down_revision: str | None = "0036_wo_seed_timestamps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seed_world",
        sa.Column("world_id", sa.String(), nullable=False),
        sa.Column("schema", sa.String(), nullable=False),
        sa.Column("generator_version", sa.String(), nullable=False),
        sa.Column("fleet_digest", sa.String(), nullable=False),
        sa.Column("horizon_start", sa.String(), nullable=False),
        sa.Column("applied_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seq", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("world_id"),
    )


def downgrade() -> None:
    op.drop_table("seed_world")
