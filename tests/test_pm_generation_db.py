"""PM 生成切片的 DB 整合測試(testcontainers-postgres)。ADR-021。

本機無 Docker 時自動 skip。涵蓋按需生成 PM 工單 + 完成回寫:
- 生成 → PM 工單(work_type=PM / status=OPEN / pm_source_id / 稽核 source_actor=human:<id>);
- 冪等(未結案時重複生成 → 同一張,不新增列);
- 結案後再生成 → 新一張(前一張為終態);
- 完成回寫:結案推進 next_due_date(Fixed,從排定日起算)+ 記 last_pm_date;
- 不週期(frequency_interval=0):結案不推進 next_due_date;
- 到期清單:依到期日過濾 + 排除 suppress;
- suppress 的 PM 仍允許按需生成(真人覆寫)。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.asset.models import Asset  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.pm_schedule.loader import load as load_pm  # noqa: E402
from cmms.domain.pm_schedule.service import PmScheduleService  # noqa: E402
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.task.loader import load as load_tasks  # noqa: E402
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import WO_STATUS_SEED  # noqa: E402
from cmms.domain.work_order.models import WorkType  # noqa: E402
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService  # noqa: E402
from cmms.domain.work_order.transform import TAIPEI  # noqa: E402

ACTOR = Actor.human("tester")

ASSET_ROWS = [
    {
        "compid": "EID-001",
        "comp_desc": "Rig 1",
        "assettype": "Production",
        "department": "EQ",
        "line_no": "10K",
        "available": "Yes",
    },
    {
        "compid": "EID-002",
        "comp_desc": "Rig 2",
        "assettype": "Production",
        "department": "EQ",
        "line_no": "10K",
        "available": "Yes",
    },
]
# 三 task 皆被下方 SA 引用 → T3 不標記 idle;description 供生成時帶入 brief_description
TASK_ROWS = [
    {"task_no": "T-A", "task_desc": "Annual inspection of rig"},
    {"task_no": "T-B", "task_desc": "One-off calibration"},
    {"task_no": "T-C", "task_desc": "Suppressed monthly check"},
]
# P1:週期 12 Months、到期 06/01/26、未停用 → 生成 + Fixed 回寫主場景(lastpmno 空 → 首次可生成)
# P2:不週期(pmfreqx=0)、無到期日、未停用 → 不週期回寫場景
# P3:週期 6 Months、到期 05/01/26、停用(suppress=T)→ 到期清單排除 + suppress 覆寫生成
SA_ROWS = [
    {
        "pmid": "P1", "compid": "EID-001", "task_no": "T-A",
        "pmfreqx": "12", "pmfreq": "Months", "pmnextdate": "06/01/26",
        "lastpmdate": "06/01/25", "lastpmno": "", "suppress": "F",
        "assignto": "CMA (Iris Chiu)", "standard": "1.50", "estlabor": "1.00",
        "dayscmpl": "2.5", "pm_type": "PM",
    },
    {
        "pmid": "P2", "compid": "EID-002", "task_no": "T-B",
        "pmfreqx": "0", "pmfreq": "", "pmnextdate": "",
        "lastpmdate": "", "lastpmno": "", "suppress": "F",
        "assignto": "", "standard": ".00", "estlabor": ".20",
        "dayscmpl": ".00", "pm_type": "PM",
    },
    {
        "pmid": "P3", "compid": "EID-001", "task_no": "T-C",
        "pmfreqx": "6", "pmfreq": "Months", "pmnextdate": "05/01/26",
        "lastpmdate": "", "lastpmno": "", "suppress": "T",
        "assignto": "", "standard": ".00", "estlabor": ".00",
        "dayscmpl": "2.5", "pm_type": "PM",
    },
]


async def _seed_wo_lookups(session) -> None:
    """種 PM 生成所需 work_order lookup:work_type=PM + canonical wo_status(經單一寫入路徑)。"""
    svc = WorkOrderService(session)
    async with svc.write(Actor.human("migration")):
        await svc.upsert_lookup(WorkType, "PM", "PM")
        for code, label, rank, is_terminal, is_downtime in WO_STATUS_SEED:
            await svc.upsert_status(
                code, label, rank=rank, is_terminal=is_terminal, is_downtime=is_downtime
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
            await load_assets(ASSET_ROWS, s)  # FK 前置
            await load_tasks(TASK_ROWS, s)
            await load_pm(SA_ROWS, s)  # pm_schedule + freq_unit / vendor 種子
            await _seed_wo_lookups(s)  # work_type=PM + canonical wo_status
            yield s
        await engine.dispose()


def _ts(h: int, m: int = 0) -> datetime:
    """2026-05-20 當地(Taipei)時間戳(便於斷言 closed_at 當地日)。"""
    return datetime(2026, 5, 20, h, m, tzinfo=TAIPEI)


async def test_generate_creates_pm_work_order(session) -> None:
    svc = WorkOrderService(session)
    wo = await svc.generate_pm_work_order(pm_id="P1", actor=Actor.human("eng1"), at=_ts(9))

    assert wo.work_type == "PM"
    assert wo.status == "OPEN"
    assert wo.asset_id == "EID-001"  # 來自 pm_schedule.asset_id
    assert wo.pm_source_id == "P1"
    assert wo.brief_description == "Annual inspection of rig"  # 來自 Task.description
    assert wo.source_actor == "human:eng1"  # 稽核歸屬(ADR-005)

    pm = await PmScheduleService(session).get_pm_schedule("P1")
    assert pm.last_work_order_no == wo.work_order_no  # soft-ref 回指
    assert pm.source_actor == "human:eng1"


async def test_generate_is_idempotent_while_open(session) -> None:
    svc = WorkOrderService(session)
    wo1 = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(9))
    wo2 = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(10))

    assert wo2.work_order_no == wo1.work_order_no  # 未結案 → 回同一張,不重複生成
    pm_wos = await svc.list_work_orders(asset_id="EID-001", work_type="PM", limit=100)
    assert len(pm_wos) == 1  # DB 中只有一列


async def test_generate_again_after_close_creates_new(session) -> None:
    svc = WorkOrderService(session)
    wo1 = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(9))
    no1 = wo1.work_order_no
    await svc.start_work(no1, ACTOR, at=_ts(10))
    await svc.complete_work(no1, ACTOR, at=_ts(11))
    await svc.close_work_order(no1, ACTOR, at=_ts(12))  # 終態

    wo2 = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(13))
    assert wo2.work_order_no != no1  # 前一張已結案 → 新一張(下個週期)
    pm = await PmScheduleService(session).get_pm_schedule("P1")
    assert pm.last_work_order_no == wo2.work_order_no


async def test_close_advances_next_due_date_fixed(session) -> None:
    svc = WorkOrderService(session)
    pmsvc = PmScheduleService(session)
    wo = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))
    await svc.complete_work(no, ACTOR, at=_ts(11))
    await svc.close_work_order(no, ACTOR, at=_ts(12))

    pm = await pmsvc.get_pm_schedule("P1")
    # Fixed:由排定的 next_due_date(2026-06-01)+ 12 Months = 2027-06-01(非從完成日起算)
    assert pm.next_due_date == date(2027, 6, 1)
    assert pm.last_pm_date == date(2026, 5, 20)  # closed_at 當地日
    assert pm.source_actor == "human:tester"


async def test_non_recurring_close_keeps_next_due_date(session) -> None:
    svc = WorkOrderService(session)
    pmsvc = PmScheduleService(session)
    wo = await svc.generate_pm_work_order(pm_id="P2", actor=ACTOR, at=_ts(9))  # 不週期
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))
    await svc.complete_work(no, ACTOR, at=_ts(11))
    await svc.close_work_order(no, ACTOR, at=_ts(12))

    pm = await pmsvc.get_pm_schedule("P2")
    assert pm.next_due_date is None  # 不週期:不推進
    assert pm.last_pm_date == date(2026, 5, 20)  # 仍記完成日


async def test_due_list_filters_due_and_excludes_suppressed(session) -> None:
    pmsvc = PmScheduleService(session)
    due = await pmsvc.list_pm_schedules(
        due_on_or_before=date(2026, 12, 31), is_suppressed=False, limit=100
    )
    # P1(到期 06/01/26、未停用)入;P3(到期但 suppress)排除;P2(無到期日)排除
    assert {s.pm_id for s in due} == {"P1"}


async def test_generate_allowed_when_suppressed(session) -> None:
    svc = WorkOrderService(session)
    # P3 被 suppress;按需是真人明確覆寫 → 仍應生成(suppress 僅治理自動排程器 + 到期清單)
    wo = await svc.generate_pm_work_order(pm_id="P3", actor=ACTOR, at=_ts(9))
    assert wo.work_type == "PM" and wo.status == "OPEN"
    assert wo.pm_source_id == "P3"
    pm = await PmScheduleService(session).get_pm_schedule("P3")
    assert pm.last_work_order_no == wo.work_order_no


async def test_generate_unknown_pm_raises(session) -> None:
    svc = WorkOrderService(session)
    with pytest.raises(WorkOrderError):
        await svc.generate_pm_work_order(pm_id="NOPE", actor=ACTOR)


async def test_generate_pm_carries_assigned_person(session) -> None:
    """#5a:PM 生成的工單自動帶 pm.assigned_person(P1 assignto=CMA (Iris Chiu) → 「Iris Chiu」)。"""
    svc = WorkOrderService(session)
    pm = await PmScheduleService(session).get_pm_schedule("P1")
    assert pm.assigned_person == "Iris Chiu"  # parse_assintto:vendor=CMA / person=Iris Chiu
    wo = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(9))
    assert wo.assigned_person == "Iris Chiu"  # 沿用 pm 的 owner


