"""asset.owner(設備負責人)+ 由 pm_schedule.assigned_person 回填、消抵 per-PM 冗餘

Revision ID: 0029_asset_owner
Revises: 0028_storage_bin
Create Date: 2026-07-11

Jordan 裁決:每台機器都有一位負責人(負責其 PM + 維修)。過去 owner 只落在 per-PM
排程(`pm_schedule.assigned_person`,原本就是從機台 owner 帶下來的)。本切片收斂:
owner 落在資產(`asset.owner`),PM 排程與 REACTIVE 工單的 assignee 由它衍生;per-PM
`assigned_person` 退化為「明確覆寫」(罕見)。

schema:`asset` 新增 `owner VARCHAR NULL`。

data 回填(同一 migration):
  1. 對每台有 PM 排程且 assigned_person 非空的資產:若其所有 PM 排程的 assigned_person
     **恰好只有一個 distinct 值** → `asset.owner` 設為該值;若 >1(歧義)→ 留 NULL
     (Jordan 日後逐台人工釐清)。
  2. 然後把 `pm_schedule.assigned_person` = 該資產 owner 者一律清 NULL(這些成為「由資產
     owner 衍生」);其餘非空值即為刻意的 per-PM 覆寫,保留。

downgrade(近似,見下註):把 owner 回灌到 assigned_person 為 NULL 的 PM,再 drop 欄。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_asset_owner"
down_revision: str | None = "0028_storage_bin"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("asset", sa.Column("owner", sa.String(), nullable=True))
    # 回填 ①:唯一 assigned_person 的資產 → asset.owner(歧義多值者留 NULL)
    op.execute(
        """
        UPDATE asset AS a
        SET owner = sub.person
        FROM (
            SELECT asset_id, MIN(assigned_person) AS person
            FROM pm_schedule
            WHERE assigned_person IS NOT NULL
            GROUP BY asset_id
            HAVING COUNT(DISTINCT assigned_person) = 1
        ) AS sub
        WHERE a.asset_id = sub.asset_id
        """
    )
    # 回填 ②:等於資產 owner 的 per-PM assigned_person 清 NULL(改為「衍生自 asset.owner」)。
    # 剩餘非空值即刻意的 per-PM 覆寫,保留。
    op.execute(
        """
        UPDATE pm_schedule AS p
        SET assigned_person = NULL
        FROM asset AS a
        WHERE p.asset_id = a.asset_id
          AND a.owner IS NOT NULL
          AND p.assigned_person = a.owner
        """
    )


def downgrade() -> None:
    # 近似還原(不可能完全逆轉):把 asset.owner 回灌到 assigned_person 為 NULL 的 PM。
    # ★ 近似性:升級前本就「無 assignee」的 PM,若其資產有 owner,downgrade 後會多得一個
    #   assignee。這是可接受的 downgrade 取捨(資訊在升級 ② 步已不可分辨)。
    op.execute(
        """
        UPDATE pm_schedule AS p
        SET assigned_person = a.owner
        FROM asset AS a
        WHERE p.asset_id = a.asset_id
          AND p.assigned_person IS NULL
          AND a.owner IS NOT NULL
        """
    )
    op.drop_column("asset", "owner")
