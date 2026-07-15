"""RFQ / procurement slice: reorder_quantity + supplier↔org + person.is_main + rfq tables(ADR-026)

Revision ID: 0018_rfq_procurement
Revises: 0017_agent_forwarding
Create Date: 2026-07-02

additive:
- inventory_item += reorder_quantity(orderqty;缺→RFQ 退回 reorder_point−on_hand)
  + supplier_org_id(FK→organization;由 supplier 文字解析,對不上留 NULL、RFQ-ineligible)。
- person += is_main(一機構一位主要聯絡人;RFQ 收件人優先其 email)。
- rfq_request + rfq_request_line(詢價 governed 落庫;status drafted/sent/failed;idempotent)。
對既有列零變更(新欄 nullable / is_main default false)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_rfq_procurement"
down_revision: str | None = "0017_agent_forwarding"
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
    op.add_column("inventory_item", sa.Column("reorder_quantity", sa.Numeric(12, 3), nullable=True))
    op.add_column(
        "inventory_item",
        sa.Column(
            "supplier_org_id", sa.String(), sa.ForeignKey("organization.org_id"), nullable=True
        ),
    )
    op.create_index(
        "ix_inventory_item_supplier_org_id", "inventory_item", ["supplier_org_id"]
    )
    op.add_column(
        "person",
        sa.Column("is_main", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "rfq_request",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "supplier_org_id", sa.String(), sa.ForeignKey("organization.org_id"), nullable=False
        ),
        sa.Column("recipient_email", sa.String(), nullable=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'drafted'")),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        *_audit_columns(),
    )
    op.create_index("ix_rfq_request_supplier_org_id", "rfq_request", ["supplier_org_id"])
    op.create_index("uq_rfq_idem", "rfq_request", ["idempotency_key"], unique=True)

    op.create_table(
        "rfq_request_line",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("rfq_id", sa.BigInteger(), sa.ForeignKey("rfq_request.id"), nullable=False),
        sa.Column(
            "item_code", sa.String(), sa.ForeignKey("inventory_item.item_code"), nullable=False
        ),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        *_audit_columns(),
    )
    op.create_index("ix_rfq_line_rfq", "rfq_request_line", ["rfq_id"])


def downgrade() -> None:
    op.drop_index("ix_rfq_line_rfq", table_name="rfq_request_line")
    op.drop_table("rfq_request_line")
    op.drop_index("uq_rfq_idem", table_name="rfq_request")
    op.drop_index("ix_rfq_request_supplier_org_id", table_name="rfq_request")
    op.drop_table("rfq_request")
    op.drop_column("person", "is_main")
    op.drop_index("ix_inventory_item_supplier_org_id", table_name="inventory_item")
    op.drop_column("inventory_item", "supplier_org_id")
    op.drop_column("inventory_item", "reorder_quantity")
