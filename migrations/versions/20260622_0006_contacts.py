"""contacts slice #6: organization + person + person_alias + org_type/contact_category lookups

Revision ID: 0006_contacts
Revises: 0005_inventory
Create Date: 2026-06-22

對應 docs/domain-model/06-contacts.md §9。手寫。
- organization(org_id = company slug 代理鍵)+ person(person_id = contactid)+ person_alias
  (保守去重:別名 contactid → canonical)。
- lookup:org_type(Supplier/Contractor/Customer/Internal)、contact_category(eMaint 原始分類)。
- 軟參照(supplier / assigned_person / closed_by)**不在此 retrofit FK**;app_user 延 #4b。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_contacts"
down_revision: str | None = "0005_inventory"
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


def upgrade() -> None:
    _code_label("org_type")  # Supplier / Contractor / Customer / Internal
    _code_label("contact_category")  # eMaint 原始分類:Supplier / Employee / Customer

    op.create_table(
        "organization",
        sa.Column("org_id", sa.String(), primary_key=True),  # company slug 代理鍵
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("org_type", sa.String(), sa.ForeignKey("org_type.code"), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("website", sa.String(), nullable=True),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("phone", sa.String(), nullable=True),
        *_audit_columns(),
    )

    op.create_table(
        "person",
        sa.Column("person_id", sa.String(), primary_key=True),  # contactid
        sa.Column("org_id", sa.String(), sa.ForeignKey("organization.org_id"), nullable=False),
        sa.Column("category", sa.String(), sa.ForeignKey("contact_category.code"), nullable=True),
        sa.Column("first_name", sa.String(), nullable=True),
        sa.Column("last_name", sa.String(), nullable=True),
        sa.Column("full_name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("work_phone", sa.String(), nullable=True),
        sa.Column("extension", sa.String(), nullable=True),
        sa.Column("mobile", sa.String(), nullable=True),
        sa.Column("work_address", sa.Text(), nullable=True),
        *_audit_columns(),
    )

    op.create_table(
        "person_alias",
        sa.Column("alias_contact_id", sa.String(), primary_key=True),  # SMWU / NOPT
        sa.Column(
            "person_id", sa.String(), sa.ForeignKey("person.person_id"), nullable=False
        ),  # canonical:SAMWU99 / NOPTIC
        *_audit_columns(),
    )


def downgrade() -> None:
    op.drop_table("person_alias")
    op.drop_table("person")
    op.drop_table("organization")
    op.drop_table("contact_category")
    op.drop_table("org_type")
