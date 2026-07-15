"""inventory slice #5: inventory_item + item_category/asset_subtype lookups + junctions

Revision ID: 0005_inventory
Revises: 0004_work_orders
Create Date: 2026-06-21

對應 docs/domain-model/04-inventory.md §8。手寫。A3:asset_subtype canonical lookup(asset +
inventory 共用參照;asset.asset_subtype 維持 text 軟參照,不 retrofit FK)。多值欄拆 3 junction。
uom / supplier→company FK / stock_transaction 延後。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_inventory"
down_revision: str | None = "0004_work_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _code_label(name: str) -> None:
    op.create_table(
        name,
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )


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


def _junction(name: str, col_a: str, col_b: str) -> None:
    op.create_table(
        name,
        sa.Column(col_a, sa.String(), sa.ForeignKey("inventory_item.item_code"), primary_key=True),
        sa.Column(col_b, sa.String(), primary_key=True),
        *_audit_columns(),
    )


def upgrade() -> None:
    _code_label("item_category")  # ES / EC(legacy 前綴)
    _code_label("asset_subtype")  # canonical 子類型(A3)

    op.create_table(
        "inventory_item",
        sa.Column("item_code", sa.String(), primary_key=True),
        sa.Column("item_category", sa.String(), sa.ForeignKey("item_category.code"), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("vendor_part_no", sa.String(), nullable=True),
        sa.Column("quantity_on_hand", sa.Numeric(12, 3), nullable=True),
        sa.Column("reorder_point", sa.Numeric(12, 3), nullable=True),
        sa.Column("lead_time_weeks", sa.Integer(), nullable=True),
        sa.Column("unit_cost", sa.Numeric(14, 5), nullable=True),
        sa.Column("currency", sa.String(), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("bin_location", sa.String(), nullable=True),
        sa.Column("supplier", sa.String(), nullable=True),  # FK→company 延 Contacts 切片
        sa.Column("weblink", sa.String(), nullable=True),
        sa.Column("photo_ref", sa.String(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("is_stocked", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_obsolete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_audit_columns(),
    )

    # 多值 junction(asset_subtype FK→asset_subtype;alt/kit 自參照 FK→inventory_item)
    op.create_table(
        "inventory_item_asset_subtype",
        sa.Column(
            "item_code", sa.String(), sa.ForeignKey("inventory_item.item_code"), primary_key=True
        ),
        sa.Column(
            "asset_subtype", sa.String(), sa.ForeignKey("asset_subtype.code"), primary_key=True
        ),
        *_audit_columns(),
    )
    op.create_table(
        "inventory_item_alternative",
        sa.Column(
            "item_code", sa.String(), sa.ForeignKey("inventory_item.item_code"), primary_key=True
        ),
        sa.Column(
            "alt_item_code",
            sa.String(),
            sa.ForeignKey("inventory_item.item_code"),
            primary_key=True,
        ),
        *_audit_columns(),
    )
    op.create_table(
        "inventory_item_kit",
        sa.Column(
            "parent_item_code",
            sa.String(),
            sa.ForeignKey("inventory_item.item_code"),
            primary_key=True,
        ),
        sa.Column(
            "child_item_code",
            sa.String(),
            sa.ForeignKey("inventory_item.item_code"),
            primary_key=True,
        ),
        *_audit_columns(),
    )


def downgrade() -> None:
    op.drop_table("inventory_item_kit")
    op.drop_table("inventory_item_alternative")
    op.drop_table("inventory_item_asset_subtype")
    op.drop_table("inventory_item")
    op.drop_table("asset_subtype")
    op.drop_table("item_category")
