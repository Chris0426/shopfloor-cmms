"""ScheduledActivity 切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip。涵蓋三切片整合(asset+task+pm_schedule):
載入器 idempotent、讀取、assignto 拆解、日期/週期解析、T3 閒置 task 標記、稽核。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.pm_schedule.loader import load as load_pm  # noqa: E402
from cmms.domain.pm_schedule.service import PmScheduleService  # noqa: E402
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.task.loader import load as load_tasks  # noqa: E402
from cmms.domain.task.service import TaskService  # noqa: E402

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
# T-C 不被任何 SA 引用 → T3 應標記 inactive
TASK_ROWS = [
    {"task_no": "T-A", "task_desc": "Task A"},
    {"task_no": "T-B", "task_desc": "Task B"},
    {"task_no": "T-C", "task_desc": "Task C (idle)"},
]
SA_ROWS = [
    {
        "pmid": "P1",
        "compid": "EID-001",
        "task_no": "T-A",
        "pmfreqx": "12",
        "pmfreq": "Months",
        "pmnextdate": "05/19/25",
        "lastpmdate": "12/17/21",
        "lastpmno": "10415",
        "suppress": "F",
        "assignto": "CMA (Iris Chiu)",
        "standard": "1.50",
        "estlabor": "1.00",
        "dayscmpl": "2.5",
        "pm_type": "PM",
    },
    {
        "pmid": "P2",
        "compid": "EID-002",
        "task_no": "T-B",
        "pmfreqx": "0",
        "pmfreq": "",
        "pmnextdate": "",
        "lastpmdate": "08/01/25",
        "lastpmno": "22178",
        "suppress": "T",
        "assignto": "",
        "standard": ".00",
        "estlabor": ".20",
        "dayscmpl": ".00",
        "pm_type": "PM",
    },
]


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.exec_driver_sql(
                "INSERT INTO external_id_namespace (code, label) VALUES "
                "('mes_equipment','MES equipment id'),('layer_b_sensor','sensor')"
            )
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            # 前置:FK 依賴(asset + task)先載入
            await load_assets(ASSET_ROWS, s)
            await load_tasks(TASK_ROWS, s)
            yield s
        await engine.dispose()


async def test_load_reads_and_parses(session) -> None:
    res = await load_pm(SA_ROWS, session)
    assert res.pm_schedules == 2
    assert res.freq_units == 1  # Months(P2 不週期無 unit)
    assert res.vendors == 1  # CMA

    svc = PmScheduleService(session)
    p1 = await svc.get_pm_schedule("P1")
    assert p1 is not None
    assert p1.asset_id == "EID-001" and p1.task_id == "T-A"
    assert p1.frequency_interval == 12 and p1.frequency_unit == "Months"
    assert p1.next_due_date == date(2025, 5, 19)
    assert p1.last_work_order_no == 10415
    assert p1.assigned_vendor == "CMA" and p1.assigned_person == "Iris Chiu"
    assert p1.standard_hours == Decimal("1.50")
    assert p1.is_suppressed is False
    assert p1.source_actor == "human:migration"  # 稽核(ADR-005)

    p2 = await svc.get_pm_schedule("P2")
    assert p2 is not None
    assert p2.frequency_interval == 0 and p2.frequency_unit is None  # 不週期
    assert p2.next_due_date is None
    assert p2.assigned_vendor is None and p2.assigned_person is None
    assert p2.is_suppressed is True


async def test_t3_idle_task_marking(session) -> None:
    res = await load_pm(SA_ROWS, session)
    assert res.idle_tasks_marked == 1  # 僅 T-C 未被引用

    tsvc = TaskService(session)
    inactive = await tsvc.list_tasks(is_active=False, limit=1000)
    assert {t.task_no for t in inactive} == {"T-C"}
    active = await tsvc.list_tasks(is_active=True, limit=1000)
    assert {t.task_no for t in active} == {"T-A", "T-B"}
    # 被標記的 task 帶稽核歸屬
    tc = await tsvc.get_task("T-C")
    assert tc is not None and tc.source_actor == "human:migration"


async def test_load_is_idempotent(session) -> None:
    await load_pm(SA_ROWS, session)
    res2 = await load_pm(SA_ROWS, session)
    assert res2.pm_schedules == 2
    assert res2.idle_tasks_marked == 0  # 再跑無新標記(狀態已穩定)

    svc = PmScheduleService(session)
    assert len(await svc.list_pm_schedules(limit=1000)) == 2
    tsvc = TaskService(session)
    assert len(await tsvc.list_tasks(is_active=False, limit=1000)) == 1  # T-C 仍 inactive


# ---- 2026-07-03 批:admin PM 排程 CRUD(governed;非 loader)----


async def test_pm_schedule_crud_governed(session) -> None:
    from cmms.audit import Actor
    from cmms.domain.pm_schedule.service import PmScheduleError

    await load_pm(SA_ROWS, session)  # 種 freq_unit lookup(Months)+ 既有 (EID-001, T-A)
    admin = Actor.human("jordan.lee")
    svc = PmScheduleService(session)

    # 建排程:PMW- 新命名空間、稽核、(asset, task) 唯一
    pm = await svc.create_pm_schedule(
        asset_id="EID-002", task_id="T-A", actor=admin,
        frequency_interval=3, frequency_unit="Months",
        next_due_date=date(2026, 8, 1), assigned_person="Jordan Lee",
    )
    assert pm.pm_id.startswith("PMW-") and pm.created_by == admin.value
    assert pm.is_suppressed is False
    with pytest.raises(PmScheduleError):  # (EID-001, T-A) 已由 P1 佔用
        await svc.create_pm_schedule(asset_id="EID-001", task_id="T-A", actor=admin)
    with pytest.raises(PmScheduleError):  # 週期不變量:interval>0 需 unit
        await svc.create_pm_schedule(
            asset_id="EID-002", task_id="T-B", actor=admin, frequency_interval=5
        )
    with pytest.raises(PmScheduleError):  # 未知設備
        await svc.create_pm_schedule(asset_id="EID-NOPE", task_id="T-A", actor=admin)

    # 改排程(週期 / 到期 / 負責人)
    pm = await svc.update_pm_schedule(
        pm.pm_id, actor=admin, frequency_interval=6, frequency_unit="Months",
        next_due_date=date(2026, 9, 1), assigned_person=None,
    )
    assert pm.frequency_interval == 6 and pm.next_due_date == date(2026, 9, 1)
    assert pm.assigned_person is None and pm.updated_by == admin.value
    with pytest.raises(PmScheduleError):  # 未知 unit(lookup 驗證)
        await svc.update_pm_schedule(
            pm.pm_id, actor=admin, frequency_interval=1, frequency_unit="Years",
            next_due_date=None, assigned_person=None,
        )

    # 暫停/恢復(冪等)
    pm = await svc.set_suppressed(pm.pm_id, True, admin)
    assert pm.is_suppressed is True
    pm = await svc.set_suppressed(pm.pm_id, True, admin)  # no-op
    pm = await svc.set_suppressed(pm.pm_id, False, admin)
    assert pm.is_suppressed is False


async def test_filters(session) -> None:
    await load_pm(SA_ROWS, session)
    svc = PmScheduleService(session)

    contractor = await svc.list_pm_schedules(assigned_vendor="CMA", limit=1000)
    assert {s.pm_id for s in contractor} == {"P1"}

    suppressed = await svc.list_pm_schedules(is_suppressed=True, limit=1000)
    assert {s.pm_id for s in suppressed} == {"P2"}

    by_asset = await svc.list_pm_schedules(asset_id="EID-001", limit=1000)
    assert {s.pm_id for s in by_asset} == {"P1"}

    # 到期日過濾:只回有日期且 <= 界線者(P2 無日期不計)
    due = await svc.list_pm_schedules(due_on_or_before=date(2025, 12, 31), limit=1000)
    assert {s.pm_id for s in due} == {"P1"}

    # 月曆下界(due_on_or_after):P1 到期日 2025-05-19,NULL 一律排除
    assert {s.pm_id for s in await svc.list_pm_schedules(
        due_on_or_after=date(2025, 1, 1), limit=1000)} == {"P1"}
    assert await svc.list_pm_schedules(due_on_or_after=date(2025, 6, 1), limit=1000) == []
    # 月視窗 [after, before]:2025-05 命中 P1,2025-06 不命中
    assert {s.pm_id for s in await svc.list_pm_schedules(
        due_on_or_after=date(2025, 5, 1), due_on_or_before=date(2025, 5, 31), limit=1000)} == {"P1"}
    assert await svc.list_pm_schedules(
        due_on_or_after=date(2025, 6, 1), due_on_or_before=date(2025, 6, 30), limit=1000) == []


async def test_list_pm_schedules_effective_assignee(session) -> None:
    """0031:有效 assignee = per-PM 覆寫(單值)否則設備負責人(全部)。Mine 過濾 + 顯示標註皆用之。"""
    from cmms.domain.asset.models import AssetOwner

    await load_pm(SA_ROWS, session)
    # P2 的機台(P2 無 per-PM assigned_person)→ 兩位設備負責人
    session.add_all([
        AssetOwner(asset_id="EID-002", person_name="Owner Bob", position=0),
        AssetOwner(asset_id="EID-002", person_name="Cara Lin", position=1),
    ])
    await session.flush()
    svc = PmScheduleService(session)

    rows = {p.pm_id: p for p in await svc.list_pm_schedules(limit=1000)}
    assert rows["P1"].effective_assignee == "Iris Chiu"        # per-PM 覆寫優先(單值)
    assert rows["P1"].effective_assignees == ["Iris Chiu"]
    assert rows["P2"].effective_assignee == "Owner Bob、Cara Lin"  # 無覆寫 → 設備負責人(全部)
    assert rows["P2"].effective_assignees == ["Owner Bob", "Cara Lin"]

    # Mine 過濾:覆寫名撈得到 P1;任一設備負責人撈得到 P2(第二位亦命中)
    assert {p.pm_id for p in await svc.list_pm_schedules(
        assigned_person="Iris Chiu", limit=1000)} == {"P1"}
    assert {p.pm_id for p in await svc.list_pm_schedules(
        assigned_person="Owner Bob", limit=1000)} == {"P2"}
    assert {p.pm_id for p in await svc.list_pm_schedules(
        assigned_person="Cara Lin", limit=1000)} == {"P2"}  # 第二位負責人也命中
    # get_pm_schedule 亦標註有效 assignee(補開工單頁預填用)
    assert (await svc.get_pm_schedule("P2")).effective_assignee == "Owner Bob、Cara Lin"


async def test_review_fix_periodic_schedule_requires_next_due(session) -> None:
    """review f14cf8d S3:週期排程(interval>0)沒 next_due 會靜默休眠(到期清單/排程器
    都濾 next_due IS NOT NULL)—— 建立/更新時強制,不再收下永遠不會發生的排程。"""
    from cmms.audit import Actor
    from cmms.domain.pm_schedule.service import PmScheduleError

    await load_pm(SA_ROWS, session)
    admin = Actor.human("jordan.lee")
    svc = PmScheduleService(session)
    with pytest.raises(PmScheduleError, match="next_due_date"):
        await svc.create_pm_schedule(
            asset_id="EID-002", task_id="T-A", actor=admin,
            frequency_interval=3, frequency_unit="Months",  # 缺 next_due_date
        )
    pm = await svc.create_pm_schedule(
        asset_id="EID-002", task_id="T-A", actor=admin,
        frequency_interval=3, frequency_unit="Months", next_due_date=date(2026, 8, 1),
    )
    with pytest.raises(PmScheduleError, match="next_due_date"):
        await svc.update_pm_schedule(
            pm.pm_id, actor=admin, frequency_interval=3, frequency_unit="Months",
            next_due_date=None, assigned_person=None,
        )
    # 一次性(interval=0)不受影響
    ok = await svc.update_pm_schedule(
        pm.pm_id, actor=admin, frequency_interval=0, frequency_unit=None,
        next_due_date=None, assigned_person=None,
    )
    assert ok.frequency_interval == 0
    # 歸屬守門:task_id 不符 → 拒(review f14cf8d S2)
    with pytest.raises(PmScheduleError, match="belong"):
        await svc.set_suppressed(pm.pm_id, True, admin, task_id="T-OTHER")
