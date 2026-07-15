"""agent forwarding governance core: PAT vault + WO↔MRQ link + MCP scoped token(ADR-020/022 §5)

Revision ID: 0017_agent_forwarding
Revises: 0016_task_steps
Create Date: 2026-07-02

ADR-020 接 agent 的治理地基(cmms 端;live Jira/gateway 為 blocked 外層)。additive 三表:
- user_external_credential:per-user Jira PAT 保管庫(封套加密,DB 只存密文;可撤;限本人取用)。
- mcp_scoped_token:web session 衍生的 MCP scoped token(authN/authZ;帶委派 human:<id>)。
- work_order_external_link:工單↔MRQ 連結(N:M;冪等唯一鍵;cmms 不呼叫 Jira、只記連了什麼,決策 1)。
對既有列零變更。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_agent_forwarding"
down_revision: str | None = "0016_task_steps"
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
        "user_external_credential",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "user_id", sa.String(), sa.ForeignKey("user_account.user_id"), nullable=False
        ),
        sa.Column("system", sa.String(), nullable=False),
        sa.Column("secret_ciphertext", sa.Text(), nullable=False),  # Fernet 密文(明文永不入庫)
        sa.Column("key_version", sa.String(), nullable=False, server_default=sa.text("'v1'")),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
    )
    op.create_index(
        "ix_user_external_credential_user_id", "user_external_credential", ["user_id"]
    )
    op.create_index(
        "uq_user_external_credential_active",
        "user_external_credential",
        ["user_id", "system"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "mcp_scoped_token",
        sa.Column("token", sa.String(), primary_key=True),
        sa.Column(
            "user_id", sa.String(), sa.ForeignKey("user_account.user_id"), nullable=False
        ),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_mcp_scoped_token_user_id", "mcp_scoped_token", ["user_id"])

    op.create_table(
        "work_order_external_link",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "work_order_no",
            sa.BigInteger(),
            sa.ForeignKey("work_order.work_order_no"),
            nullable=False,
        ),
        sa.Column("system", sa.String(), nullable=False),
        sa.Column("external_key", sa.String(), nullable=False),
        sa.Column("link_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        *_audit_columns(),
    )
    op.create_index(
        "ix_work_order_external_link_work_order_no",
        "work_order_external_link",
        ["work_order_no"],
    )
    op.create_index(
        "uq_wo_external_link",
        "work_order_external_link",
        ["work_order_no", "system", "external_key", "link_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_wo_external_link", table_name="work_order_external_link")
    op.drop_index(
        "ix_work_order_external_link_work_order_no", table_name="work_order_external_link"
    )
    op.drop_table("work_order_external_link")
    op.drop_index("ix_mcp_scoped_token_user_id", table_name="mcp_scoped_token")
    op.drop_table("mcp_scoped_token")
    op.drop_index(
        "uq_user_external_credential_active", table_name="user_external_credential"
    )
    op.drop_index(
        "ix_user_external_credential_user_id", table_name="user_external_credential"
    )
    op.drop_table("user_external_credential")
