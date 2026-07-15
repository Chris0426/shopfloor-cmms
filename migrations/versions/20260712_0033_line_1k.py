"""產線受控值更名:01K → 1K(01K 為遷就舊 eMaint 字母排序的 workaround,)

Revision ID: 0033_line_1k
Revises: 0032_notify_watch
Create Date: 2026-07-12

data-only migration:把 `line` lookup 的 `01K` 列改名為 `1K`,並將 `asset.line` 指向的
FK 值一併搬遷。line 表由 loader 動態種子(migration 不 seed),故空庫(fresh upgrade
0001→0033、資料尚未灌)時三條語句全為 no-op,必須安全。

- upgrade:①(若存在 01K 列)插入 1K 列〔label:原值 '01K' → '1K',否則沿用原 label〕
  → ② UPDATE asset SET line='1K' WHERE line='01K' → ③ DELETE line WHERE code='01K'。
- downgrade:鏡像反向(1K → 01K)。
- 純 SQL(PostgreSQL),INSERT…SELECT…ON CONFLICT DO NOTHING 保證冪等。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033_line_1k"
down_revision: str | None = "0032_notify_watch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① 依 01K 現有列建 1K 列(label '01K' 正名為 '1K',其餘 label 沿用);已存在則不動
    op.execute(
        """
        INSERT INTO line (code, label)
        SELECT '1K', CASE WHEN label = '01K' THEN '1K' ELSE label END
        FROM line WHERE code = '01K'
        ON CONFLICT (code) DO NOTHING
        """
    )
    # ② 搬遷 FK 引用
    op.execute("UPDATE asset SET line = '1K' WHERE line = '01K'")
    # ③ 移除舊列
    op.execute("DELETE FROM line WHERE code = '01K'")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO line (code, label)
        SELECT '01K', CASE WHEN label = '1K' THEN '01K' ELSE label END
        FROM line WHERE code = '1K'
        ON CONFLICT (code) DO NOTHING
        """
    )
    op.execute("UPDATE asset SET line = '01K' WHERE line = '1K'")
    op.execute("DELETE FROM line WHERE code = '1K'")
