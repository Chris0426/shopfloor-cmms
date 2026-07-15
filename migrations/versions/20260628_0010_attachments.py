"""attachment slice #7: polymorphic media pointers (binaries on R2, pointers in PG)

Revision ID: 0010_attachments
Revises: 0009_asset_composition
Create Date: 2026-06-28

對應 docs/domain-model/07-attachments.md、ARCHITECTURE.md ADR-019(app→R2 媒體耦合)。手寫。
- attachment_owner_type lookup(inventory_item / work_order / asset),種子固定 3 值。
- attachment:多型指標表(owner_type FK→lookup + owner_id 文字軟參照;無 hard FK,
  因 owner_id 依 owner_type 指向不同主表;owner 存在性由 domain service 把關)。
- 唯一性 uq(owner_type, owner_id, r2_key):防同 owner 同 key 重複(loader 冪等基礎;
  r2_key 內嵌 sha8 → 同 owner 同內容去重、不同內容開新列,支援 1:N 多圖)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_attachments"
down_revision: str | None = "0009_asset_composition"
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
        "attachment_owner_type",
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )
    # 種子固定 lookup(FK 目標;確保非載入器路徑與部署不 FK-fail,比照 0009 / 0001)。
    op.bulk_insert(
        sa.table(
            "attachment_owner_type",
            sa.column("code", sa.String),
            sa.column("label", sa.String),
        ),
        [
            {"code": "inventory_item", "label": "備品 / 耗材品項照片(owner_id = item_code)"},
            {"code": "work_order", "label": "工單照片 / 文件(owner_id = work_order_no 文字)"},
            {"code": "asset", "label": "設備主檔照片(owner_id = asset_id / EID)"},
        ],
    )

    op.create_table(
        "attachment",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_type",
            sa.String(),
            sa.ForeignKey("attachment_owner_type.code"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(), nullable=False),  # 多型軟參照(無 hard FK)
        sa.Column("r2_bucket", sa.String(), nullable=False),
        sa.Column("r2_key", sa.String(), nullable=False),  # <prefix>/<OWNER_ID>/<sha8>.<ext>
        sa.Column("content_type", sa.String(), nullable=False),  # image/jpeg, image/png
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),  # 內容識別(hex)
        sa.Column("original_filename", sa.String(), nullable=True),  # 來源檔名(溯源)
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        *_audit_columns(),
    )

    # 防同 owner 同 key 重複(loader 冪等基礎);內嵌 sha8 → 等同同 owner 內容去重。
    op.create_index(
        "uq_attachment_owner_key",
        "attachment",
        ["owner_type", "owner_id", "r2_key"],
        unique=True,
    )
    # list_attachments(owner_type, owner_id) 熱路徑
    op.create_index("ix_attachment_owner", "attachment", ["owner_type", "owner_id"])


def downgrade() -> None:
    op.drop_index("ix_attachment_owner", table_name="attachment")
    op.drop_index("uq_attachment_owner_key", table_name="attachment")
    op.drop_table("attachment")
    op.drop_table("attachment_owner_type")
