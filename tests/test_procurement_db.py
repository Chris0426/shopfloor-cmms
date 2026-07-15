"""RFQ / procurement DB 測試(ADR-026;testcontainers)。無 Docker 自動 skip。

驗:autolink_suppliers(名稱配對)、resolve_supplier_email(is_main + fallback)、set_main_contact、
create_rfq(dry-run 草稿 + 數量 fallback、live 經 InMemory sender、冪等、無收件人→failed)、
draft_below_safety_stock(依 org 分組、只納已連 org)。需全 model(inventory→organization FK)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.contacts.models import Organization, Person  # noqa: E402
from cmms.domain.contacts.service import ContactsService  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.models import InventoryItem  # noqa: E402
from cmms.domain.inventory.service import InventoryService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.procurement import models as _proc_models  # noqa: E402, F401
from cmms.domain.procurement.models import RfqRequestLine  # noqa: E402
from cmms.domain.procurement.service import ProcurementService  # noqa: E402
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.email import InMemoryEmailSender  # noqa: E402

OP = Actor.human("op")


def _item(code: str, supplier: str, rp: str, oh: str, rq: str | None = None) -> InventoryItem:
    return InventoryItem(
        item_code=code, supplier=supplier, reorder_point=Decimal(rp),
        quantity_on_hand=Decimal(oh), reorder_quantity=(Decimal(rq) if rq else None),
        created_by="test",
    )


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            s.add(Organization(org_id="NORDIC", name="NORDIC GmbH", created_by="t"))
            s.add(Organization(org_id="EMPTY", name="Empty Co", created_by="t"))
            s.add(Person(person_id="HANS", org_id="NORDIC", email="hans@nordic.example.com",
                         full_name="Hans", created_by="t"))
            s.add(Person(person_id="OTTO", org_id="NORDIC", email="otto@nordic.example.com",
                         full_name="Otto", created_by="t"))
            s.add(_item("ES001", "NORDIC GmbH", "5", "2", "10"))  # rq=10
            s.add(_item("ES002", "NORDIC GmbH", "5", "1"))  # 無 rq → fallback 5-1=4
            s.add(_item("ES003", "Unknown Co", "5", "1"))  # supplier 對不上 → unmatched
            s.add(_item("ES004", "Empty Co", "5", "1"))  # 連 EMPTY(無 persons)
            await s.commit()
            yield s
        await engine.dispose()


async def test_autolink_suppliers(session) -> None:
    linked, unmatched = await InventoryService(session).autolink_suppliers(OP)
    assert linked == 3 and unmatched == 1  # ES001/2/4 連上;ES003(Unknown Co)對不上
    assert (await session.get(InventoryItem, "ES001")).supplier_org_id == "NORDIC"
    assert (await session.get(InventoryItem, "ES003")).supplier_org_id is None


async def test_resolve_email_main_then_fallback(session) -> None:
    proc = ProcurementService(session)
    # 未設 main → fallback 最小 person_id 有 email = HANS
    assert await proc.resolve_supplier_email("NORDIC") == "hans@nordic.example.com"
    await ContactsService(session).set_main_contact("NORDIC", "OTTO", OP)
    assert await proc.resolve_supplier_email("NORDIC") == "otto@nordic.example.com"  # is_main 優先


async def test_create_rfq_dry_run_qty(session) -> None:
    await InventoryService(session).autolink_suppliers(OP)
    proc = ProcurementService(session)
    rfq = await proc.create_rfq(
        supplier_org_id="NORDIC", item_codes=["ES001", "ES002"], actor=OP, dry_run=True
    )
    assert rfq.status == "drafted" and rfq.recipient_email == "hans@nordic.example.com"
    lines = list(
        (await session.scalars(select(RfqRequestLine).where(RfqRequestLine.rfq_id == rfq.id))).all()
    )
    qty = {ln.item_code: ln.quantity for ln in lines}
    assert qty["ES001"] == Decimal("10")  # reorder_quantity
    assert qty["ES002"] == Decimal("4")  # fallback reorder_point−on_hand = 5−1


async def test_create_rfq_live_and_idempotent(session) -> None:
    await InventoryService(session).autolink_suppliers(OP)
    proc = ProcurementService(session)
    snd = InMemoryEmailSender()
    rfq = await proc.create_rfq(
        supplier_org_id="NORDIC", item_codes=["ES001"], actor=OP,
        dry_run=False, sender=snd, idempotency_key="k1",
    )
    assert rfq.status == "sent" and rfq.provider_message_id == "mem-1"
    assert len(snd.sent) == 1 and snd.sent[0]["to"] == "hans@nordic.example.com"
    rfq2 = await proc.create_rfq(
        supplier_org_id="NORDIC", item_codes=["ES001"], actor=OP,
        dry_run=False, sender=snd, idempotency_key="k1",
    )
    assert rfq2.id == rfq.id and len(snd.sent) == 1  # 冪等:不重送


async def test_no_recipient_fails(session) -> None:
    await InventoryService(session).autolink_suppliers(OP)  # ES004 → EMPTY(無 persons)
    proc = ProcurementService(session)
    rfq = await proc.create_rfq(
        supplier_org_id="EMPTY", item_codes=["ES004"], actor=OP, dry_run=False,
        sender=InMemoryEmailSender(),
    )
    assert rfq.status == "failed" and "recipient" in (rfq.error or "")


async def test_draft_below_safety_stock_grouped(session) -> None:
    await InventoryService(session).autolink_suppliers(OP)
    drafts = await ProcurementService(session).draft_below_safety_stock()
    by_org = {d.supplier_org_id: d for d in drafts}
    assert set(by_org) == {"NORDIC", "EMPTY"}  # 已連 org 且低於安全庫存;ES003(未連)不入
    assert {c for c, _ in by_org["NORDIC"].lines} == {"ES001", "ES002"}
