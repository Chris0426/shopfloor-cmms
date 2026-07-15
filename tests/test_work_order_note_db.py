"""work_order_note + 照片掛 note 的 DB 整合(§1.6;ADR-019/020;testcontainers)。本機無 Docker skip。

蓋 web monkeypatch 測不到的服務真 DB 行為:add_note 落庫 / 冪等 / list_notes 排序;
AttachmentService 對 owner_type='work_order_note' 的 _owner_exists + 拒孤兒。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.attachment.models import AttachmentOwnerType  # noqa: E402
from cmms.domain.attachment.service import AttachmentError, AttachmentService  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.models import WoNoteType, WorkType  # noqa: E402
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402
from cmms.storage import InMemoryStorageBackend  # noqa: E402

ACTOR = Actor.human("tester")
ASSET_ROWS = [
    {
        "compid": "EID-001", "comp_desc": "Rig 1", "assettype": "Production",
        "department": "EQ", "line_no": "10K", "available": "Yes",
    }
]


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load_assets(ASSET_ROWS, s)
            svc = WorkOrderService(s)
            async with svc.write(ACTOR):  # 種 create_all 未含的 lookup 列(migration 才 bulk_insert)
                await svc.upsert_lookup(WorkType, "REACTIVE", "Reactive")
                await svc.upsert_status(
                    "OPEN", "Open", rank=1, is_terminal=False, is_downtime=True
                )
                await svc.upsert_lookup(WoNoteType, "report", "報修")
                await svc.upsert_lookup(WoNoteType, "progress", "進度")
                s.add(AttachmentOwnerType(code="work_order_note", label="工單日誌照片"))
            yield s
        await engine.dispose()


async def test_add_note_roundtrip_and_idempotency(session) -> None:
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, brief_description="fault"
    )

    note = await svc.add_note(wo.work_order_no, entry_type="report", body="初始故障", actor=ACTOR)
    assert note.id is not None
    assert note.author == "human:tester"
    assert note.source_actor == "human:tester"
    assert note.occurred_at is not None

    await svc.add_note(wo.work_order_no, entry_type="progress", body="拆檢中", actor=ACTOR)
    notes = await svc.list_notes(wo.work_order_no)
    assert [n.body for n in notes] == ["初始故障", "拆檢中"]  # 依 occurred_at 升冪

    # 冪等:同 idempotency_key 回既有、不重記、不覆寫
    a = await svc.add_note(
        wo.work_order_no, entry_type="progress", body="x", actor=ACTOR, idempotency_key="k1"
    )
    b = await svc.add_note(
        wo.work_order_no, entry_type="progress", body="y", actor=ACTOR, idempotency_key="k1"
    )
    assert a.id == b.id and b.body == "x"
    assert len(await svc.list_notes(wo.work_order_no)) == 3


async def test_photo_attaches_to_note(session) -> None:
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, brief_description="fault"
    )
    note = await svc.add_note(wo.work_order_no, entry_type="report", body="故障", actor=ACTOR)

    att_svc = AttachmentService(session, backend=InMemoryStorageBackend())
    att, created = await att_svc.add_attachment(
        owner_type="work_order_note", owner_id=str(note.id),
        data=b"\xff\xd8jpgbytes", ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    assert created is True
    assert att.owner_type == "work_order_note"
    assert att.owner_id == str(note.id)
    found = await att_svc.list_attachments("work_order_note", str(note.id))
    assert len(found) == 1

    # 不存在的 note id → 拒記孤兒(_owner_exists 對 work_order_note 生效)
    with pytest.raises(AttachmentError):
        await att_svc.add_attachment(
            owner_type="work_order_note", owner_id="999999",
            data=b"x", ext="jpg", content_type="image/jpeg", actor=ACTOR,
        )


async def test_ai_candidate_note_type(session) -> None:
    """峰會 D11(migration 0021):`ai_candidate` 為受控 wo_note_type 值,
    AI agent(source_actor=agent:<name>)可對其 add_note 落庫。"""
    svc = WorkOrderService(session)
    # migration 0021 才 bulk_insert;此 fixture 走 create_all,故就地種該受控值
    async with svc.write(Actor.human("seed")):
        await svc.upsert_lookup(WoNoteType, "ai_candidate", "AI 候選(未確認)")

    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, brief_description="fault"
    )
    agent = Actor.agent("analytics-hermes")
    note = await svc.add_note(
        wo.work_order_no,
        entry_type="ai_candidate",
        body="evidence: MES-EID-001-2026\n建議更換軸承(AI 候選)",
        actor=agent,
    )
    assert note.entry_type == "ai_candidate"
    assert note.author == "agent:analytics-hermes"
    assert note.source_actor == "agent:analytics-hermes"

    types = {t.code for t in await svc.list_note_types()}
    assert "ai_candidate" in types
