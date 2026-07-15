"""ProcurementService — RFQ 詢價的領域服務(ADR-026;唯一寫入路徑,ADR-001)。

create_rfq:解析收件人 + 組信 + (非 dry_run)經 EmailSender adapter 發送;governed + 冪等 + 稽核。
draft_below_safety_stock:低於安全庫存的品項依 supplier org 分組(預覽/dry-run,agent 面預設)。
live SMTP = blocked(InMemory fallback,見 email.py);此檔只管落庫 + 委派 adapter。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.base import DomainService
from cmms.domain.contacts.models import Organization, Person
from cmms.domain.inventory.models import InventoryItem
from cmms.domain.procurement.models import RfqRequest, RfqRequestLine
from cmms.email import EmailError, EmailSender, get_email_sender


class ProcurementError(Exception):
    """RFQ 領域錯誤(供應商/品項不存在、無明細等)。"""


@dataclass(frozen=True, slots=True)
class RfqDraft:
    """低於安全庫存的一筆 RFQ 候選(依 supplier org 分組;預覽用,不落庫)。"""

    supplier_org_id: str
    supplier_name: str
    recipient_email: str | None
    lines: list[tuple[str, Decimal]]  # (item_code, quantity)


class ProcurementService(DomainService):
    async def resolve_supplier_email(self, org_id: str) -> str | None:
        """RFQ 收件人:org 的 is_main person email;fallback 最小 person_id 有 email 者(決策 c)。"""
        main = await self.session.scalar(
            select(Person.email).where(
                Person.org_id == org_id,
                Person.is_main.is_(True),
                Person.email.is_not(None),
            )
        )
        if main is not None:
            return main
        return await self.session.scalar(
            select(Person.email)
            .where(Person.org_id == org_id, Person.email.is_not(None))
            .order_by(Person.person_id)
            .limit(1)
        )

    @staticmethod
    def _rfq_qty(item: InventoryItem) -> Decimal:
        """詢價數量:優先 reorder_quantity;缺則 max(0, reorder_point − on_hand)(fallback)。"""
        if item.reorder_quantity is not None:
            return item.reorder_quantity
        rp = item.reorder_point or Decimal(0)
        oh = item.quantity_on_hand or Decimal(0)
        return max(Decimal(0), rp - oh)

    async def create_rfq(
        self,
        *,
        supplier_org_id: str,
        item_codes: list[str],
        actor: Actor,
        idempotency_key: str | None = None,
        dry_run: bool = False,
        sender: EmailSender | None = None,
    ) -> RfqRequest:
        """建 RFQ(governed + 冪等)。`dry_run=True`(agent MCP 預設)= 只草稿不送;
        `dry_run=False`(web 一鍵 / admin)= 經 EmailSender 送 → status=sent/failed。"""
        if idempotency_key is not None:
            existing = await self.session.scalar(
                select(RfqRequest).where(RfqRequest.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing
        org = await self.session.get(Organization, supplier_org_id)
        if org is None:
            raise ProcurementError(f"organization {supplier_org_id} not found")
        lines: list[tuple[InventoryItem, Decimal]] = []
        for code in item_codes:
            item = await self.session.get(InventoryItem, code)
            if item is None:
                raise ProcurementError(f"inventory_item {code} not found")
            lines.append((item, self._rfq_qty(item)))
        if not lines:
            raise ProcurementError("RFQ needs at least one item")

        recipient = await self.resolve_supplier_email(supplier_org_id)
        subject = f"[Shopfloor PLANT-1] RFQ — {org.name}"
        body_lines = [f"Dear {org.name},", "", "Please quote the following parts:", ""]
        body_lines += [
            f"  - {item.item_code}  {item.name or item.description or ''}  x {qty}"
            for item, qty in lines
        ]
        body_lines += ["", "Thank you.", "Shopfloor PLANT-1 Maintenance"]
        body = "\n".join(body_lines)

        async with self.write(actor):
            rfq = RfqRequest(
                supplier_org_id=supplier_org_id,
                recipient_email=recipient,
                subject=subject,
                body=body,
                status="drafted",
                idempotency_key=idempotency_key,
                source_actor=actor.value,
                created_by=actor.value,
            )
            self.session.add(rfq)
            await self.session.flush()
            for item, qty in lines:
                self.session.add(
                    RfqRequestLine(
                        rfq_id=rfq.id,
                        item_code=item.item_code,
                        quantity=qty,
                        source_actor=actor.value,
                        created_by=actor.value,
                    )
                )
            if not dry_run:
                if not recipient:
                    rfq.status = "failed"
                    rfq.error = "no recipient email for supplier"
                else:
                    settings = get_settings()
                    try:
                        mid = await (sender or get_email_sender()).send(
                            to=recipient,
                            subject=subject,
                            body=body,
                            from_addr=settings.rfq_from or "cmms@example.com",
                            reply_to=settings.rfq_reply_to,
                        )
                        rfq.status = "sent"
                        rfq.provider_message_id = mid
                    except EmailError as e:
                        rfq.status = "failed"
                        rfq.error = str(e)
            rfq_id = rfq.id
        return await self.session.get(RfqRequest, rfq_id)

    async def draft_below_safety_stock(self, *, limit: int = 200) -> list[RfqDraft]:
        """低於再訂購點的品項依 supplier org 分組(RFQ 候選預覽;僅納入已連 org 者)。讀取/dry-run。"""
        stmt = (
            select(InventoryItem)
            .where(
                InventoryItem.quantity_on_hand.is_not(None),
                InventoryItem.reorder_point.is_not(None),
                InventoryItem.quantity_on_hand < InventoryItem.reorder_point,
                InventoryItem.supplier_org_id.is_not(None),
            )
            .order_by(InventoryItem.supplier_org_id, InventoryItem.item_code)
            .limit(limit)
        )
        by_org: dict[str, list[InventoryItem]] = {}
        for it in (await self.session.scalars(stmt)).all():
            by_org.setdefault(it.supplier_org_id, []).append(it)
        drafts: list[RfqDraft] = []
        for org_id, its in by_org.items():
            org = await self.session.get(Organization, org_id)
            drafts.append(
                RfqDraft(
                    supplier_org_id=org_id,
                    supplier_name=org.name if org else org_id,
                    recipient_email=await self.resolve_supplier_email(org_id),
                    lines=[(it.item_code, self._rfq_qty(it)) for it in its],
                )
            )
        return drafts
