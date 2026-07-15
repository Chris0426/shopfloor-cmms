"""Attachment ORM models(對應 docs/domain-model/07-attachments.md)。

- lookup:`attachment_owner_type`(code/label,比照其他切片 _CodeLabel)。
- 主表 `attachment`:多型指標(owner_type + owner_id 軟參照)+ R2 座標 + 內容指紋 + 稽核。
  唯一鍵 (owner_type, owner_id, r2_key) 與熱路徑索引 (owner_type, owner_id) 由 migration 0010 建。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class AttachmentOwnerType(Base):
    __tablename__ = "attachment_owner_type"  # inventory_item / work_order / asset

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class Attachment(AuditMixin, Base):
    __tablename__ = "attachment"
    # 與 migration 0010 對齊(create_all 路徑 / on_conflict 需此唯一鍵;否則 alembic check 漂移):
    # 唯一鍵 (owner_type, owner_id, r2_key) = loader 冪等基礎;(owner_type, owner_id) = list 熱路徑。
    __table_args__ = (
        Index("uq_attachment_owner_key", "owner_type", "owner_id", "r2_key", unique=True),
        Index("ix_attachment_owner", "owner_type", "owner_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    owner_type: Mapped[str] = mapped_column(
        ForeignKey("attachment_owner_type.code"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String, nullable=False)  # 多型軟參照(canonical 大寫)
    r2_bucket: Mapped[str] = mapped_column(String, nullable=False)
    r2_key: Mapped[str] = mapped_column(String, nullable=False)  # <prefix>/<OWNER_ID>/<sha8>.<ext>
    content_type: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
