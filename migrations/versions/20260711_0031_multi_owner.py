"""多負責人:asset_owner + work_order_assignee 交叉表(取代單一字串欄)

Revision ID: 0031_multi_owner
Revises: 0030_notify
Create Date: 2026-07-11

Slice C(Jordan 拍板):難維護的機台有**多位**負責人。報修開單時,該設備所有負責人皆自動
指派到工單、開立/結案皆通知全部人。單一字串欄無法承載多名(精確比對語意:Mine 過濾、通知
比對會壞),故改交叉表。

- `asset_owner`:(asset_id, person_name) 複合 PK + position 排序(position 只餵回相容單值欄)。
  所有負責人平等(除排序外無「主要」概念)。
- `work_order_assignee`:(work_order_no, person_name) 複合 PK + position。

資料回填(同一 migration):
  1. asset_owner ← asset.owner(非空者,position=0)。
  2. work_order_assignee ← work_order.assigned_person(非空者,position=0;~21k 列單一
     INSERT..SELECT)。
  3. DROP COLUMN asset.owner(交叉表成單一事實)。
  4. **保留** work_order.assigned_person 欄:分析平台契約(assigned_person=首位)+ 21k 歷史 + 顯示
     相容;自此為由 domain 寫入維護的「denormalized 首位 assignee」。

downgrade(近似):重建 asset.owner ← 每資產最小 position 的 person_name;drop 兩表。
  ★ 近似性:多負責人資產僅還原首位(其餘遺失);work_order.assigned_person 未動故無損。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_multi_owner"
down_revision: str | None = "0030_notify"
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
        "asset_owner",
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("person_name", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.asset_id"]),
        sa.PrimaryKeyConstraint("asset_id", "person_name"),
    )
    op.create_index("ix_asset_owner_person", "asset_owner", ["person_name"], unique=False)

    op.create_table(
        "work_order_assignee",
        sa.Column("work_order_no", sa.BigInteger(), nullable=False),
        sa.Column("person_name", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default=sa.text("0")),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["work_order_no"], ["work_order.work_order_no"]),
        sa.PrimaryKeyConstraint("work_order_no", "person_name"),
    )
    op.create_index(
        "ix_work_order_assignee_person", "work_order_assignee", ["person_name"], unique=False
    )

    # 回填 ①:asset.owner(非空)→ asset_owner position=0
    op.execute(
        """
        INSERT INTO asset_owner (asset_id, person_name, position, created_by, source_actor)
        SELECT asset_id, owner, 0, 'migration:0031', 'migration:0031'
        FROM asset
        WHERE owner IS NOT NULL AND btrim(owner) <> ''
        """
    )
    # 回填 ②:work_order.assigned_person(非空)→ work_order_assignee position=0(~21k 列)
    op.execute(
        """
        INSERT INTO work_order_assignee
            (work_order_no, person_name, position, created_by, source_actor)
        SELECT work_order_no, assigned_person, 0, 'migration:0031', 'migration:0031'
        FROM work_order
        WHERE assigned_person IS NOT NULL AND btrim(assigned_person) <> ''
        """
    )
    # ③ 交叉表成單一事實 → drop asset.owner。work_order.assigned_person 保留(見 docstring)。
    op.drop_column("asset", "owner")


def downgrade() -> None:
    op.add_column("asset", sa.Column("owner", sa.String(), nullable=True))
    # 近似還原:每資產取最小 position 的 person_name 回灌 asset.owner(多負責人只還原首位)。
    op.execute(
        """
        UPDATE asset AS a
        SET owner = sub.person_name
        FROM (
            SELECT DISTINCT ON (asset_id) asset_id, person_name
            FROM asset_owner
            ORDER BY asset_id, position, person_name
        ) AS sub
        WHERE a.asset_id = sub.asset_id
        """
    )
    op.drop_index("ix_work_order_assignee_person", table_name="work_order_assignee")
    op.drop_table("work_order_assignee")
    op.drop_index("ix_asset_owner_person", table_name="asset_owner")
    op.drop_table("asset_owner")
