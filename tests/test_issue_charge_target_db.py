"""領料歸屬(ADR-024)的 DB 整合測試(testcontainers-postgres)。本機無 Docker 時自動 skip。

驗證:① `InventoryService.issue_to_asset` 直領(扣 on_hand、charge_target_asset_id、kind ISSUE 負號、
**不建 work_order_part**、未知 EID/item raise、冪等);② DB CHECK `ck_stock_transaction_issue_charge`
守門 —— ISSUE 恰一個歸屬,拒孤兒(皆空)與雙重歸屬(皆非空)。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.loader import load as load_inv  # noqa: E402
from cmms.domain.inventory.service import InventoryError, InventoryService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.transform import TAIPEI  # noqa: E402

ACTOR = Actor.human("tester")
_OCCURRED = datetime(2026, 6, 1, tzinfo=TAIPEI)

ASSET_ROWS = [
    {
        "compid": "EID-002",
        "comp_desc": "Rig 2",
        "assettype": "Production",
        "department": "EQ",
        "line_no": "10K",
        "available": "Yes",
    },
]


def _inv_row(item: str, **over: str) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "item": item,
        "asset_sub": "",
        "sf_desc": "",
        "vpartno": "",
        "descrip": "d",
        "location": "",
        "orderpt": "",
        "onhand": "",
        "cost": "",
        "lead_time": "",
        "obsol": "F",
        "stock": "T",
        "supplier": "",
        "weblink": "",
        "photo": "",
        "parnt_item": "",
        "child_item": "",
        "alt_item": "",
        "comment": "",
    }
    base.update(over)
    return base


INV_ROWS = [_inv_row("ES001", onhand="10.000")]

# CLOSED WO 30167 on EID-002(供 double-charge 違規測試需要一個有效 WO;並種 stock_txn_kind)
WO_ROWS = [
    {
        "wo": "30167",
        "compid": "EID-002",
        "comp_desc": "x",
        "assetsubtp": "",
        "brief_desc": "fix",
        "diag": "",
        "comments": "",
        "date_wo": "05/21/26",
        "sch_date": "",
        "wo_type": "REACTIVE",
        "workstatus": "H",
        "miscreated": "F",
        "assignto": "CMA (Tester)",
        "edittime": "15:00:00",
        "editdate": "05/21/26",
        "edituser": "T",
        "time": "10:00:00",
        "time_cmpl": "15:00:00",
    },
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
            await load_assets(ASSET_ROWS, s)  # FK 前置(EID-002)
            await load_inv(INV_ROWS, s)  # ES001 onhand 10
            await load_wo(WO_ROWS, s)  # 種 stock_txn_kind/wo_status + WO 30167
            yield s
        await engine.dispose()


async def test_issue_to_asset_decrements_on_hand_and_sets_charge_target(session) -> None:
    inv = InventoryService(session)
    ok = await inv.issue_to_asset(
        asset_id="EID-002", item_code="ES001", quantity="3", actor=ACTOR, reason="bench swap"
    )
    assert ok is True
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("7.000")  # 10 → 7(直領扣帳)
    row = (
        await session.execute(
            text(
                "SELECT kind, qty_delta, work_order_no, charge_target_asset_id, reason "
                "FROM stock_transaction"
            )
        )
    ).one()
    assert row.kind == "ISSUE"
    assert row.qty_delta == Decimal("-3.000")  # 領料為負
    assert row.work_order_no is None  # 非工單
    assert row.charge_target_asset_id == "EID-002"  # 歸屬設備
    assert row.reason == "bench swap"
    # 直領不建 work_order_part
    cnt = (await session.execute(text("SELECT count(*) FROM work_order_part"))).scalar()
    assert cnt == 0


async def test_issue_to_asset_idempotent(session) -> None:
    inv = InventoryService(session)
    ok1 = await inv.issue_to_asset(
        asset_id="EID-002", item_code="ES001", quantity="2", actor=ACTOR, idempotency_key="k-1"
    )
    ok2 = await inv.issue_to_asset(
        asset_id="EID-002", item_code="ES001", quantity="2", actor=ACTOR, idempotency_key="k-1"
    )
    assert ok1 is True and ok2 is False  # 同 key 第二次跳過
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("8.000")  # 只扣一次(10→8)
    cnt = (await session.execute(text("SELECT count(*) FROM stock_transaction"))).scalar()
    assert cnt == 1


async def test_issue_to_asset_unknown_eid_or_item_raises(session) -> None:
    inv = InventoryService(session)
    with pytest.raises(InventoryError):
        await inv.issue_to_asset(asset_id="EID-999", item_code="ES001", quantity="1", actor=ACTOR)
    with pytest.raises(InventoryError):
        await inv.issue_to_asset(asset_id="EID-002", item_code="ZZZ999", quantity="1", actor=ACTOR)
    # 皆未寫任何帳、未動 on_hand
    cnt = (await session.execute(text("SELECT count(*) FROM stock_transaction"))).scalar()
    assert cnt == 0
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("10.000")


async def test_check_rejects_orphan_issue(session) -> None:
    """ISSUE 無任何歸屬(工單與設備皆空)→ DB CHECK 拒(禁孤兒領料)。"""
    inv = InventoryService(session)
    with pytest.raises(IntegrityError):
        async with inv.write(ACTOR):
            await inv.post_stock_transaction(
                item_code="ES001",
                qty_delta=Decimal("-1"),
                kind="ISSUE",
                actor=ACTOR,
                occurred_at=_OCCURRED,  # work_order_no 與 charge_target_asset_id 皆 None
            )


async def test_list_asset_part_usage_union(session) -> None:
    """單機零件消耗(ADR-024 讀取面):直領 ∪ 工單領料,新到舊。"""
    from cmms.domain.work_order.service import WorkOrderService

    inv = InventoryService(session)
    await inv.issue_to_asset(
        asset_id="EID-002", item_code="ES001", quantity="1", actor=ACTOR,
        at=datetime(2026, 6, 2, tzinfo=TAIPEI),
    )
    svc = WorkOrderService(session)
    async with svc.write(ACTOR):
        await svc.backfill_part_issue(
            work_order_no=30167, item_code="ES001", quantity="2",
            occurred_at=datetime(2026, 6, 1, tzinfo=TAIPEI), actor=ACTOR,
            idempotency_key="pu-1",
        )
    rows = await inv.list_asset_part_usage(["EID-002"])
    # 直領(6/2)在前、工單領料(6/1)在後;兩種歸屬都收
    assert [(r.item_code, r.work_order_no) for r in rows] == [("ES001", None), ("ES001", 30167)]
    assert await inv.list_asset_part_usage([]) == []


async def test_check_rejects_double_charge(session) -> None:
    """ISSUE 同時掛工單與設備 → DB CHECK 拒(禁雙重歸屬)。"""
    inv = InventoryService(session)
    with pytest.raises(IntegrityError):
        async with inv.write(ACTOR):
            await inv.post_stock_transaction(
                item_code="ES001",
                qty_delta=Decimal("-1"),
                kind="ISSUE",
                actor=ACTOR,
                work_order_no=30167,
                charge_target_asset_id="EID-002",
                occurred_at=_OCCURRED,
            )
