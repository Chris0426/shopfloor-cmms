"""part_issue_backfill 的 DB 整合測試(testcontainers-postgres)。本機無 Docker 時自動 skip。

驗證:回填到 CLOSED WO 成功(對照 issue_part_to_work_order 終態 raise)、**不動 on_hand**、
stock_transaction(ISSUE / 負號 / occurred_at=date_wo / reason provenance)、重複 (wo,item)
各一筆、loader 冪等重跑、缺 FK(WO/item)log+skip 不 raise、malformed 計數、符號。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.loader import load as load_inv  # noqa: E402
from cmms.domain.inventory.service import InventoryService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.part_issue_backfill import BACKFILL_ACTOR  # noqa: E402
from cmms.domain.work_order.part_issue_backfill import load as load_part_issues  # noqa: E402
from cmms.domain.work_order.service import (  # noqa: E402
    PartIssueOutcome,
    WorkOrderError,
    WorkOrderService,
)
from cmms.domain.work_order.transform import TAIPEI  # noqa: E402

ACTOR = Actor.human("tester")
_OCCURRED = datetime(2016, 8, 31, tzinfo=TAIPEI)

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


INV_ROWS = [_inv_row("ES001", onhand="10.000"), _inv_row("ES002", onhand="5.000")]

# CLOSED WO 30167 on EID-002(workstatus H → CLOSED)
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


def _pi_row(
    wo: str, item: str, *, qty: str = "1.00", descrip: str = "part", compid: str = "EID-002"
) -> dict[str, str | None]:
    """一列 part_issues.csv(鍵 = 表頭)。`compid` = 領料列自帶的設備 EID(救援退路,ADR-024)。"""
    return {
        "wo": wo,
        "date_wo": "08/31/16",
        "assetsubtp": "",
        "compid": compid,
        "comp_desc": "x",
        "item": item,
        "vpartno": "",
        "descrip": descrip,
        "qty": qty,
        "unitcost": "0.00",
        "extcost": "0.00",
        "category": "Parts",
        "": None,
    }


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
            await load_inv(INV_ROWS, s)  # 領料品項(ES001 onhand 10、ES002 onhand 5)
            await load_wo(WO_ROWS, s)  # 種 stock_txn_kind/wo_status + CLOSED WO 30167
            yield s
        await engine.dispose()


async def test_backfill_to_closed_wo_succeeds(session) -> None:
    svc = WorkOrderService(session)
    assert (await svc.get_work_order(30167)).status == "CLOSED"
    async with svc.write(BACKFILL_ACTOR):
        outcome = await svc.backfill_part_issue(
            work_order_no=30167,
            item_code="ES001",
            quantity="3",
            occurred_at=_OCCURRED,
            actor=BACKFILL_ACTOR,
            idempotency_key="k-1",
            reason="filter",
        )
    assert outcome is PartIssueOutcome.INSERTED
    parts = await svc.get_parts(30167)
    assert len(parts) == 1 and parts[0].item_code == "ES001"
    assert parts[0].quantity == Decimal("3.000")  # work_order_part 為正
    assert parts[0].source_actor == "human:data-migration"  # 稽核 D2


async def test_backfill_does_not_touch_on_hand(session) -> None:
    svc = WorkOrderService(session)
    inv = InventoryService(session)
    async with svc.write(BACKFILL_ACTOR):
        await svc.backfill_part_issue(
            work_order_no=30167,
            item_code="ES001",
            quantity="3",
            occurred_at=_OCCURRED,
            actor=BACKFILL_ACTOR,
            idempotency_key="k-1",
        )
    item = await inv.get_item("ES001")
    assert item.quantity_on_hand == Decimal("10.000")  # 未變(規則 ②;非 7)


async def test_backfill_writes_negative_issue_stock_txn(session) -> None:
    svc = WorkOrderService(session)
    async with svc.write(BACKFILL_ACTOR):
        await svc.backfill_part_issue(
            work_order_no=30167,
            item_code="ES001",
            quantity="3",
            occurred_at=_OCCURRED,
            actor=BACKFILL_ACTOR,
            idempotency_key="k-1",
            reason="0.2 micron filter",
        )
    row = (
        await session.execute(
            text(
                "SELECT kind, qty_delta, occurred_at, reason, work_order_no "
                "FROM stock_transaction WHERE idempotency_key='k-1'"
            )
        )
    ).one()
    assert row.kind == "ISSUE"
    assert row.qty_delta == Decimal("-3.000")  # 領料為負
    assert row.occurred_at == _OCCURRED  # = date_wo(tz-aware,同一瞬間)
    assert row.reason == "0.2 micron filter"  # descrip provenance(D1)
    assert row.work_order_no == 30167


async def test_issue_part_raises_on_closed_but_backfill_allows(session) -> None:
    """對照:governed issue_part_to_work_order 對 CLOSED(終態)會 raise;回填則允許。"""
    svc = WorkOrderService(session)
    with pytest.raises(WorkOrderError):
        await svc.issue_part_to_work_order(
            work_order_no=30167, item_code="ES001", quantity="1", actor=ACTOR
        )


async def test_loader_duplicate_pair_each_one_row(session) -> None:
    rows = [
        _pi_row("30167", "ES001", qty="2.00"),
        _pi_row("30167", "ES001", qty="2.00"),  # 同 (wo,item) 第 2 列
    ]
    res = await load_part_issues(rows, session)
    assert res.rows_read == 2 and res.inserted == 2 and res.duplicates_skipped == 0

    svc = WorkOrderService(session)
    assert len(await svc.get_parts(30167)) == 2  # 各成一筆,不合併
    cnt = (
        await session.execute(
            text("SELECT count(*) FROM stock_transaction WHERE work_order_no=30167")
        )
    ).scalar()
    assert cnt == 2


async def test_loader_idempotent_rerun(session) -> None:
    rows = [_pi_row("30167", "ES001"), _pi_row("30167", "ES002"), _pi_row("30167", "ES001")]
    r1 = await load_part_issues(rows, session)
    assert r1.inserted == 3
    r2 = await load_part_issues(rows, session)
    assert r2.inserted == 0 and r2.duplicates_skipped == 3  # 重跑全冪等命中

    svc = WorkOrderService(session)
    assert len(await svc.get_parts(30167)) == 3  # 無新增列


async def test_loader_missing_fk_logs_and_skips(session) -> None:
    rows = [
        _pi_row("999999", "ES001", compid="EID-002"),  # WO 缺、compid 有效 → 掛設備救援
        _pi_row("888888", "ES001", compid="EID-999"),  # WO 缺、compid 無效 → 不可救
        _pi_row("30167", "ZZZ999"),  # 不存在的 item → 不可救(先判)
        _pi_row("30167", "ES001"),  # 正常掛工單
    ]
    res = await load_part_issues(rows, session)
    assert res.inserted == 1  # 掛工單
    assert res.rescued_to_asset == 1  # 掛設備救援(ADR-024)
    assert res.missing_wo_skipped == 1  # compid 無效 → 不可救
    assert res.missing_item_skipped == 1
    assert res.missing_wo_samples == ["888888"]
    assert res.missing_item_samples == ["ZZZ999"]
    assert res.rescued_asset_samples == ["999999->EID-002"]

    svc = WorkOrderService(session)
    parts = await svc.get_parts(30167)
    assert len(parts) == 1 and parts[0].item_code == "ES001"  # 僅掛工單那筆有 work_order_part


async def test_backfill_rescues_missing_wo_to_asset(session) -> None:
    """WO 不存在但 compid 有效 → 掛設備(charge_target_asset_id),無 work_order_part(ADR-024)。"""
    svc = WorkOrderService(session)
    async with svc.write(BACKFILL_ACTOR):
        outcome = await svc.backfill_part_issue(
            work_order_no=999999,  # 不存在的 WO
            item_code="ES001",
            quantity="2",
            occurred_at=_OCCURRED,
            actor=BACKFILL_ACTOR,
            idempotency_key="rescue-1",
            asset_id="EID-002",  # 領料列 compid,有效 → 救援退路
        )
    assert outcome is PartIssueOutcome.INSERTED_ASSET
    row = (
        await session.execute(
            text(
                "SELECT kind, qty_delta, work_order_no, charge_target_asset_id "
                "FROM stock_transaction WHERE idempotency_key='rescue-1'"
            )
        )
    ).one()
    assert row.kind == "ISSUE"
    assert row.qty_delta == Decimal("-2.000")
    assert row.work_order_no is None  # 無工單
    assert row.charge_target_asset_id == "EID-002"  # 歸屬設備
    cnt = (await session.execute(text("SELECT count(*) FROM work_order_part"))).scalar()
    assert cnt == 0  # 救援不建 work_order_part


async def test_loader_counts_malformed(session) -> None:
    rows = [_pi_row("", "ES001"), _pi_row("30167", "ES001")]  # 缺 wo → malformed
    res = await load_part_issues(rows, session)
    assert res.malformed_skipped == 1 and res.inserted == 1


async def test_backfill_missing_fk_returns_enum_no_raise(session) -> None:
    svc = WorkOrderService(session)
    async with svc.write(BACKFILL_ACTOR):
        o1 = await svc.backfill_part_issue(
            work_order_no=999999,
            item_code="ES001",
            quantity="1",
            occurred_at=_OCCURRED,
            actor=BACKFILL_ACTOR,
            idempotency_key="m-1",
        )
        o2 = await svc.backfill_part_issue(
            work_order_no=30167,
            item_code="ZZZ999",
            quantity="1",
            occurred_at=_OCCURRED,
            actor=BACKFILL_ACTOR,
            idempotency_key="m-2",
        )
    assert o1 is PartIssueOutcome.MISSING_WORK_ORDER
    assert o2 is PartIssueOutcome.MISSING_ITEM
    # 皆未寫任何帳
    cnt = (await session.execute(text("SELECT count(*) FROM stock_transaction"))).scalar()
    assert cnt == 0
