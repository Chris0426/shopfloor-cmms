"""user_account.emaint_assignee: legacy 指派名橋(Slice 2「我的工單/保養」過濾)

Revision ID: 0014_user_emaint_assignee
Revises: 0013_identity
Create Date: 2026-07-01

「我的」= work_order/pm_schedule.assigned_person == user_account.emaint_assignee。
純 additive ADD COLUMN(nullable),對既有 user_account 列安全。
不採 person_id FK —— 實測多數 assigned_person 對不上 contacts fullname,
最大的技師(Alice Fang/Ben Yeh/Cara Lo)全不在 contacts,故存確切字串直接等值比對。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_user_emaint_assignee"
down_revision: str | None = "0013_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_account", sa.Column("emaint_assignee", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_account", "emaint_assignee")
