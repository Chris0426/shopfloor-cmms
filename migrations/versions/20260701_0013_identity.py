"""identity slice: 本地帳號 + server-side session + RBAC(ADR-022)

Revision ID: 0013_identity
Revises: 0012_work_order_note
Create Date: 2026-07-01

cmms 自建本地帳密(argon2id)+ DB-backed opaque session(可即時撤銷)。手寫。
- user_account:user_id PK(= human:<id>)、username unique、role(admin/engineer)、
  is_active、ui_locale/jira_output_locale(ADR-023,預設 en)。
- user_session:session_token PK、user_id FK、expires_at、revoked_at(即時撤銷)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_identity"
down_revision: str | None = "0012_work_order_note"
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
        "user_account",
        sa.Column("user_id", sa.String(), primary_key=True),  # = human:<id> 的 <id>
        sa.Column("username", sa.String(), nullable=False, unique=True),  # 登入帳號
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),  # argon2id
        sa.Column("org", sa.String(), nullable=False),  # plant / contractor
        sa.Column("role", sa.String(), nullable=False, server_default=sa.text("'engineer'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("ui_locale", sa.String(), nullable=False, server_default=sa.text("'en'")),
        sa.Column(
            "jira_output_locale", sa.String(), nullable=False, server_default=sa.text("'en'")
        ),
        *_audit_columns(),
    )

    op.create_table(
        "user_session",
        sa.Column("session_token", sa.String(), primary_key=True),  # opaque(token_urlsafe)
        sa.Column(
            "user_id", sa.String(), sa.ForeignKey("user_account.user_id"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),  # 即時撤銷
    )
    op.create_index("ix_user_session_user_id", "user_session", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_user_session_user_id", table_name="user_session")
    op.drop_table("user_session")
    op.drop_table("user_account")
