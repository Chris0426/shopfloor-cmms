"""telegram bridge:綁定碼 + chat_id↔user 連結 + webhook 冪等去重

Revision ID: 0034_telegram_bridge
Revises: 0033_line_1k
Create Date: 2026-07-12

續-15(Jordan 拍板):把 cmms hermes dock 助理能力搬上 Telegram DM;工程師不登入 cmms 亦
可對 bot 提問。DM 模式(非群組):一人一 chat_id、綁定即回填通知 chat_id。

- `telegram_link`:(`user_id` PK FK→user_account)一人一 DM 綁定;`chat_id` UNIQUE NOT NULL
  —— 一個 Telegram chat 只能綁一位使用者(重綁=REPLACE 自己那筆;撞他人 → 誠實拒)。audit 欄。
- `telegram_link_code`:一次性綁定碼。`code_hash` PK(sha256 hex,**明文不落庫**);`user_id`
  FK(加 index,作廢舊碼查得快);`expires_at`(TTL 10 分鐘)、`used_at` nullable(兌換即標)。
  audit 欄。
- `telegram_update_seen`:webhook 冪等去重。`update_id` BigInteger PK(Telegram 更新序號);
  `received_at` server_default now()。Telegram 可能重送 → 兌換/處理前 INSERT ON CONFLICT
  DO NOTHING,已見過即 skip;opportunistic prune(>7 天)。不需完整 audit 欄。

downgrade = drop 三表(反序)。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_telegram_bridge"
down_revision: str | None = "0033_line_1k"
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
        "telegram_link",
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("chat_id", sa.String(), nullable=False),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.user_id"]),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("chat_id", name="uq_telegram_link_chat_id"),
    )

    op.create_table(
        "telegram_link_code",
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        *_audit_columns(),
        sa.ForeignKeyConstraint(["user_id"], ["user_account.user_id"]),
        sa.PrimaryKeyConstraint("code_hash"),
    )
    op.create_index(
        "ix_telegram_link_code_user", "telegram_link_code", ["user_id"], unique=False
    )

    op.create_table(
        "telegram_update_seen",
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("update_id"),
    )


def downgrade() -> None:
    op.drop_table("telegram_update_seen")
    op.drop_index("ix_telegram_link_code_user", table_name="telegram_link_code")
    op.drop_table("telegram_link_code")
    op.drop_table("telegram_link")
