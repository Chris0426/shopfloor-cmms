"""asset composition graph (ADR-018): machine<->module tree + shared-resource graph

Revision ID: 0009_asset_composition
Revises: 0008_work_order_gated_write
Create Date: 2026-06-25

對應 docs/domain-model/01-assets.md §1.7 / §8、ARCHITECTURE.md ADR-018。手寫。
- asset_relationship_type lookup(contains_module / shared_dependency)。
- asset_relationship:typed N:M 組成圖(from/to/type/source/temporal + 稽核);
  partial unique(同型同對只一條 valid_to IS NULL 現行邊)+ no-self-loop CHECK。
D2=(a):不鑄合成 id、asset 主檔不動(不加 identity_source)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_asset_composition"
down_revision: str | None = "0008_work_order_gated_write"
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
    op.create_table(
        "asset_relationship_type",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )
    # 種子固定 lookup(ADR-018 固定 2 值、無資料源)— 比照 0001 external_id_namespace。
    # FK 目標 asset_relationship.relationship_type:確保非載入器路徑(直接 link_*)與部署不 FK-fail。
    op.bulk_insert(
        sa.table(
            "asset_relationship_type",
            sa.column("code", sa.String),
            sa.column("label", sa.String),
        ),
        [
            {"code": "contains_module", "label": "機台內含模組(containment,1:N 樹)"},
            {"code": "shared_dependency", "label": "共用資源服務機台(N:M 圖)"},
        ],
    )

    op.create_table(
        "asset_relationship",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "from_asset_id",
            sa.String(),
            sa.ForeignKey("asset.asset_id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "to_asset_id", sa.String(), sa.ForeignKey("asset.asset_id"), nullable=False, index=True
        ),
        sa.Column(
            "relationship_type",
            sa.String(),
            sa.ForeignKey("asset_relationship_type.code"),
            nullable=False,
        ),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("from_asset_id <> to_asset_id", name="ck_asset_relationship_no_self"),
        *_audit_columns(),
    )

    # 同型同對只允許一條現行邊(valid_to IS NULL);保留歷史(已關閉的邊不受限)。
    op.create_index(
        "uq_asset_relationship_active",
        "asset_relationship",
        ["from_asset_id", "to_asset_id", "relationship_type"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_asset_relationship_active", table_name="asset_relationship")
    op.drop_table("asset_relationship")
    op.drop_table("asset_relationship_type")
