"""Procurement ORM models(ADR-026;migration 0018)。

RFQ 詢價:對某供應商送一封含多品項的詢價信。governed + 冪等 + 稽核。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import BigInteger, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class RfqRequest(AuditMixin, Base):
    """一次詢價(對某供應商 org)。status:drafted(草稿/agent dry-run/未送)→ sent(已送)/ failed。

    recipient 由 supplier org 的 is_main person email 解析(fallback 最小 person_id 有 email 者)。
    冪等 idempotency_key(agent/批次觸發防重)。明文憑證無關(RFQ 走 SMTP,非 PAT)。
    """

    __tablename__ = "rfq_request"
    __table_args__ = (Index("uq_rfq_idem", "idempotency_key", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    supplier_org_id: Mapped[str] = mapped_column(
        ForeignKey("organization.org_id"), nullable=False, index=True
    )
    recipient_email: Mapped[str | None] = mapped_column(String, nullable=True)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'drafted'")
    )  # drafted / sent / failed
    provider_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)


class RfqRequestLine(AuditMixin, Base):
    """RFQ 明細:一品項 + 詢價數量(reorder_quantity 或 reorder_point−on_hand)。"""

    __tablename__ = "rfq_request_line"
    __table_args__ = (Index("ix_rfq_line_rfq", "rfq_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    rfq_id: Mapped[int] = mapped_column(ForeignKey("rfq_request.id"), nullable=False)
    item_code: Mapped[str] = mapped_column(ForeignKey("inventory_item.item_code"), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)
