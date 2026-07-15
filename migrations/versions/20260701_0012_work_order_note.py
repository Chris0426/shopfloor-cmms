"""work_order_note slice: append-only 工作日誌(domain-model 02 §1.6;ADR-020 決策 7)

Revision ID: 0012_work_order_note
Revises: 0011_part_issue_backfill
Create Date: 2026-07-01

長 down 工單分次、跨日更新 → 每筆一列(occurred_at + author),保留原貌。手寫。
- wo_note_type lookup(progress/diagnosis/hold/resume/part/note/report),種子固定 7 值。
- work_order_note:FK→work_order / wo_note_type;status_history_id 可連狀態轉移;
  idempotency_key unique(agent/pipeline 建 note 防重 + note↔jira comment 同步錨,ADR-006/020)。
- attachment_owner_type += 'work_order_note'(照片掛此 owner,owner_id = note.id 文字)→
  照片隨該筆時間戳,UI 時間線與 Jira comment 皆按此筆呈現。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_work_order_note"
down_revision: str | None = "0011_part_issue_backfill"
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


_NOTE_TYPES = [
    {"code": "report", "label": "初始報修 / 開單當下故障"},
    {"code": "progress", "label": "進度 / 處理紀錄"},
    {"code": "diagnosis", "label": "診斷 / 判定"},
    {"code": "hold", "label": "轉等待(等料 / 等商)+ 原因"},
    {"code": "resume", "label": "復工 / 恢復"},
    {"code": "part", "label": "領料註記"},
    {"code": "note", "label": "一般備註"},
]


def upgrade() -> None:
    op.create_table(
        "wo_note_type",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )
    op.bulk_insert(
        sa.table("wo_note_type", sa.column("code", sa.String), sa.column("label", sa.String)),
        _NOTE_TYPES,
    )

    op.create_table(
        "work_order_note",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "work_order_no",
            sa.BigInteger(),
            sa.ForeignKey("work_order.work_order_no"),
            nullable=False,
        ),
        sa.Column("entry_type", sa.String(), sa.ForeignKey("wo_note_type.code"), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),  # 保留原貌(html.unescape)
        sa.Column("author", sa.String(), nullable=False),  # human:<id> / agent:<name>
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),  # 更新時點
        sa.Column(
            "status_history_id",
            sa.BigInteger(),
            sa.ForeignKey("work_order_status_history.id"),
            nullable=True,
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=True),  # 建 note 防重 / jira 同步錨
        *_audit_columns(),
    )
    # 時間線熱路徑:某工單的 note 依 occurred_at 排序
    op.create_index("ix_work_order_note_wo", "work_order_note", ["work_order_no", "occurred_at"])
    # 建 note 防重(ADR-006);note↔jira comment 同步錨(ADR-020 決策 7)
    op.create_index(
        "uq_work_order_note_idem", "work_order_note", ["idempotency_key"], unique=True
    )

    # 照片 owner:note.id(文字)→ 照片隨該筆時間戳,對到 UI 時間線與 Jira comment
    op.bulk_insert(
        sa.table(
            "attachment_owner_type", sa.column("code", sa.String), sa.column("label", sa.String)
        ),
        [{"code": "work_order_note", "label": "工單日誌逐筆照片(owner_id = work_order_note.id)"}],
    )


def downgrade() -> None:
    op.execute("DELETE FROM attachment_owner_type WHERE code = 'work_order_note'")
    op.drop_index("uq_work_order_note_idem", table_name="work_order_note")
    op.drop_index("ix_work_order_note_wo", table_name="work_order_note")
    op.drop_table("work_order_note")
    op.drop_table("wo_note_type")
