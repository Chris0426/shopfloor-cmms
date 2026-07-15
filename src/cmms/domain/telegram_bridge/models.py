"""telegram_bridge ORM models(對應 migration 0034)。

- `TelegramLink`:一人一 DM 綁定(user_id PK FK→user_account、chat_id UNIQUE NOT NULL)。
  重綁 = REPLACE 自己那筆;chat_id 已綁他人 → service 誠實拒。
- `TelegramLinkCode`:一次性綁定碼(code_hash PK,sha256 hex;明文不落庫)。TTL 10 分,
  used_at 兌換即標。舊未用碼於重新產生時作廢(一人同時只一組有效碼)。
- `TelegramUpdateSeen`:webhook 冪等去重(update_id PK)。Telegram 重送 → 已見過即 skip。
  只 mapped 兩欄(不需完整 audit)。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class TelegramLink(AuditMixin, Base):
    __tablename__ = "telegram_link"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), primary_key=True
    )
    # 一個 Telegram chat 只能綁一位使用者(唯一約束;撞他人 → service 拒)
    chat_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)


class TelegramLinkCode(AuditMixin, Base):
    __tablename__ = "telegram_link_code"
    __table_args__ = (Index("ix_telegram_link_code_user", "user_id"),)

    # sha256 hex(明文只顯示一次、絕不落庫)
    code_hash: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TelegramUpdateSeen(Base):
    __tablename__ = "telegram_update_seen"

    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