async def test_generate_pm_assigned_person_override(session) -> None:
    """#5a:傳入 assigned_person override 生效;冪等命中既有工單時不改既有 owner。"""
    svc = WorkOrderService(session)
    wo1 = await svc.generate_pm_work_order(
        pm_id="P1", actor=ACTOR, at=_ts(9), assigned_person="Ben Yeh"
    )
    assert wo1.assigned_person == "Ben Yeh"  # override 覆蓋 pm 的「Iris Chiu」
    # 冪等命中(未結案)→ 回同一張,不因新 override 改既有 owner
    wo2 = await svc.generate_pm_work_order(
        pm_id="P1", actor=ACTOR, at=_ts(10), assigned_person="Cara Lo"
    )
    assert wo2.work_order_no == wo1.work_order_no
    assert wo2.assigned_person == "Ben Yeh"  # 既有工單 owner 不被覆寫


async def test_generate_pm_falls_back_to_asset_owners(session) -> None:
    """0031:PM 無 per-PM 覆寫 + 無 override → assignee 落到設備負責人(全部)。"""
    from cmms.domain.asset.models import AssetOwner

    svc = WorkOrderService(session)
    session.add_all([
        AssetOwner(asset_id="EID-002", person_name="Owner Bob", position=0),
        AssetOwner(asset_id="EID-002", person_name="Ben Yeh", position=1),
    ])
    await session.flush()
    wo = await svc.generate_pm_work_order(pm_id="P2", actor=ACTOR, at=_ts(9))
    assert await svc.get_assignees(wo.work_order_no) == ["Owner Bob", "Ben Yeh"]
    assert wo.assigned_person == "Owner Bob"  # denormalized 首位


