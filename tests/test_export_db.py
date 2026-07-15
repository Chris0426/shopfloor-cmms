"""ExportService 的 DB 整合測試(testcontainers-postgres)。

驗五個資料集的 count/rows、過濾器命中/未命中、軟刪過濾、join 顯示欄,以及
pm_task_details 的步驟 UI 序枚舉 + 多料展多列 + 無料步驟一列料欄空。本機無 Docker 時自動 skip。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset.models import (  # noqa: E402
    Asset,
    AssetOwner,
    AssetType,
    Department,
    Line,
)
from cmms.domain.assistant import models as _assistant_models  # noqa: E402, F401
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.exports.service import ExportService  # noqa: E402
from cmms.domain.failure_vocab import models as _fv_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory.models import (  # noqa: E402
    InventoryItem,
    StockTransaction,
    StockTxnKind,
)
from cmms.domain.inventory.service import InventoryService  # noqa: E402
from cmms.domain.pm_schedule.models import FreqUnit, PmSchedule  # noqa: E402
from cmms.domain.procurement import models as _proc_models  # noqa: E402, F401
from cmms.domain.task.models import Task, TaskPart, TaskStep  # noqa: E402
from cmms.domain.work_order.models import (  # noqa: E402
    WorkOrder,
    WorkOrderPart,
    WorkType,
    WoStatus,
)


def _dt(y: int, mo: int, d: int, h: int = 0) -> datetime:
    return datetime(y, mo, d, h, tzinfo=UTC)


async def _seed(session) -> None:
    # 受控 lookup(FK 前置)
    session.add_all([
        AssetType(code="Production", label="Production"),
        AssetType(code="Support", label="Support"),
        Department(code="EQ", label="EQ"),
        Line(code="10K", label="10K"),
        WorkType(code="REACTIVE", label="Reactive"),
        WorkType(code="PM", label="Preventive"),
        WoStatus(code="OPEN", label="Open"),
        WoStatus(code="CLOSED", label="Closed"),
        FreqUnit(code="Months", label="Months"),
        StockTxnKind(code="ISSUE", label="Issue"),
        StockTxnKind(code="RETURN", label="Return"),
    ])
    await session.flush()

    # 設備:EID-001 啟用 Production;EID-002 退役 Support
    session.add_all([
        Asset(asset_id="EID-001", description="Rig One", asset_type="Production",
              department="EQ", line="10K", site="PLANT-1", is_active=True),
        Asset(asset_id="EID-002", description="Rig Two", asset_type="Support",
              department="EQ", line="10K", site="PLANT-1", is_active=False),
    ])
    await session.flush()
    # 0031:設備負責人交叉表(EID-001 → Owner Bob;EID-002 無負責人)
    session.add(AssetOwner(asset_id="EID-001", person_name="Owner Bob", position=0))
    # 備品
    session.add_all([
        InventoryItem(item_code="ES001", name="Valve A", currency="USD"),
        InventoryItem(item_code="ES002", name="Pump B", currency="USD"),
    ])
    await session.flush()

    # 工單:WO100 已結案(EID-001,REACTIVE);WO101 開立中(EID-002,PM)
    session.add_all([
        WorkOrder(work_order_no=100, asset_id="EID-001", work_type="REACTIVE",
                  status="CLOSED", brief_description="belt snapped",
                  opened_date=date(2026, 5, 1), closed_date=date(2026, 5, 2),
                  downtime_minutes=120, assigned_person="Alice Fang"),
        WorkOrder(work_order_no=101, asset_id="EID-002", work_type="PM",
                  status="OPEN", brief_description="quarterly",
                  opened_date=date(2026, 6, 1)),
    ])
    await session.flush()

    # 工單領料:part1 存活、part2 軟刪(必須被排除)
    session.add_all([
        WorkOrderPart(work_order_no=100, item_code="ES001", quantity=Decimal("2"),
                      created_at=_dt(2026, 5, 1, 10)),
        WorkOrderPart(work_order_no=100, item_code="ES002", quantity=Decimal("1"),
                      created_at=_dt(2026, 5, 3, 10), deleted_at=_dt(2026, 5, 4)),
    ])

    # 保養任務 + 步驟(proc_seq 亂序 + NULL + 一筆軟刪)
    session.add(Task(task_no="T1", description="Quarterly PM"))
    await session.flush()
    step_b = TaskStep(task_no="T1", proc_seq=10, task_desc="Clean head")
    step_a = TaskStep(task_no="T1", proc_seq=20, task_desc="Check belt")
    step_c = TaskStep(task_no="T1", proc_seq=None, task_desc="Inspect")
    step_d = TaskStep(task_no="T1", proc_seq=5, task_desc="DELETED STEP",
                      deleted_at=_dt(2026, 1, 1))
    session.add_all([step_b, step_a, step_c, step_d])
    await session.flush()
    # step B 兩料(展兩列);step A 無料(一列料欄空);step C 一活料 + 一軟刪料
    session.add_all([
        TaskPart(task_step_id=step_b.id, item_code="ES001", replace_qty=Decimal("1")),
        TaskPart(task_step_id=step_b.id, item_code="ES002", replace_qty=Decimal("2")),
        TaskPart(task_step_id=step_c.id, item_code="ES001", replace_qty=None),
        TaskPart(task_step_id=step_c.id, item_code="ES002", replace_qty=Decimal("9"),
                 deleted_at=_dt(2026, 1, 1)),
    ])

    # 保養排程:PMW-1(EID-001,週期,未暫停);PMW-2(EID-002,已暫停,無週期單位)
    session.add_all([
        PmSchedule(pm_id="PMW-1", asset_id="EID-001", task_id="T1",
                   frequency_interval=3, frequency_unit="Months",
                   next_due_date=date(2026, 7, 1), is_suppressed=False),
        PmSchedule(pm_id="PMW-2", asset_id="EID-002", task_id="T1",
                   frequency_interval=0, frequency_unit=None, is_suppressed=True),
    ])
    await session.commit()

    # 直領(ADR-024 非工單直領)走單一寫入路徑 InventoryService:
    #   D1 存活 = EID-002 / ES002 / 5(5/5 10:00Z);D2 已取消 = EID-001 / ES001 / 3(5/6 10:00Z)
    # 已取消直領須被匯出排除(對稱工單側軟刪排除)。
    inv = InventoryService(session)
    actor = Actor.human("seed")
    await inv.issue_to_asset(
        asset_id="EID-002", item_code="ES002", quantity=Decimal("5"), actor=actor,
        at=_dt(2026, 5, 5, 10), idempotency_key="seed-d1",
    )
    await inv.issue_to_asset(
        asset_id="EID-001", item_code="ES001", quantity=Decimal("3"), actor=actor,
        at=_dt(2026, 5, 6, 10), idempotency_key="seed-d2",
    )
    d2_txn_id = await session.scalar(
        select(StockTransaction.txn_id).where(StockTransaction.idempotency_key == "seed-d2")
    )
    await inv.cancel_asset_issue(asset_id="EID-001", txn_id=d2_txn_id, actor=actor)


@pytest.fixture
async def svc():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await _seed(s)
            yield ExportService(s)
        await engine.dispose()


# ---- ① 工單 ----

async def test_work_orders_count_and_filters(svc) -> None:
    assert await svc.count_work_orders() == 2
    assert await svc.count_work_orders(statuses=["CLOSED"]) == 1
    assert await svc.count_work_orders(work_types=["PM"]) == 1
    assert await svc.count_work_orders(opened_from=date(2026, 6, 1)) == 1
    assert await svc.count_work_orders(opened_to=date(2026, 5, 31)) == 1
    assert await svc.count_work_orders(closed_from=date(2026, 5, 1)) == 1  # 只 WO100 有結案日
    assert await svc.count_work_orders(asset_id="EID-001") == 1
    assert await svc.count_work_orders(assigned_person="Alice Fang") == 1
    assert await svc.count_work_orders(assigned_person="Nobody") == 0


async def test_work_orders_rows_join_and_order(svc) -> None:
    rows = await svc.rows_work_orders()
    assert [r["work_order_no"] for r in rows] == [101, 100]  # work_order_no DESC
    by_no = {r["work_order_no"]: r for r in rows}
    assert by_no[100]["asset_description"] == "Rig One"  # join asset
    assert by_no[100]["downtime_minutes"] == 120
    # 限筆(預覽)
    assert len(await svc.rows_work_orders(limit=1)) == 1


# ---- ② 領料(工單領料 ∪ 直掛設備直領;軟刪 + 取消排除)----

async def test_part_usage_union_both_kinds(svc) -> None:
    # 兩型各一存活列:工單領料 part1(WO100/ES001/EID-001/2)+ 直領 D1(EID-002/ES002/5);
    # 工單領料 part2 軟刪、直領 D2 已取消 → 皆排除。
    assert await svc.count_part_usage() == 2
    rows = await svc.rows_part_usage()
    assert len(rows) == 2
    by_type = {r["issue_type"]: r for r in rows}
    assert set(by_type) == {"work_order", "direct"}

    wo = by_type["work_order"]
    assert wo["work_order_no"] == 100 and wo["asset_id"] == "EID-001"
    assert wo["item_code"] == "ES001" and wo["item_name"] == "Valve A"
    assert wo["quantity"] == Decimal("2")

    dr = by_type["direct"]
    assert dr["work_order_no"] is None and dr["asset_id"] == "EID-002"
    assert dr["item_code"] == "ES002" and dr["item_name"] == "Pump B"
    assert dr["quantity"] == Decimal("5")  # −qty_delta = 正數


async def test_part_usage_issue_type_filter(svc) -> None:
    assert await svc.count_part_usage(issue_type="work_order") == 1
    assert await svc.count_part_usage(issue_type="direct") == 1
    # 未知值 / 空 → 不過濾(兩型皆出)
    assert await svc.count_part_usage(issue_type="") == 2


async def test_part_usage_cancelled_direct_excluded(svc) -> None:
    # D2(EID-001 直領)已取消 → 不出現在任一切面
    assert await svc.count_part_usage(issue_type="direct", asset_id="EID-001") == 0
    rows = await svc.rows_part_usage(issue_type="direct")
    assert [r["asset_id"] for r in rows] == ["EID-002"]  # 只剩 D1


async def test_part_usage_asset_and_wo_filters(svc) -> None:
    # asset_id 命中直領:EID-002 只有直領 D1(工單側無 EID-002 存活列)
    assert await svc.count_part_usage(asset_id="EID-002") == 1
    # EID-001:工單領料 part1 存活;直領 D2 已取消 → 只 1
    assert await svc.count_part_usage(asset_id="EID-001") == 1
    # 工單號過濾排除直領(直領無工單):只 work_order 分支
    assert await svc.count_part_usage(work_order_no=100) == 1
    rows = await svc.rows_part_usage(work_order_no=100)
    assert all(r["issue_type"] == "work_order" for r in rows)
    # 料號 ES002:工單側 part2 軟刪排除;直領 D1 為 ES002 存活 → 1
    assert await svc.count_part_usage(item_code="ES002") == 1


async def test_part_usage_issued_date_filter_taipei(svc) -> None:
    # part1 created_at = 5/1 10:00Z = 5/1 18:00 台北;直領 D1 occurred = 5/5 10:00Z = 5/5 18:00 台北
    assert await svc.count_part_usage(issued_from=date(2026, 5, 1)) == 2  # 跨兩分支
    assert await svc.count_part_usage(issued_from=date(2026, 5, 5)) == 1  # 只 D1
    assert await svc.count_part_usage(issued_to=date(2026, 5, 1)) == 1    # 只 part1
    # 排序:issued_at DESC → D1(5/5)在 part1(5/1)之前
    rows = await svc.rows_part_usage()
    assert [r["issue_type"] for r in rows] == ["direct", "work_order"]


# ---- ③ 設備 ----

async def test_assets_filters(svc) -> None:
    assert await svc.count_assets() == 2
    assert await svc.count_assets(asset_types=["Production"]) == 1
    assert await svc.count_assets(is_active=True) == 1
    assert await svc.count_assets(is_active=False) == 1
    assert await svc.count_assets(department="EQ") == 2
    rows = await svc.rows_assets()
    assert [r["asset_id"] for r in rows] == ["EID-001", "EID-002"]  # asset_id ASC
    assert rows[0]["owner"] == "Owner Bob" and rows[1]["owner"] is None  # 0029 owner 欄


# ---- ④ 保養排程 ----

async def test_pm_schedules_filters_and_join(svc) -> None:
    assert await svc.count_pm_schedules() == 2
    assert await svc.count_pm_schedules(is_suppressed=True) == 1
    assert await svc.count_pm_schedules(is_suppressed=False) == 1
    assert await svc.count_pm_schedules(frequency_units=["Months"]) == 1
    assert await svc.count_pm_schedules(due_from=date(2026, 7, 1)) == 1
    rows = await svc.rows_pm_schedules(asset_id="EID-001")
    assert rows[0]["task_name"] == "Quarterly PM"  # join task
    # 0029:匯出有效 assignee = coalesce(per-PM 覆寫, asset.owner);PMW-1 無覆寫 → 落 owner
    assert rows[0]["assigned_person"] == "Owner Bob"
    assert await svc.count_pm_schedules(assigned_person="Owner Bob") == 1  # 過濾亦以有效值


# ---- ⑤ 保養步驟明細(枚舉 + 展開 + 軟刪)----

async def test_pm_task_details_expansion(svc) -> None:
    # 過濾 EID-001 → 只 PMW-1(task T1)。live 步驟 B(seq10)/A(seq20)/C(NULL);D 軟刪排除。
    assert await svc.count_pm_task_details(asset_id="EID-001") == 4
    rows = await svc.rows_pm_task_details(asset_id="EID-001")
    seq = [(r["step_no"], r["item_code"]) for r in rows]
    # B(step_no=1)兩料展兩列;A(step_no=2)無料一列料欄空;C(step_no=3)一活料(軟刪料排除)
    assert seq == [(1, "ES001"), (1, "ES002"), (2, None), (3, "ES001")]
    assert rows[0]["step_desc"] == "Clean head"   # step_no 1 = 最小 proc_seq(D 已刪不算 step 1)
    assert rows[2]["item_code"] is None            # 無料步驟料欄空
    assert rows[0]["task_name"] == "Quarterly PM"  # join task


async def test_pm_task_details_filters(svc) -> None:
    assert await svc.count_pm_task_details(task_no="T1", asset_id="EID-001") == 4
    assert await svc.count_pm_task_details(task_desc="quarter", asset_id="EID-001") == 4
    assert await svc.count_pm_task_details(task_desc="nomatch") == 0


# ---- lookup 選項 ----

async def test_lookup_options(svc) -> None:
    wt = await svc.lookup_options("work_type")
    assert [c for c, _ in wt] == ["PM", "REACTIVE"]  # 依 code 排序
    assert await svc.lookup_options("asset_type") == [("Production", "Production"),
                                                      ("Support", "Support")]
    assert await svc.lookup_options("unknown") == []
