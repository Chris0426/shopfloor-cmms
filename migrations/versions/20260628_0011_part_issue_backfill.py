"""part_issue backfill slice: seed stock_txn_kind codes (deploy-safety)

Revision ID: 0011_part_issue_backfill
Revises: 0010_attachments
Create Date: 2026-06-28

對應 docs/domain-model/02-work-orders.md §5.8(歷史領料回填)。手寫、純種子(不建表)。

`stock_txn_kind` 表由 0007 建立但未種子;歷史領料回填(`load-part-issues`)用到 ISSUE。
本 migration 冪等種子 4 個 canonical 異動類別,使 `load-part-issues` 可在 `load-work-orders`
之前獨立執行(deploy-safety,護欄 #8)。label 與 work_order loader 的 STOCK_TXN_KIND_LABELS
一致;ON CONFLICT DO NOTHING 故與 loader 的 upsert_lookup 並存不衝突(先到者勝、後者 no-op)。

★ 鏈線性(M1):平行 attachment 切片已落地 0010_attachments(接 0009);本 migration 接 0010
  而非 0009,以維持單 head。本 migration 純資料(無 DDL)→ 不影響 `alembic check` schema 比對。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_part_issue_backfill"
down_revision: str | None = "0010_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 冪等種子:stock_txn_kind(0007 已建)可能已有 loader 寫入的列 → ON CONFLICT DO NOTHING。
    # label 與 src/cmms/domain/work_order/loader.py 的 STOCK_TXN_KIND_LABELS 一致(勿分歧)。
    op.execute(
        sa.text(
            "INSERT INTO stock_txn_kind (code, label) VALUES "
            "('ISSUE', 'Issue(領用出庫)'), "
            "('RETURN', 'Return(退回入庫)'), "
            "('ADJUST', 'Adjust(盤點調整)'), "
            "('RECEIVE', 'Receive(採購入庫)') "
            "ON CONFLICT (code) DO NOTHING"
        )
    )


def downgrade() -> None:
    # 純種子;不刪(其他切片 / loader 可能依賴這些 lookup)。
    pass