async def test_generate_pm_pm_assignee_wins_over_owner(session) -> None:
    """0031:優先序 = override → pm.assigned_person → 設備負責人;per-PM 覆寫勝過負責人。"""
    from cmms.domain.asset.models import AssetOwner

    svc = WorkOrderService(session)
    session.add(AssetOwner(asset_id="EID-001", person_name="Owner Bob", position=0))
    await session.flush()
    wo = await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(9))
    assert wo.assigned_person == "Iris Chiu"  # pm.assigned_person 勝過設備負責人
    assert await svc.get_assignees(wo.work_order_no) == ["Iris Chiu"]


async def test_assignee_suggestions_include_asset_owner(session) -> None:
    """0031:list_assignee_suggestions 併入設備負責人(asset_owner)。"""
    from cmms.domain.asset.models import AssetOwner

    svc = WorkOrderService(session)
    session.add(AssetOwner(asset_id="EID-002", person_name="Zelda Owner", position=0))
    await session.flush()
    assert "Zelda Owner" in await svc.list_assignee_suggestions("zelda")


async def test_generate_pm_retired_asset_raises(session) -> None:
    """#1:退役 asset(is_active=false)的 PM 按需生成被擋(Jordan 2026-07-05 裁決)。"""
    svc = WorkOrderService(session)
    asset = await session.get(Asset, "EID-001")  # P1 的機台
    asset.is_active = False
    await session.flush()
    with pytest.raises(WorkOrderError, match="retired"):
        await svc.generate_pm_work_order(pm_id="P1", actor=ACTOR, at=_ts(9))


async def test_scheduler_skips_retired_asset_without_crashing(session) -> None:
    """#1:排程器對退役 asset 的 PM → per-PM 例外隔離為 error,不炸整批。"""
    svc = WorkOrderService(session)
    asset = await session.get(Asset, "EID-001")  # P1 的機台(唯一到期且週期性者)
    asset.is_active = False
    await session.flush()
    results = await svc.generate_due_pm_work_orders(
        actor=Actor.scheduler(), as_of=date(2026, 6, 30)
    )
    p1 = [r for r in results if r.pm_id == "P1"]
    assert len(p1) == 1
    assert p1[0].work_order_no is None and p1[0].created is False
    assert "retired" in (p1[0].error or "")  # 隔離為 error,批次未中斷
