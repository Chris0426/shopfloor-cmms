"""stock_transaction charge target slice: 領料歸屬 WorkOrder | Asset(ADR-024)

Revision ID: 0015_stock_txn_charge_target
Revises: 0014_user_emaint_assignee
Create Date: 2026-07-01

放寬「領料硬綁工單」→ 歸屬對象二元(WorkOrder | Asset),ISSUE 恰含其一。手寫、additive。
- `charge_target_asset_id`:直領(非工單)歸屬的設備 EID(FK→asset;工單領料留 NULL,asset 經工單解析)。
- CHECK `ck_stock_transaction_issue_charge`:僅約束 `kind='ISSUE'` —— num_nonnulls(work_order_no,
  charge_target_asset_id)=1(恰含其一;禁孤兒領料、禁雙重歸屬)。`RECEIVE`/`ADJUST`/`RETURN` 不受限。
- 對既有列安全:所有既有 ISSUE 列 `work_order_no` 非空、新欄全 NULL → num_nonnulls=1,ADD CONSTRAINT
  的既有列驗證必過;無非 ISSUE 列存在。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_stock_txn_charge_target"
down_revision: str | None = "0014_user_emaint_assignee"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 直領歸屬:設備 EID(FK→asset;inline FK → Postgres 預設名 *_fkey,與 model 反射對齊)。
    op.add_column(
        "stock_transaction",
        sa.Column(
            "charge_target_asset_id",
            sa.String(),
            sa.ForeignKey("asset.asset_id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_stock_transaction_charge_target_asset_id",
        "stock_transaction",
        ["charge_target_asset_id"],
    )
    # 不變量守門(ADR-024 決策 1):ISSUE 恰有一個歸屬(工單 xor 設備);非 ISSUE 不受限。
    op.create_check_constraint(
        "ck_stock_transaction_issue_charge",
        "stock_transaction",
        "kind <> 'ISSUE' OR num_nonnulls(work_order_no, charge_target_asset_id) = 1",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_stock_transaction_issue_charge", "stock_transaction", type_="check"
    )
    op.drop_index(
        "ix_stock_transaction_charge_target_asset_id", table_name="stock_transaction"
    )
    op.drop_column("stock_transaction", "charge_target_asset_id")
