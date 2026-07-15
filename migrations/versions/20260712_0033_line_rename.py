"""產線受控值更名:ZA-legacy → Zone A(ZA-legacy 為遷就舊 eMaint 字母排序的 workaround,)

Revision ID: 0033_line_rename
Revises: 0032_notify_watch
Create Date: 2026-07-12

data-only migration:把 `line` lookup 的 `ZA-legacy` 列改名為 `Zone A`,並將 `asset.line` 指向的
FK 值一併搬遷。line 表由 loader 動態種子(migration 不 seed),故空庫(fresh upgrade
0001→0033、資料尚未灌)時三條語句全為 no-op,必須安全。

- upgrade:①(若存在 ZA-legacy 列)插入 Zone A 列〔label:原值 'ZA-legacy' → 'Zone A',否則沿用原 label〕
  → ② UPDATE asset SET line='Zone A' WHERE line='ZA-legacy' → ③ DELETE line WHERE code='ZA-legacy'。
- downgrade:鏡像反向(Zone A → ZA-legacy)。
- 純 SQL(PostgreSQL),INSERT…SELECT…ON CONFLICT DO NOTHING 保證冪等。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0033_line_rename"
down_revision: str | None = "0032_notify_watch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ① 依 ZA-legacy 現有列建 Zone A 列(label 'ZA-legacy' 正名為 'Zone A',其餘 label 沿用);已存在則不動
    op.execute(
        """
        INSERT INTO line (code, label)
        SELECT 'Zone A', CASE WHEN label = 'ZA-legacy' THEN 'Zone A' ELSE label END
        FROM line WHERE code = 'ZA-legacy'
        ON CONFLICT (code) DO NOTHING
        """
    )
    # ② 搬遷 FK 引用
    op.execute("UPDATE asset SET line = 'Zone A' WHERE line = 'ZA-legacy'")
    # ③ 移除舊列
    op.execute("DELETE FROM line WHERE code = 'ZA-legacy'")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO line (code, label)
        SELECT 'ZA-legacy', CASE WHEN label = 'Zone A' THEN 'ZA-legacy' ELSE label END
        FROM line WHERE code = 'Zone A'
        ON CONFLICT (code) DO NOTHING
        """
    )
    op.execute("UPDATE asset SET line = 'ZA-legacy' WHERE line = 'Zone A'")
    op.execute("DELETE FROM line WHERE code = 'Zone A'")
