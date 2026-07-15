"""PM 自動排程器的 DB 整合測試(testcontainers-postgres)。ADR-021 time-based。

本機無 Docker 時自動 skip。涵蓋 `generate_due_pm_work_orders`(unattended 批次生成):
- 到期且週期性、未 suppress 的 PM → 生成 OPEN 工單,source_actor=scheduler;
- 排除:未到期(next_due_date > as_of)、suppress、非週期(frequency_interval=0)、無到期日;
- 冪等:重跑 → 既有未結案工單 created=False,不重複生成;
- as_of 窗口:推進截止日納入更晚到期的 PM;
- 結案推進 next_due_date 後,排程器於下一週期才再生成(catch-up 正確)。
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
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.pm_schedule.loader import load as load_pm  # noqa: E402
from cmms.domain.pm_schedule.service import PmScheduleService  # noqa: E402
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.task.loader import load as load_tasks  # noqa: E402
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import WO_STATUS_SEED  # noqa: E402
from cmms.domain.work_order.models import WorkType  # noqa: E402
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402
from cmms.domain.work_order.transform import TAIPEI  # noqa: E402

SCHEDULER = Actor.scheduler()
HUMAN = Actor.human("tester")

ASSET_ROWS = [
    {
        "compid": "EID-001", "comp_desc": "Rig 1", "assettype": "Production",
        "department": "EQ", "line_no": "10K", "available": "Yes",
    },
    {
        "compid": "EID-002", "comp_desc": "Rig 2", "assettype": "Production",
        "department": "EQ", "line_no": "10K", "available": "Yes",
    },
]
TASK_ROWS = [
    {"task_no": "T-A", "task_desc": "Annual inspection of rig"},
    {"task_no": "T-B", "task_desc": "One-off calibration"},
    {"task_no": "T-C", "task_desc": "Suppressed monthly check"},
    {"task_no": "T-D", "task_desc": "Monthly lubrication"},
]
# P1:週期 12 Months、到期 06/01/26、未停用 → 排程器主場景(到期且週期性)
# P2:不週期(pmfreqx=0)、無到期日 → 排程器排除(留按需)
# P3:週期 6 Months、到期 05/01/26、停用 → 排程器排除(suppress)
# P4:週期 1 Months、到期 07/15/26、未停用 → 測 as_of 窗口(較晚到期)
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
    {
        "pmid": "P4", "compid": "EID-002", "task_no": "T-D",
        "pmfreqx": "1", "pmfreq": "Months", "pmnextdate": "07/15/26",
        "lastpmdate": "06/15/26", "lastpmno": "", "suppress": "F",
        "assignto": "", "standard": ".00", "estlabor": ".50",
        "dayscmpl": "1.0", "pm_type": "PM",
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
            await load_pm(SA_ROWS, s)
            await _seed_wo_lookups(s)
            yield s
        await engine.dispose()


def _ts(h: int, m: int = 0) -> datetime:
    """2026-06-30 當地(Taipei)時間戳(供生成工單注入確定的 opened_at)。"""
    return datetime(2026, 6, 30, h, m, tzinfo=TAIPEI)


async def test_scheduler_generates_due_periodic_only(session) -> None:
    svc = WorkOrderService(session)
    # as_of=2026-06-30:P1 到期(06/01)應生成;P4(07/15)未到、P3 suppress、P2 無到期日 → 排除
    results = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 6, 30), at=_ts(9)
    )
    assert [r.pm_id for r in results] == ["P1"]
    r = results[0]
    assert r.created is True and r.error is None and r.work_order_no is not None

    wo = await svc.get_work_order(r.work_order_no)
    assert wo.work_type == "PM"
    assert wo.status == "OPEN"
    assert wo.asset_id == "EID-001"
    assert wo.pm_source_id == "P1"
    assert wo.brief_description == "Annual inspection of rig"  # 來自 Task.description
    assert wo.source_actor == "scheduler"  # ADR-021 §4:時鐘驅動誠實標示

    pm = await PmScheduleService(session).get_pm_schedule("P1")
    assert pm.last_work_order_no == wo.work_order_no
    assert pm.source_actor == "scheduler"


async def test_scheduler_is_idempotent_on_rerun(session) -> None:
    svc = WorkOrderService(session)
    first = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 6, 30), at=_ts(9)
    )
    second = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 6, 30), at=_ts(10)
    )

    assert first[0].created is True
    assert second[0].created is False  # 本週期未結案 → 冪等命中,不重複生成
    assert second[0].work_order_no == first[0].work_order_no
    pm_wos = await svc.list_work_orders(asset_id="EID-001", work_type="PM", limit=100)
    assert len(pm_wos) == 1  # DB 中仍只有一張


async def test_scheduler_skips_when_none_due(session) -> None:
    svc = WorkOrderService(session)
    # as_of 早於所有到期日 → 無到期 PM
    results = await svc.generate_due_pm_work_orders(actor=SCHEDULER, as_of=date(2026, 1, 1))
    assert results == []


async def test_scheduler_as_of_window_includes_later_due(session) -> None:
    svc = WorkOrderService(session)
    # as_of=2026-08-01:P1(06/01)與 P4(07/15)皆到期;P3 suppress、P2 無到期日仍排除
    results = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 8, 1), at=_ts(9)
    )
    assert {r.pm_id for r in results} == {"P1", "P4"}
    assert all(r.created and r.error is None for r in results)


async def test_scheduler_weekend_forward_generation(session) -> None:
    """週末提前生成(#5d):到期落週六 → 其前的週五(有效生成日)就生成;週四不生成。

    only-timing:next_due_date 本身不變、Fixed 推進鏈不變 —— 這裡驗的是「何時被排程器選中」。
    EID-002/T-A 未被既有 SA_ROWS 佔用(避開 (asset,task) 唯一約束)。
    """
    pm_svc = PmScheduleService(session)
    wo_svc = WorkOrderService(session)
    sat = date(2026, 7, 11)  # 週六
    assert sat.weekday() == 5
    pm = await pm_svc.create_pm_schedule(
        asset_id="EID-002", task_id="T-A", actor=HUMAN,
        frequency_interval=1, frequency_unit="Months", next_due_date=sat,
    )
    # 週四(07-09):有效生成日=週五 07-10 > 07-09 → 尚未生成
    thu = await wo_svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 7, 9), at=_ts(9)
    )
    assert pm.pm_id not in {r.pm_id for r in thu}
    # 週五(07-10):有效生成日=07-10 <= 07-10 → 生成(即便到期日是隔天週六)
    fri = await wo_svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 7, 10), at=_ts(10)
    )
    hit = next((r for r in fri if r.pm_id == pm.pm_id), None)
    assert hit is not None and hit.created is True and hit.work_order_no is not None
    # next_due_date 未被生成時機改動(仍為原週六;推進只在結案回寫)
    fresh = await pm_svc.get_pm_schedule(pm.pm_id)
    assert fresh.next_due_date == sat


async def test_scheduler_regenerates_next_cycle_after_close(session) -> None:
    svc = WorkOrderService(session)
    # 第一輪生成 P1
    first = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 6, 30), at=_ts(9)
    )
    no1 = first[0].work_order_no
    # 結案 → 完成回寫推進 next_due_date(Fixed:2026-06-01 + 12mo = 2027-06-01)
    await svc.start_work(no1, HUMAN, at=_ts(10))
    await svc.complete_work(no1, HUMAN, at=_ts(11))
    await svc.close_work_order(no1, HUMAN, at=_ts(12))
    pm = await PmScheduleService(session).get_pm_schedule("P1")
    assert pm.next_due_date == date(2027, 6, 1)

    # 結案後同窗口再跑:P1 已不到期 → 不生成(下個週期才到)
    again = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2026, 12, 31), at=_ts(13)
    )
    assert "P1" not in {r.pm_id for r in again}  # P1 下個週期(2027-06)才再到期

    # 推進到 P1 下個週期到期日 → P1 重新生成一張新工單(created=True、號碼不同)
    next_cycle = await svc.generate_due_pm_work_orders(
        actor=SCHEDULER, as_of=date(2027, 6, 30), at=_ts(13)
    )
    p1 = next((r for r in next_cycle if r.pm_id == "P1"), None)
    assert p1 is not None and p1.created is True
    assert p1.work_order_no != no1
