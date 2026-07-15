"""assistant ORM models(ADR-020 dock 助理;對話落 DB)。

- `assistant_conversation`:一則對話 = 一個 session。`user_id` FK → user_account,
  擁有權的權威來源(所有查詢按此過濾)。`title` = 首則使用者訊息截短。
  `closed_at` null = 開啟中;user 主動「結束對話」才設(非空 → 不再出現在切換列)。
  排序用 AuditMixin 的 `updated_at`(每輪觸碰),null 時退回 `created_at`。
- `assistant_message`:對話的逐則訊息,`role` ∈ {user, assistant};依 `id` 遞增即時序。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class AssistantConversation(AuditMixin, Base):
    """一則助理對話(session)。擁有權 = user_id(domain 層強制,non-owner 永遠查不到)。"""

    __tablename__ = "assistant_conversation"
    # 熱路徑:列某人的開啟中對話(closed_at IS NULL)並依最近活動排序
    __table_args__ = (Index("ix_assistant_conversation_user", "user_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String, nullable=False)  # 首則使用者訊息截短(~60 字)
    # null = 開啟中;user 主動結束才設。結束後不再出現在切換列,但保留稽核 / 歷史。
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantMessage(AuditMixin, Base):
    """對話的一則訊息。role = user / assistant;id 遞增即時序(同交易寫入 user+assistant)。"""

    __tablename__ = "assistant_message"
    __table_args__ = (Index("ix_assistant_message_conversation", "conversation_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("assistant_conversation.id"), nullable=False
    )
    role: Mapped[str] = mapped_column(String, nullable=False)  # 'user' | 'assistant'
    content: Mapped[str] = mapped_column(Text, nullable=False)
