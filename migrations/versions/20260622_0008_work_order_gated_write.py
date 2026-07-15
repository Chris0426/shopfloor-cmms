"""work_order gated-write #4b-2: pending_proposal (ADR-016) + on-box columns (ADR-017)

Revision ID: 0008_work_order_gated_write
Revises: 0007_work_order_write
Create Date: 2026-06-22

對應 docs/ARCHITECTURE.md ADR-016 / ADR-017、docs/domain-model/02-work-orders.md §3.2 / §8。手寫。
- pending_proposal:兩階段外部確認(propose/confirm + pending token,proposer/confirmer 稽核)。
- work_order 加 on-box 三欄:origin_station / idempotency_key(128, unique)/ evidence_ref(160),
  verbatim 存(不 parse;欄寬與 分析平台釘死的形狀對齊,下游契約登記)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_work_order_gated_write"
down_revision: str | None = "0007_work_order_write"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_proposal",
        sa.Column("pending_token", sa.String(), primary_key=True),
        sa.Column("operation", sa.String(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("dry_run_diff", postgresql.JSONB(), nullable=True),
        sa.Column("proposed_by", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), unique=True, nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column("work_order", sa.Column("origin_station", sa.String(), nullable=True))
    op.add_column("work_order", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("work_order", sa.Column("evidence_ref", sa.String(160), nullable=True))
    op.create_unique_constraint("uq_work_order_idempotency_key", "work_order", ["idempotency_key"])


def downgrade() -> None:
    op.drop_constraint("uq_work_order_idempotency_key", "work_order", type_="unique")
    op.drop_column("work_order", "evidence_ref")
    op.drop_column("work_order", "idempotency_key")
    op.drop_column("work_order", "origin_station")
    op.drop_table("pending_proposal")
