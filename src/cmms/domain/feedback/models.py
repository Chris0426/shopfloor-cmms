"""feedback ORM models(對應 migration 0035)。

- `HelpFeedback`:說明中心回饋。`id` PK autoincrement;`user_id` FK→user_account(誰留);
  `message` 全文;`resolved_at` / `resolved_by` nullable(admin 標記已處理即填)。audit 欄。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class HelpFeedback(AuditMixin, Base):
    __tablename__ = "help_feedback"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), nullable=False
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # 已處理 = admin 標記(冪等);未處理留 NULL。
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)
