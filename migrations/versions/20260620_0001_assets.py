"""asset slice #1: asset + lookups + identity crosswalk

Revision ID: 0001_assets
Revises:
Create Date: 2026-06-20

對應 docs/domain-model/01-assets.md §8 與 ADR-015。手寫(本機無 DB 可 autogenerate)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_assets"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _code_label(name: str) -> None:
    op.create_table(
        name,
        sa.Column("code", sa.String(), primary_key=True),
        sa.Column("label", sa.String(), nullable=False),
    )


def _audit_columns() -> list[sa.Column]:
    # 與 cmms.audit.AuditMixin 對齊(ADR-005/016)
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
    # lookups
    _code_label("asset_type")
    _code_label("department")
    _code_label("line")
    _code_label("external_id_namespace")

    # asset 主表
    op.create_table(
        "asset",
        sa.Column("asset_id", sa.String(), primary_key=True),
        sa.Column("description", sa.String(), nullable=False),
        sa.Column("parent_asset_id", sa.String(), sa.ForeignKey("asset.asset_id"), nullable=True),
        sa.Column("asset_type", sa.String(), sa.ForeignKey("asset_type.code"), nullable=False),
        sa.Column("asset_subtype", sa.String(), nullable=True),
        sa.Column("department", sa.String(), sa.ForeignKey("department.code"), nullable=True),
        sa.Column("process_segment_class", sa.String(), nullable=True),
        sa.Column("line", sa.String(), sa.ForeignKey("line.code"), nullable=True),
        sa.Column("site", sa.String(), nullable=False),
        sa.Column("building", sa.String(), nullable=True),
        sa.Column("floor_level", sa.String(), nullable=True),
        sa.Column("room_space", sa.String(), nullable=True),
        sa.Column("manufacturer", sa.String(), nullable=True),
        sa.Column("model_no", sa.String(), nullable=True),
        sa.Column("serial_no", sa.String(), nullable=True),
        sa.Column(
            "available_for_service", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("up_down_tracking", sa.Boolean(), nullable=True),
        sa.Column("host_name", sa.String(), nullable=True),
        sa.Column("asset_ref", sa.String(), nullable=True),
        sa.Column("product", sa.String(), nullable=True),
        sa.Column("weblink", sa.String(), nullable=True),
        sa.Column("picture_url", sa.String(), nullable=True),
        sa.Column("comments", sa.Text(), nullable=True),
        *_audit_columns(),
    )

    # 身分 crosswalk(ADR-015)
    op.create_table(
        "asset_external_id",
        sa.Column(
            "namespace", sa.String(), sa.ForeignKey("external_id_namespace.code"), primary_key=True
        ),
        sa.Column("external_id", sa.String(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("asset.asset_id"), nullable=False),
        *_audit_columns(),
        sa.UniqueConstraint("asset_id", "namespace", "external_id"),
    )

    # 種子:身分 namespace(無資料源,固定值)。其餘 lookup 由載入器依資料種子。
    op.bulk_insert(
        sa.table(
            "external_id_namespace",
            sa.column("code", sa.String),
            sa.column("label", sa.String),
        ),
        [
            {"code": "mes_equipment", "label": "MES equipment id"},
            {"code": "layer_b_sensor", "label": "Analytics Layer B 物理感測器 id"},
        ],
    )


def downgrade() -> None:
    op.drop_table("asset_external_id")
    op.drop_table("asset")
    op.drop_table("external_id_namespace")
    op.drop_table("line")
    op.drop_table("department")
    op.drop_table("asset_type")
