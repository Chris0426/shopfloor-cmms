"""operator RBAC 白名單 DB 整合測試(iPad 產線共用帳號)。無 Docker 自動 skip。

Jordan 拍板:operator 只准 ① 開 REACTIVE 報修 ② 取消自己開的 OPEN 誤報 ③ 讀取 / 改自己密碼語言。
其餘 governed 寫入一律 raise AuthorizationError(RBAC 在 domain 強制,護欄縱深)。
驗:白名單放行 + 白名單外全拒 + engineer 對照不受影響 + agent/on-box 路徑不受影響。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _a  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.contacts import models as _c  # noqa: E402, F401
from cmms.domain.identity import models as _id  # noqa: E402, F401
from cmms.domain.identity.service import AuthorizationError, IdentityService  # noqa: E402
from cmms.domain.inventory import models as _i  # noqa: E402, F401
from cmms.domain.inventory.service import InventoryService  # noqa: E402
from cmms.domain.pm_schedule import models as _p  # noqa: E402, F401
from cmms.domain.work_order import models as _w  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.models import WoNoteType, WorkType  # noqa: E402
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402

ASSET_ROWS = [
    {
        "compid": "EID-001",
        "comp_desc": "Rig",
        "assettype": "Production",
        "department": "EQ",
        "line_no": "10K",
        "available": "Yes",
    },
]
# 一筆種子工單 → 種 REACTIVE work_type + 狀態機 lookup
SEED_WO = [
    {
        "wo": "24172", "compid": "EID-001", "comp_desc": "y", "assetsubtp": "",
        "brief_desc": "PM", "diag": "", "comments": "", "date_wo": "05/23/26",
        "sch_date": "", "wo_type": "REACTIVE", "workstatus": "O", "miscreated": "F",
        "assignto": "", "edittime": "", "editdate": "", "edituser": "",
        "time": "", "time_cmpl": "",
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
            await load_assets(ASSET_ROWS, s)
            await load_wo(SEED_WO, s)  # seed REACTIVE work_type + 狀態機 lookup
            # 補種:PM work_type(engineer/agent 開 PM 對照)+ note_type FK(report/note)
            svc = WorkOrderService(s)
            async with svc.write(Actor.human("bootstrap")):
                await svc.upsert_lookup(WorkType, "PM", "PM")
                await svc.upsert_lookup(WoNoteType, "report", "報修")
                await svc.upsert_lookup(WoNoteType, "note", "備註")
            yield s
        await engine.dispose()


async def _mk_user(session, uid: str, *, role: str) -> Actor:
    await IdentityService(session).create_user(
        user_id=uid, username=uid, display_name=uid, password="pw-123456",
        org="contractor", role=role, actor=Actor.human("bootstrap"),
    )
    return Actor.human(uid)


# ---- ① 白名單:開 REACTIVE + 取消自己開的 OPEN ----

async def test_operator_can_open_reactive(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=op)
    assert wo.status == "OPEN" and wo.created_by == "human:ipad"


async def test_operator_cannot_open_pm(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    with pytest.raises(AuthorizationError, match="REACTIVE"):
        await svc.open_work_order(asset_id="EID-001", work_type="PM", actor=op)


async def test_operator_can_cancel_own_open(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=op)
    cancelled = await svc.cancel_reactive_report(wo.work_order_no, op, reason="false alarm")
    assert cancelled.status == "CANCELLED"


async def test_operator_cannot_cancel_others_open(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    eng = await _mk_user(session, "alice", role="engineer")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=eng)
    with pytest.raises(AuthorizationError, match="opened"):
        await svc.cancel_reactive_report(wo.work_order_no, op, reason="not mine")
    assert (await svc.get_work_order(wo.work_order_no)).status == "OPEN"  # 未被取消


# ---- add_note:僅本人 OPEN 單的 report 筆 ----

async def test_operator_add_report_note_own_open_ok(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=op)
    note = await svc.add_note(wo.work_order_no, entry_type="report", body="堵塞", actor=op)
    assert note.entry_type == "report" and note.author == "human:ipad"


async def test_operator_add_progress_note_rejected(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=op)
    with pytest.raises(AuthorizationError, match="report notes"):
        await svc.add_note(wo.work_order_no, entry_type="progress", body="更新", actor=op)


async def test_operator_add_note_on_others_wo_rejected(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    eng = await _mk_user(session, "alice", role="engineer")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=eng)
    with pytest.raises(AuthorizationError):
        await svc.add_note(wo.work_order_no, entry_type="report", body="x", actor=op)


# ---- 白名單外的寫入一律拒 ----

async def test_operator_denied_all_other_writes(session) -> None:
    svc = WorkOrderService(session)
    op = await _mk_user(session, "ipad", role="operator")
    eng = await _mk_user(session, "alice", role="engineer")
    # eng 開一張供 operator 嘗試操作(操作前狀態 = OPEN)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=eng)
    no = wo.work_order_no
    with pytest.raises(AuthorizationError, match="start_work"):
        await svc.start_work(no, op)
    with pytest.raises(AuthorizationError, match="set_hold"):
        await svc.set_hold(no, "WAITING_PARTS", op)
    with pytest.raises(AuthorizationError, match="resume_or_start"):
        await svc.resume_or_start(no, op)
    with pytest.raises(AuthorizationError, match="finish_work_order"):
        await svc.finish_work_order(no, op)
    with pytest.raises(AuthorizationError, match="close_work_order"):
        await svc.close_work_order(no, op)
    with pytest.raises(AuthorizationError, match="complete_work"):
        await svc.complete_work(no, op)
    with pytest.raises(AuthorizationError, match="set_assignees"):
        await svc.set_assignees(no, assignees=["Alice Fang"], actor=op)
    with pytest.raises(AuthorizationError, match="set_assignees"):  # set_assignee 委派給它
        await svc.set_assignee(no, assigned_person="Alice Fang", actor=op)
    with pytest.raises(AuthorizationError, match="update_brief_description"):
        await svc.update_brief_description(no, brief_description="x", actor=op)
    with pytest.raises(AuthorizationError, match="issue_part_to_work_order"):
        await svc.issue_part_to_work_order(
            work_order_no=no, item_code="ES000803", quantity=1, actor=op
        )
    with pytest.raises(AuthorizationError, match="record_external_link"):
        await svc.record_external_link(
            work_order_no=no, external_key="MRQ-1", link_type="referenced", actor=op
        )
    with pytest.raises(AuthorizationError, match="generate_pm_work_order"):
        await svc.generate_pm_work_order(pm_id="PMW-1", actor=op)
    with pytest.raises(AuthorizationError, match="propose"):
        await svc.propose(
            operation="void_work_order",
            params={"work_order_no": no, "reason": "x"},
            proposed_by=op,
        )


async def test_operator_denied_inventory_issue(session) -> None:
    """領料(直領到設備)非 operator 職責 —— InventoryService 也閘。"""
    inv = InventoryService(session)
    op = await _mk_user(session, "ipad", role="operator")
    with pytest.raises(AuthorizationError, match="issue_to_asset"):
        await inv.issue_to_asset(
            asset_id="EID-001", item_code="ES000803", quantity=1, actor=op
        )


# ---- engineer 對照:白名單閘不影響 engineer ----

async def test_engineer_unaffected(session) -> None:
    svc = WorkOrderService(session)
    eng = await _mk_user(session, "alice", role="engineer")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=eng)
    started = await svc.start_work(wo.work_order_no, eng)  # 狀態機操作放行
    assert started.status == "IN_PROGRESS"
    # engineer 開 PM 亦不受 operator 閘影響
    pm_wo = await svc.open_work_order(asset_id="EID-001", work_type="PM", actor=eng)
    assert pm_wo.work_type == "PM"


async def test_agent_paths_unaffected(session) -> None:
    """agent actor(非 human)一律非 operator → is_operator False,自動化路徑不受閘影響。"""
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="PM", actor=Actor.agent("mes-pipeline")
    )
    assert wo.work_type == "PM"  # agent 開 PM 不被 operator 閘擋
