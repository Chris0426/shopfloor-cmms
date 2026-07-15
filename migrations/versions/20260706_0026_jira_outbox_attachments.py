"""jira_outbox 加 attachments_uploaded(照片同步的重試防重旗標)

Revision ID: 0026_jira_outbox_attachments
Revises: 0025_jira_outbox
Create Date: 2026-07-06

工作紀錄照片同步到 MRQ comment(ADR-020 決策 1 延伸):
- jira_outbox 加 `attachments_uploaded`(Boolean NOT NULL server_default false)。
- 語意:flush 送 comment 前,先把該 note 的未軟刪附件逐張上傳到 MRQ;全部成功才把此旗標落定,
  最後才送 comment。重試時旗標=true → 跳過再上傳(防重複附件),只重送 comment。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_jira_outbox_attachments"
down_revision: str | None = "0025_jira_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jira_outbox",
        sa.Column(
            "attachments_uploaded",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("jira_outbox", "attachments_uploaded")
