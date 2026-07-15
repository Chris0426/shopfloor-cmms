"""A3 視窗查詢(Analytics 消費端需求)`list_active_in_window` 的 DB 整合測試(testcontainers)。

窗語意 = **活躍於窗內**(非 opened 於窗內):工單活躍窗 = opened_at → 首個 COMPLETED/終態;
遷移單無 history → opened_at→closed_at;仍 open → 至今。與 [start, end] 相交即回。
覆蓋:跨窗頭 / 跨窗尾 / 整窗內 / 包住整窗 / 窗外不回 / 仍 open 至今 / 遷移單 fallback /
asset_id 過濾 / truncated 上限。本機無 Docker 自動 skip。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402
from cmms.domain.work_order.transform import TAIPEI  # noqa: E402

ACTOR = Actor.human("tester")

ASSET_ROWS = [
    {"compid": "EID-001", "comp_desc": "Rig 1", "assettype": "Production",
     "department": "EQ", "line_no": "10K", "available": "Yes"},
    {"compid": "EID-002", "comp_desc": "Rig 2", "assettype": "Production",
     "department": "EQ", "line_no": "10K", "available": "Yes"},
]

# 一張遷移(loader)工單:opened 05/21 10:00、closed 05/21 15:00、**無 status_history**
MIGRATION_WO = [{
    "wo": "30167", "compid": "EID-002", "comp_desc": "x", "assetsubtp": "",
    "brief_desc": "fix", "diag": "", "comments": "",
    "date_wo": "05/21/26", "sch_date": "", "wo_type": "REACTIVE", "workstatus": "H",
    "miscreated": "F", "assignto": "CMA (Lin Hsu)", "edittime": "15:00:00",
    "editdate": "05/21/26", "edituser": "T", "time": "10:00:00", "time_cmpl": "15:00:00",
}]


def _t(day: int, h: int, m: int = 0) -> datetime:
    """2026-05-<day> 廠區台北(TAIPEI)tz-aware 時間戳。"""
    return datetime(2026, 5, day, h, m, tzinfo=TAIPEI)


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
            await load_wo(MIGRATION_WO, s)  # 種 lookup(work_type/status/hold)+ 遷移單 30167
            yield s
        await engine.dispose()


async def _live(svc: WorkOrderService, opened: datetime, completed: datetime | None) -> int:
    """建 live 工單(EID-001):open→start;completed 給定則 complete。活躍窗尾 = completed(或 now)。"""
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=opened
    )
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=opened + timedelta(minutes=1))
    if completed is not None:
        await svc.complete_work(no, ACTOR, at=completed)
    return no


async def test_window_intersection_boundaries(session) -> None:
    """跨窗頭 / 跨窗尾 / 整窗內 / 包住整窗 / 窗外(前後)/ 仍 open 至今 —— 一次覆蓋。"""
    svc = WorkOrderService(session)
    a = await _live(svc, _t(20, 10, 0), _t(20, 12, 0))   # [10:00,12:00] 跨窗頭
    _b = await _live(svc, _t(20, 8, 0), _t(20, 9, 0))    # [08:00,09:00] 窗前 → 不回
    _c = await _live(svc, _t(20, 14, 0), _t(20, 16, 0))  # [14:00,16:00] 窗後 → 不回
    d = await _live(svc, _t(20, 9, 0), _t(20, 18, 0))    # [09:00,18:00] 包住整窗
    e = await _live(svc, _t(20, 11, 0), None)            # [11:00, now] 仍 open 至今
    g = await _live(svc, _t(20, 11, 45), _t(20, 12, 15))  # [11:45,12:15] 整窗內
    h = await _live(svc, _t(20, 12, 15), _t(20, 13, 30))  # [12:15,13:30] 跨窗尾

    # 查詢窗 [11:30, 12:30],now 固定 13:00(讓 e 的活躍窗尾 = 13:00,可重現)
    rows, truncated = await svc.list_active_in_window(
        start=_t(20, 11, 30), end=_t(20, 12, 30), at=_t(20, 13, 0)
    )
    got = {wo.work_order_no for wo, _hist in rows}
    assert got == {a, d, e, g, h}
    assert truncated is False


async def test_asset_id_filter(session) -> None:
    svc = WorkOrderService(session)
    await _live(svc, _t(20, 11, 0), _t(20, 12, 0))  # EID-001
    rows_1, _ = await svc.list_active_in_window(
        start=_t(20, 11, 0), end=_t(20, 12, 0), asset_id="EID-001", at=_t(20, 13, 0)
    )
    rows_2, _ = await svc.list_active_in_window(
        start=_t(20, 11, 0), end=_t(20, 12, 0), asset_id="EID-002", at=_t(20, 13, 0)
    )
    assert len(rows_1) == 1 and rows_1[0][0].asset_id == "EID-001"
    assert rows_2 == []  # EID-002 該窗無活躍(遷移單 30167 在 05/21)


async def test_migration_wo_fallback_no_history(session) -> None:
    """遷移單無 status_history → 活躍窗 = opened_at→closed_at(30167:05/21 10:00–15:00)。"""
    svc = WorkOrderService(session)
    # 窗 [05/21 14:00, 16:00] 與 [10:00,15:00] 相交 → 回;且 status_history 為空(fallback 佐證)
    rows, _ = await svc.list_active_in_window(start=_t(21, 14, 0), end=_t(21, 16, 0))
    got = {wo.work_order_no: hist for wo, hist in rows}
    assert 30167 in got
    assert got[30167] == []  # 遷移單無 history
    # 窗 [05/21 16:00, 17:00] 在 closed(15:00)之後 → 不回
    rows2, _ = await svc.list_active_in_window(start=_t(21, 16, 0), end=_t(21, 17, 0))
    assert {wo.work_order_no for wo, _ in rows2} == set()


async def test_inline_status_history_shape(session) -> None:
    """live 工單回傳含 inline 全量 status_history(升冪),供路由序列化成 分析平台契約形。"""
    svc = WorkOrderService(session)
    no = await _live(svc, _t(20, 11, 0), _t(20, 12, 0))
    rows, _ = await svc.list_active_in_window(
        start=_t(20, 11, 0), end=_t(20, 12, 30), at=_t(20, 13, 0)
    )
    hist = next(h for wo, h in rows if wo.work_order_no == no)
    # OPEN → IN_PROGRESS → COMPLETED(升冪);首個 COMPLETED = 活躍窗尾
    assert [h.to_status for h in hist] == ["OPEN", "IN_PROGRESS", "COMPLETED"]


async def test_truncated_flag(session) -> None:
    """粗篩候選 > cap → truncated=True(v1 上限截斷誠實回報)。"""
    svc = WorkOrderService(session)
    for _ in range(3):
        await _live(svc, _t(20, 11, 0), _t(20, 12, 0))
    rows, truncated = await svc.list_active_in_window(
        start=_t(20, 11, 0), end=_t(20, 12, 0), cap=2, at=_t(20, 13, 0)
    )
    assert truncated is True
    assert len(rows) <= 2
