"""產線受控值收斂(公開版為 no-op)

Revision ID: 0033_line_rename
Revises: 0032_notify_watch
Create Date: 2026-07-12

原系統中,此 revision 把一個 legacy 產線 code 收斂到其正規 code(data-only migration)。
公開快照不含任何 legacy 資料、也不含廠區專屬產線 code,故此步僅為維持 revision 鏈
連續而保留,upgrade / downgrade 皆不做任何事。
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0033_line_rename"
down_revision: str | None = "0032_notify_watch"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 刻意 no-op:僅維持 revision 鏈連續(見模組 docstring)
    pass


def downgrade() -> None:
    # 刻意 no-op:對應 upgrade 無任何變更
    pass
