"""WorkOrder 切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip。#4a 讀取 + #4b 寫入引擎:載入/idempotent/html.unescape、
miscreated 過濾、狀態映射、歷史 downtime;狀態機生命週期 + downtime 精算(試跑不計、等料計入、
扣非生產時段)、領料連動扣庫存 + idempotency、狀態機護欄。
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
from cmms.domain.failure_vocab.models import EquipmentFailureCode  # noqa: E402
from cmms.domain.identity.service import AuthorizationError, IdentityService  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.loader import load as load_inv  # noqa: E402
from cmms.domain.inventory.service import InventoryService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.models import WoNoteType  # noqa: E402
from cmms.domain.work_order.service import (  # noqa: E402
    BACKFILL_ACTOR,
    InvalidTransition,
    PartIssueOutcome,
    WorkOrderError,
    WorkOrderService,
)
from cmms.domain.work_order.transform import TAIPEI  # noqa: E402

ACTOR = Actor.human("tester")


async def _seed_admin(session, user_id: str = "admin1") -> Actor:
    """種一個 active admin 帳號(void / confirm-void 的 domain 角色守門需要,review f14cf8d)。"""
    await IdentityService(session).create_user(
        user_id=user_id, username=user_id, display_name=user_id, password="pw-123456",
        org="Shopfloor", actor=Actor.human("bootstrap"), role="admin",
    )
    return Actor.human(user_id)

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

WO_ROWS = [
    # 已結案 REACTIVE,中文 html-entity,CMA;同日 10:00→15:00 = 300 分(全生產時段),estimated
    {
        "wo": "30167",
        "compid": "EID-002",
        "comp_desc": "x",
        "assetsubtp": "STA4 GEN2",
        "brief_desc": "&#32784;&#29105;&#33184;&#24067;&#30772;&#25613;",
        "diag": "&#32791;&#26448;&#26356;&#25563;",
        "comments": "MRQ-4220",
        "date_wo": "05/21/26",
        "sch_date": "",
        "wo_type": "REACTIVE",
        "workstatus": "H",
        "miscreated": "F",
        "assignto": "CMA (Lin Hsu)",
        "edittime": "15:00:00",
        "editdate": "05/21/26",
        "edituser": "SAMWU99",
        "time": "10:00:00",
        "time_cmpl": "15:00:00",
    },
    # 開立中 PM,SF(新 vendor),AM/PM 時間
    {
        "wo": "24172",
        "compid": "EID-001",
        "comp_desc": "y",
        "assetsubtp": "",
        "brief_desc": "Annual Maintenance",
        "diag": "",
        "comments": "",
        "date_wo": "05/23/26",
        "sch_date": "06/01/26",
        "wo_type": "PM",
        "workstatus": "O",
        "miscreated": "F",
        "assignto": "SF (Self)",
        "edittime": "",
        "editdate": "",
        "edituser": "",
        "time": "09:00:00 AM",
        "time_cmpl": "",
    },
    # 誤開 → 應被過濾(不匯入)
    {
        "wo": "30168",
        "compid": "EID-001",
        "comp_desc": "z",
        "assetsubtp": "",
        "brief_desc": "oops",
        "diag": "",
        "comments": "",
        "date_wo": "05/22/26",
        "sch_date": "",
        "wo_type": "REACTIVE",
        "workstatus": "O",
        "miscreated": "T",
        "assignto": "",
        "edittime": "",
        "editdate": "",
        "edituser": "",
        "time": "",
        "time_cmpl": "",
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
            await load_assets(ASSET_ROWS, s)  # FK 前置
            await load_inv(INV_ROWS, s)  # 領料測試用品項
            yield s
        await engine.dispose()


def _ts(h: int, m: int = 0) -> datetime:
    """2026-05-20 當地(Taipei)時間戳(生產時段內,便於 downtime 斷言)。"""
    return datetime(2026, 5, 20, h, m, tzinfo=TAIPEI)


async def test_load_filters_miscreated_and_maps_status(session) -> None:
    res = await load_wo(WO_ROWS, session)
    assert res.work_orders == 2 and res.filtered_miscreated == 1  # 30168 誤開被丟棄
    assert res.wo_statuses == 7 and res.wo_hold_reasons == 5 and res.stock_txn_kinds == 4

    svc = WorkOrderService(session)
    assert await svc.get_work_order(30168) is None  # 誤開未匯入
    wo = await svc.get_work_order(30167)
    assert wo.status == "CLOSED"  # H → CLOSED
    assert wo.brief_description == "耐熱膠布破損"
    assert wo.assigned_vendor == "CMA" and wo.assigned_person == "Lin Hsu"
    # 歷史 downtime:10:00→15:00 全生產時段 = 300 分,estimated
    assert wo.downtime_minutes == 300 and wo.downtime_estimated is True
    wo2 = await svc.get_work_order(24172)
    assert wo2.status == "OPEN" and wo2.downtime_minutes is None


async def test_lifecycle_and_downtime(session) -> None:
    await load_wo(WO_ROWS, session)  # 種 lookup + work_type
    svc = WorkOrderService(session)

    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(10, 0)
    )
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10, 30))  # OPEN 10:00–10:30 = 30(down)
    await svc.hold_work(no, "TEST_RUN", ACTOR, at=_ts(11, 30))  # IN_PROG 60(down)
    await svc.resume_work(no, ACTOR, at=_ts(12, 0))  # ON_HOLD/TEST_RUN 30(不計)
    await svc.hold_work(no, "WAITING_PARTS", ACTOR, at=_ts(12, 30))  # IN_PROG 30(down)
    await svc.resume_work(no, ACTOR, at=_ts(14, 30))  # ON_HOLD/等料 120(計入)
    await svc.complete_work(no, ACTOR, at=_ts(15, 0))  # IN_PROG 30(down)
    wo = await svc.close_work_order(no, ACTOR, at=_ts(16, 0))  # COMPLETED 60(不計)

    assert wo.status == "CLOSED"
    # downtime = OPEN30 + IN60 + IN30 + 等料120 + IN30 = 270(試跑30、COMPLETED60 不計)
    assert wo.downtime_minutes == 270
    assert wo.downtime_estimated is False  # 系統時間戳精算
    history = await svc.get_status_history(no)
    assert [h.to_status for h in history] == [
        "OPEN",
        "IN_PROGRESS",
        "ON_HOLD",
        "IN_PROGRESS",
        "ON_HOLD",
        "IN_PROGRESS",
        "COMPLETED",
        "CLOSED",
    ]


async def test_pm_open_segment_not_downtime(session) -> None:
    """內部規格:PM 單 OPEN 段機台照跑不計 downtime;工程師切 IN_PROGRESS 才起算。

    OPEN→IN_PROGRESS→COMPLETED→CLOSED,downtime 只含 IN_PROGRESS 段(PM OPEN 段排除,
    對比 REACTIVE 生命週期 OPEN 段計入)。**不回溯**——引擎僅在此次結案時跑。
    """
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)

    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="PM", actor=ACTOR, at=_ts(9, 0)
    )
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10, 0))  # PM OPEN 9:00–10:00 = 60(不計)
    await svc.complete_work(no, ACTOR, at=_ts(11, 0))  # IN_PROGRESS 10:00–11:00 = 60(計入)
    wo = await svc.close_work_order(no, ACTOR, at=_ts(12, 0))  # COMPLETED 60(不計)

    assert wo.status == "CLOSED"
    assert wo.downtime_minutes == 60  # 只含 IN_PROGRESS 段;PM OPEN 段不計
    assert wo.downtime_estimated is False


async def test_invalid_transition_guarded(session) -> None:
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="PM", actor=ACTOR, at=_ts(9))
    # 先取號:operator RBAC 閘在 write() 前查帳號(autobegin)→ 失敗 rollback 會 expire ORM
    # 實例(比照 close/finish 既有的交易外先驗行為),失敗後不可再 lazy 讀 wo 屬性。
    no = wo.work_order_no
    with pytest.raises(InvalidTransition):
        await svc.complete_work(no, ACTOR, at=_ts(10))  # OPEN→COMPLETED 不允許
    # close 後為終態,不可再轉
    await svc.cancel_reactive_report(no, ACTOR, at=_ts(10))  # OPEN→CANCELLED
    with pytest.raises(InvalidTransition):
        await svc.start_work(no, ACTOR, at=_ts(11))


async def test_reactive_open_guarded_by_asset_state(session) -> None:
    """REACTIVE 開單 domain 守門(#4b,所有通道一致):在籍照常;退役拒;未知 EID 拒。

    退役機台已不在產線,不該再產生 reactive/downtime 工單;守門在 domain 層 = web 報修 /
    on-box / confirm / 未來 MES 一致生效。
    """
    from cmms.domain.asset.service import AssetService
    await load_wo(WO_ROWS, session)
    admin = await _seed_admin(session)
    svc = WorkOrderService(session)
    # 在籍(預設 is_active=true)→ 照常開
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9)
    )
    assert wo.status == "OPEN"
    # 退役 EID-002 → 拒(admin governed setter 標退役後)
    await AssetService(session).set_asset_active("EID-002", False, actor=admin)
    with pytest.raises(WorkOrderError):
        await svc.open_work_order(
            asset_id="EID-002", work_type="REACTIVE", actor=ACTOR, at=_ts(10)
        )
    # 未知 EID → 也拒(不靠 FK 崩,顯性 domain 錯誤)
    with pytest.raises(WorkOrderError):
        await svc.open_work_order(
            asset_id="EID-NOPE", work_type="REACTIVE", actor=ACTOR, at=_ts(10)
        )
    # PM 生成不套此擋:退役資產仍可(排程決定論,另循環)—— 驗 PM 不被 REACTIVE 守門誤擋
    pm_wo = await svc.open_work_order(
        asset_id="EID-002", work_type="PM", actor=ACTOR, at=_ts(11)
    )
    assert pm_wo.status == "OPEN"


async def test_hold_requires_reason(session) -> None:
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="PM", actor=ACTOR, at=_ts(9))
    await svc.start_work(wo.work_order_no, ACTOR, at=_ts(10))
    with pytest.raises(WorkOrderError):
        await svc.hold_work(wo.work_order_no, "", ACTOR, at=_ts(11))


async def test_issue_part_decrements_inventory_and_idempotent(session) -> None:
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    inv = InventoryService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))

    ok = await svc.issue_part_to_work_order(
        work_order_no=no, item_code="ES001", quantity="3", actor=ACTOR, idempotency_key="k1"
    )
    assert ok is True
    item = await inv.get_item("ES001")
    assert item.quantity_on_hand == Decimal("7.000")  # 10 - 3
    parts = await svc.get_parts(no)
    assert len(parts) == 1 and parts[0].item_code == "ES001"
    # stock_transaction ISSUE -3
    delta = (
        await session.execute(text("SELECT qty_delta FROM stock_transaction WHERE kind='ISSUE'"))
    ).scalar()
    assert delta == Decimal("-3.000")

    # idempotent:同 key 重送 → 跳過,不重複扣帳
    again = await svc.issue_part_to_work_order(
        work_order_no=no, item_code="ES001", quantity="3", actor=ACTOR, idempotency_key="k1"
    )
    assert again is False
    item = await inv.get_item("ES001")
    assert item.quantity_on_hand == Decimal("7.000")  # 未再扣


async def test_issue_part_rejected_on_terminal(session) -> None:
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="PM", actor=ACTOR, at=_ts(9))
    await svc.cancel_reactive_report(wo.work_order_no, ACTOR, at=_ts(10))  # → CANCELLED(終態)
    with pytest.raises(WorkOrderError):
        await svc.issue_part_to_work_order(
            work_order_no=wo.work_order_no, item_code="ES001", quantity="1", actor=ACTOR
        )


async def test_load_is_idempotent(session) -> None:
    await load_wo(WO_ROWS, session)
    res2 = await load_wo(WO_ROWS, session)
    assert res2.work_orders == 2
    svc = WorkOrderService(session)
    assert len(await svc.list_work_orders(limit=1000)) == 2


# ---- 2026-07-03 批:指派 / 日誌更正 / 完工欄位 / 人類作廢提案 ----


async def _seed_note_types(session) -> None:
    """種 wo_note_type lookup(prod 由 migration 0012 seed;本檔 create_all 需自種)。"""
    svc = WorkOrderService(session)
    async with svc.write(ACTOR):
        for code in ("report", "progress", "hold", "resume", "note"):
            await svc.upsert_lookup(WoNoteType, code, code)


async def test_set_assignee_and_open_with_owner(session) -> None:
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    # 開單即指派
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR,
        assigned_person="Alice Fang", at=_ts(9),
    )
    assert wo.assigned_person == "Alice Fang"
    # 改派 + 清除
    wo = await svc.set_assignee(wo.work_order_no, assigned_person="Ben Yeh", actor=ACTOR)
    assert wo.assigned_person == "Ben Yeh" and wo.updated_by == ACTOR.value
    wo = await svc.set_assignee(wo.work_order_no, assigned_person="  ", actor=ACTOR)
    assert wo.assigned_person is None
    # 終態不可改派
    await svc.cancel_reactive_report(wo.work_order_no, ACTOR, at=_ts(10))
    with pytest.raises(WorkOrderError):
        await svc.set_assignee(wo.work_order_no, assigned_person="X", actor=ACTOR)
    # 自動完成:歷史指派名(工單 ∪ PM)
    names = await svc.list_assignee_suggestions("lin")
    assert names == ["Lin Hsu"]  # 來自載入的 30167


async def test_set_assignees_multi_and_mine_filter(session) -> None:
    """0031:set_assignees 整組替換交叉表 + 同步 denormalized 首位;Mine 過濾命中第二位負責人。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR,
        assignees=["Alice Fang", "Ben Yeh"], at=_ts(9),
    )
    assert await svc.get_assignees(wo.work_order_no) == ["Alice Fang", "Ben Yeh"]
    assert wo.assigned_person == "Alice Fang"  # denormalized 首位
    # Mine 過濾:以第二位負責人查得該工單(EXISTS 交叉表)
    mine2 = await svc.list_work_orders(assigned_person="Ben Yeh")
    assert wo.work_order_no in {w.work_order_no for w in mine2}
    # 改派為單一負責人 → 首位同步、交叉表整組替換
    wo = await svc.set_assignees(wo.work_order_no, assignees=["Cara Lo"], actor=ACTOR)
    assert await svc.get_assignees(wo.work_order_no) == ["Cara Lo"]
    assert wo.assigned_person == "Cara Lo"
    assert await svc.list_work_orders(assigned_person="Ben Yeh") == []  # 舊負責人已移除
    # assignees_map 批次
    assert await svc.assignees_map([wo.work_order_no]) == {wo.work_order_no: ["Cara Lo"]}
    # 清空
    wo = await svc.set_assignees(wo.work_order_no, assignees=[], actor=ACTOR)
    assert await svc.get_assignees(wo.work_order_no) == [] and wo.assigned_person is None


async def test_reactive_open_defaults_assignees_to_asset_owners(session) -> None:
    """0031:REACTIVE 開單未指派 → 衍生設備負責人(全部);明確指派仍勝出;無負責人 → 維持未指派。"""
    from cmms.domain.asset.service import AssetService

    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    asvc = AssetService(session)
    admin = await _seed_admin(session)
    await asvc.set_owners("EID-001", ["Owner Bob", "Ben Yeh"], admin)
    # 未指派 → 帶入設備全部負責人(交叉表 + denormalized 首位)
    wo = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9)
    )
    assert await svc.get_assignees(wo.work_order_no) == ["Owner Bob", "Ben Yeh"]
    assert wo.assigned_person == "Owner Bob"  # denormalized 首位
    # 明確指派勝過設備負責人
    wo2 = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR,
        assignees=["Alice Fang"], at=_ts(10),
    )
    assert await svc.get_assignees(wo2.work_order_no) == ["Alice Fang"]
    # 無負責人 → 維持未指派(domain 不硬拒空,on-box Profile B 須可運作)
    await asvc.set_owners("EID-001", [], admin)
    wo3 = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(11)
    )
    assert await svc.get_assignees(wo3.work_order_no) == []
    assert wo3.assigned_person is None


async def test_update_note_author_gate_and_edit_marker(session) -> None:
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    note = await svc.add_note(wo.work_order_no, entry_type="report", body="原文", actor=ACTOR)
    assert note.updated_at is None  # 未編輯
    # 本人可改;updated_at/updated_by 記「已編輯」
    edited = await svc.update_note(note.id, body="更正後", actor=ACTOR)
    assert edited.body == "更正後" and edited.updated_by == ACTOR.value
    await session.refresh(edited)  # updated_at 為 server onupdate → 明確 refresh 再讀(async 安全)
    assert edited.updated_at is not None
    # 非本人(非 admin)不可改;admin 身分由 domain 解析(user_account.role,不信 caller 自報)
    other = Actor.human("someone-else")
    with pytest.raises(WorkOrderError):
        await svc.update_note(note.id, body="偷改", actor=other)
    admin = await _seed_admin(session, "note-admin")
    ok = await svc.update_note(note.id, body="admin 代改", actor=admin)
    assert ok.body == "admin 代改"
    # 歸屬守門:note 不屬指定工單 → 拒
    with pytest.raises(WorkOrderError):
        await svc.update_note(note.id, body="x", actor=ACTOR, work_order_no=999999)
    # 空 body 拒絕
    with pytest.raises(WorkOrderError):
        await svc.update_note(note.id, body="   ", actor=ACTOR)


async def test_update_brief_description(session) -> None:
    """故障簡述補填/更正(2026-07-06):開單留空 → 事後補;空→None;終態限 admin;不存在拒。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    assert wo.brief_description is None  # 作業員第一時間開單、留空
    # 非終態:任何登入者可補填,稽核欄記 who
    updated = await svc.update_brief_description(no, brief_description="吸嘴堵塞", actor=ACTOR)
    assert updated.brief_description == "吸嘴堵塞" and updated.updated_by == ACTOR.value
    # 空字串 → None(語意=未填)
    cleared = await svc.update_brief_description(no, brief_description="   ", actor=ACTOR)
    assert cleared.brief_description is None
    # 內容未變 → no-op(不報錯)
    same = await svc.update_brief_description(no, brief_description=None, actor=ACTOR)
    assert same.brief_description is None
    # 走到終態(CLOSED)
    await svc.start_work(no, ACTOR, at=_ts(10))
    await svc.complete_work(no, ACTOR, at=_ts(11), action_taken="修好")
    await svc.close_work_order(no, ACTOR, at=_ts(12))
    # 終態:非 admin 拒
    with pytest.raises(WorkOrderError):
        await svc.update_brief_description(no, brief_description="偷改", actor=ACTOR)
    # 終態:admin 可更正
    admin = await _seed_admin(session, "brief-admin")
    ok = await svc.update_brief_description(no, brief_description="admin 更正", actor=admin)
    assert ok.brief_description == "admin 更正"
    # 找不到工單 → 拒
    with pytest.raises(WorkOrderError):
        await svc.update_brief_description(999999, brief_description="x", actor=ACTOR)


async def test_complete_records_action_taken_and_labor(session) -> None:
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))
    wo = await svc.complete_work(
        no, ACTOR, at=_ts(11), action_taken="更換真空泵", labor_hours="1.5"
    )
    assert wo.action_taken == "更換真空泵" and wo.labor_hours == Decimal("1.5")
    # close 未帶 → 不覆寫
    wo = await svc.close_work_order(no, ACTOR, at=_ts(12))
    assert wo.action_taken == "更換真空泵"


async def test_hold_note_and_machine_window_not_downtime(session) -> None:
    """轉等待可附延誤說明(落 hold note、連結 status_history);等機台空檔不計 downtime。"""
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))  # OPEN 60(down)
    # 等機台空檔(機台運轉中)120 分 → 不計 downtime;說明落 hold note
    await svc.hold_work(
        no, "WAITING_MACHINE_TIME", ACTOR, at=_ts(11),
        note_body="機台恢復可 run,等產線空檔 pull down 換泵",
    )
    await svc.resume_work(no, ACTOR, at=_ts(13), note_body="產線放行,開始更換")
    await svc.complete_work(no, ACTOR, at=_ts(14))  # IN_PROG 10:00-11:00 + 13:00-14:00
    wo = await svc.close_work_order(no, ACTOR, at=_ts(15))
    # downtime = OPEN60 + IN60 + IN60 = 180(等機台空檔 120 不計)
    assert wo.downtime_minutes == 180
    notes = await svc.list_notes(no)
    hold_notes = [n for n in notes if n.entry_type == "hold"]
    resume_notes = [n for n in notes if n.entry_type == "resume"]
    assert len(hold_notes) == 1 and "pull down" in hold_notes[0].body
    assert hold_notes[0].status_history_id is not None  # 連結該次轉移(可追溯延誤區段)
    assert len(resume_notes) == 1 and resume_notes[0].status_history_id is not None


async def test_human_can_propose_void_agent_cannot(session) -> None:
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    await svc.start_work(wo.work_order_no, ACTOR, at=_ts(10))
    # agent 不得提案 void(Profile A 維持不放寬)
    with pytest.raises(WorkOrderError):
        await svc.propose(
            operation="void_work_order",
            params={"work_order_no": wo.work_order_no, "reason": "x"},
            proposed_by=Actor.agent("analytics"),
        )
    # 工程師(human)可請求作廢 → admin confirm 執行:VOIDED + 事由 note
    p = await svc.propose(
        operation="void_work_order",
        params={"work_order_no": wo.work_order_no, "reason": "重複開單"},
        proposed_by=Actor.human("engineer1"),
    )
    assert p.dry_run_diff["to_status"] == "VOIDED"
    # 同單重複請求 → 回既有 PENDING(propose 端防重,review f14cf8d)
    p_dup = await svc.propose(
        operation="void_work_order",
        params={"work_order_no": wo.work_order_no, "reason": "又按一次"},
        proposed_by=Actor.human("engineer2"),
    )
    assert p_dup.pending_token == p.pending_token
    # confirm 高風險 op:自報 human id 不等於審核權 —— 非 admin 帳號 → 拒(review f14cf8d)
    with pytest.raises(AuthorizationError):
        await svc.confirm(pending_token=p.pending_token, confirmer=Actor.human("engineer1"))
    admin = await _seed_admin(session, "admin1")
    voided = await svc.confirm(pending_token=p.pending_token, confirmer=admin)
    assert voided.status == "VOIDED"
    notes = await svc.list_notes(wo.work_order_no)
    reason_notes = [n for n in notes if n.body == "重複開單" and n.entry_type == "note"]
    assert len(reason_notes) == 1
    assert reason_notes[0].status_history_id is not None  # 事由 note 連結該次 VOIDED 轉移


async def test_review_fix_expired_proposal_persists_and_sweeps(session) -> None:
    """review f14cf8d C1/W7:過期提案 confirm 時 EXPIRED 要真正落庫(不隨 raise 回滾);
    lazy sweep 可批次清逾期 PENDING;過期後同單可再提(find_pending 濾掉過期)。"""
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    await svc.start_work(wo.work_order_no, ACTOR, at=_ts(10))
    p = await svc.propose(
        operation="void_work_order",
        params={"work_order_no": wo.work_order_no, "reason": "x"},
        proposed_by=Actor.human("eng1"), ttl_seconds=60, at=_ts(10),
    )
    with pytest.raises(WorkOrderError, match="expired"):
        await svc.confirm(pending_token=p.pending_token, confirmer=ACTOR, at=_ts(12))
    expired = await svc.list_proposals(status="EXPIRED")
    assert [e.pending_token for e in expired] == [p.pending_token]  # 標記活過 raise
    # 過期後 pending 偵測不再誤報 → 同單可重新請求
    assert await svc.find_pending_proposal(
        operation="void_work_order", work_order_no=wo.work_order_no, at=_ts(12)
    ) is None
    p2 = await svc.propose(
        operation="void_work_order",
        params={"work_order_no": wo.work_order_no, "reason": "y"},
        proposed_by=Actor.human("eng1"), ttl_seconds=60, at=_ts(12),
    )
    assert p2.pending_token != p.pending_token
    # lazy sweep:逾期 PENDING → EXPIRED
    assert await svc.expire_stale_proposals(actor=ACTOR, at=_ts(14)) == 1
    assert await svc.list_proposals(status="PENDING") == []


async def test_review_fix_void_state_guard_and_admin_gate(session) -> None:
    """review f14cf8d C5/A1:COMPLETED 無 →VOIDED 轉移 → 提案端先擋;void 本體 admin 限定
    在 domain 強制(route 藏按鈕不是授權),reason 與轉移同交易落 note。"""
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))
    await svc.complete_work(no, ACTOR, at=_ts(11))
    with pytest.raises(WorkOrderError, match="cannot void"):
        await svc.propose(
            operation="void_work_order",
            params={"work_order_no": no, "reason": "x"},
            proposed_by=Actor.human("eng1"),
        )
    # 回 IN_PROGRESS(可作廢)→ 非 admin 直接 void 在 domain 被拒
    await svc.start_work(no, ACTOR, at=_ts(12))
    with pytest.raises(AuthorizationError):
        await svc.void_work_order(no, ACTOR, at=_ts(13))
    admin = await _seed_admin(session, "void-admin")
    voided = await svc.void_work_order(no, admin, at=_ts(13), reason="重複開單(admin)")
    assert voided.status == "VOIDED"
    notes = await svc.list_notes(no)
    rn = [n for n in notes if n.body == "重複開單(admin)"]
    assert len(rn) == 1 and rn[0].status_history_id is not None  # 同交易 + 連結該次轉移


async def test_review_fix_labor_hours_and_qty_guards(session) -> None:
    """review f14cf8d C2/C3:壞工時輸入在轉移前擋下(不整筆默默回滾);負數/零領料拒收。"""
    await load_wo(WO_ROWS, session)
    inv_res = await load_inv([_inv_row("EC000807", onhand="10")], session)
    assert inv_res.items == 1
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))
    with pytest.raises(WorkOrderError, match="labor_hours"):
        await svc.complete_work(no, ACTOR, at=_ts(11), labor_hours="2,5")
    assert (await svc.get_work_order(no)).status == "IN_PROGRESS"  # 轉移未被吃掉
    with pytest.raises(WorkOrderError, match="positive"):
        await svc.issue_part_to_work_order(
            work_order_no=no, item_code="EC000807", quantity="-2", actor=ACTOR,
        )
    with pytest.raises(WorkOrderError, match="positive"):
        await svc.issue_part_to_work_order(
            work_order_no=no, item_code="EC000807", quantity="0", actor=ACTOR,
        )
    onhand = (await session.execute(
        text("SELECT quantity_on_hand FROM inventory_item WHERE item_code = 'EC000807'")
    )).scalar()
    assert onhand == Decimal("10")  # 庫存不因壞輸入而動


async def test_review_fix_note_terminal_freeze_and_link_removal(session) -> None:
    """review f14cf8d S8/S4:終態工單日誌凍結(本人不可改,admin 可);MRQ 連結可軟移除+復活。"""
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    note = await svc.add_note(no, entry_type="progress", body="原文", actor=ACTOR)
    link = await svc.record_external_link(
        work_order_no=no, external_key="MRQ-1243", link_type="referenced", actor=ACTOR,
    )
    await svc.cancel_reactive_report(no, ACTOR, at=_ts(10), reason="誤開")
    # 終態凍結:本人不得再改;admin 可更正
    with pytest.raises(WorkOrderError, match="frozen"):
        await svc.update_note(note.id, body="事後偷改", actor=ACTOR)
    admin = await _seed_admin(session, "freeze-admin")
    ok = await svc.update_note(note.id, body="admin 更正", actor=admin)
    assert ok.body == "admin 更正"
    # 取消事由 note 同交易寫入並連結該次轉移
    cancel_notes = [n for n in await svc.list_notes(no) if n.body == "誤開"]
    assert len(cancel_notes) == 1 and cancel_notes[0].status_history_id is not None
    # MRQ 連結軟移除(留稽核)→ 讀取面消失 → 再連 = 復活同一列
    await svc.remove_external_link(link.id, work_order_no=no, actor=ACTOR)
    assert await svc.list_external_links(no) == []
    revived = await svc.record_external_link(
        work_order_no=no, external_key="MRQ-1243", link_type="referenced", actor=ACTOR,
    )
    assert revived.id == link.id and revived.removed_at is None
    assert [x.id for x in await svc.list_external_links(no)] == [link.id]


async def test_item_edit_proposal_flow(session) -> None:
    """裁決 #3 後續:品項主檔修改的 engineer 提案流(與作廢請求同機制)。
    propose 驗品項存在+數字格式+同品項去重;dry-run diff 只列會變欄位;
    confirm 限 active admin,執行走 inventory 單一寫入路徑(同一交易)。"""
    from cmms.domain.inventory.service import InventoryService as InvSvc

    await load_wo(WO_ROWS, session)
    # bin_location 受控詞彙(0028):confirm 走 _update_item_impl 驗 bin,B-07 須為既存 active bin
    await session.execute(text(
        "INSERT INTO storage_bin (code, is_active) VALUES ('B-07', true)"
    ))
    await session.commit()
    svc = WorkOrderService(session)
    params = {
        "item_code": "ES001", "name": "新品名", "description": "", "vendor_part_no": "",
        "bin_location": "B-07", "reorder_point": "8", "reorder_quantity": "20",
        "lead_time_weeks": "6", "unit_cost": "12.5", "supplier": "", "supplier_org_id": "",
        "weblink": "", "comment": "", "is_stocked": True, "is_obsolete": False,
    }
    # 未知品項 / 壞數字 → 提案端就擋
    with pytest.raises(WorkOrderError, match="not found"):
        await svc.propose(operation="update_item", params={**params, "item_code": "NOPE"},
                          proposed_by=Actor.human("eng1"))
    with pytest.raises(WorkOrderError, match="invalid item field"):
        await svc.propose(operation="update_item",
                          params={**params, "reorder_quantity": "2,5"},
                          proposed_by=Actor.human("eng1"))
    # agent 不得提案(Profile A 不含 update_item)
    with pytest.raises(WorkOrderError, match="not proposable"):
        await svc.propose(operation="update_item", params=params,
                          proposed_by=Actor.agent("analytics"))
    p = await svc.propose(operation="update_item", params=params,
                          proposed_by=Actor.human("eng1"))
    assert p.dry_run_diff["action"] == "update_item"
    assert p.dry_run_diff["changes"]["name"]["to"] == "新品名"  # 只列會變欄位
    # 同品項待審中 → 再提回既有(去重)
    p2 = await svc.propose(operation="update_item", params=params,
                           proposed_by=Actor.human("eng2"))
    assert p2.pending_token == p.pending_token
    # confirm:非 admin 拒;admin 執行 → 品項落地
    with pytest.raises(AuthorizationError):
        await svc.confirm(pending_token=p.pending_token, confirmer=Actor.human("eng1"))
    admin = await _seed_admin(session, "item-admin")
    await svc.confirm(pending_token=p.pending_token, confirmer=admin)
    item = await InvSvc(session).get_item("ES001")
    assert item.name == "新品名" and item.reorder_quantity == Decimal("20")
    assert item.lead_time_weeks == 6 and item.source_actor == admin.value


# ---- ADR-019 admin batch 2:受控詞彙 governed 編輯(wo_hold_reason 增/改,不刪)----


async def test_add_and_update_hold_reason_governed(session) -> None:
    """admin 面等待原因維護:add(形狀/去重/label 驗證 + RBAC)、update(label + is_downtime,
    不新增不刪)。is_downtime 為 downtime 引擎語意,可改但不回溯(此測試只驗持久化)。"""
    await load_wo(WO_ROWS, session)  # 種 wo_hold_reason lookup(WAITING_PARTS 等 5 筆)
    svc = WorkOrderService(session)
    admin = await _seed_admin(session, "vocab-admin")

    # RBAC:非 admin 拒(domain 強制,非只藏頁)
    with pytest.raises(AuthorizationError):
        await svc.add_hold_reason("WAITING_TOOLING", "Waiting tooling",
                                  is_downtime=True, actor=ACTOR)

    # 新增:code 大寫正規化 + 存 is_downtime
    r = await svc.add_hold_reason("waiting_tooling", "Waiting tooling",
                                  is_downtime=True, actor=admin)
    assert r.code == "WAITING_TOOLING" and r.is_downtime is True
    assert "WAITING_TOOLING" in {x.code for x in await svc.list_hold_reasons()}

    # 重複 code 拒
    with pytest.raises(WorkOrderError, match="already exists"):
        await svc.add_hold_reason("WAITING_TOOLING", "dup", is_downtime=False, actor=admin)

    # 非法 code 形狀拒(含空白 / 太短)
    with pytest.raises(WorkOrderError, match="UPPER_SNAKE"):
        await svc.add_hold_reason("A B", "x", is_downtime=False, actor=admin)
    with pytest.raises(WorkOrderError, match="UPPER_SNAKE"):
        await svc.add_hold_reason("AB", "x", is_downtime=False, actor=admin)  # 僅 2 字元

    # 空 label 拒
    with pytest.raises(WorkOrderError, match="label cannot be empty"):
        await svc.add_hold_reason("VALID_CODE", "  ", is_downtime=False, actor=admin)

    # 更新既有:改 label + is_downtime(既有 WAITING_PARTS 載入為 downtime=True)
    u = await svc.update_hold_reason("WAITING_PARTS", label="Awaiting parts",
                                     is_downtime=False, actor=admin)
    assert u.label == "Awaiting parts" and u.is_downtime is False

    # 更新不存在 → 錯
    with pytest.raises(WorkOrderError, match="not found"):
        await svc.update_hold_reason("NOPE_XX", label="x", is_downtime=True, actor=admin)

    # RBAC:非 admin 不可更新
    with pytest.raises(AuthorizationError):
        await svc.update_hold_reason("WAITING_PARTS", label="y", is_downtime=True, actor=ACTOR)


# ---- 2026-07-05 批 W1(Jordan #1 #3 #9):日誌軟刪 / 生命週期便利方法 / 領料改量·取消 ----


async def test_delete_note_soft_and_gates(session) -> None:
    """#1:軟刪(留誰/何時)、list_notes 排除、冪等;權限=本人或 admin;終態限 admin。"""
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    n1 = await svc.add_note(no, entry_type="progress", body="第一筆", actor=ACTOR)
    n2 = await svc.add_note(no, entry_type="progress", body="第二筆", actor=ACTOR)

    # 非本人(非 admin)不可刪
    with pytest.raises(WorkOrderError, match="author"):
        await svc.delete_note(n1.id, Actor.human("someone-else"))
    # 歸屬守門
    with pytest.raises(WorkOrderError, match="belong"):
        await svc.delete_note(n1.id, ACTOR, work_order_no=999999)
    # 本人軟刪 → 時間線消失、列仍在(deleted_at/deleted_by 可稽核)
    owner = await svc.delete_note(n1.id, ACTOR, work_order_no=no)
    assert owner == no
    assert [n.id for n in await svc.list_notes(no)] == [n2.id]
    row = (
        await session.execute(
            text("SELECT deleted_at, deleted_by FROM work_order_note WHERE id = :i"),
            {"i": n1.id},
        )
    ).one()
    assert row.deleted_at is not None and row.deleted_by == ACTOR.value
    # 冪等:再刪 = no-op
    assert await svc.delete_note(n1.id, ACTOR) == no

    # 終態凍結:結單後本人不得刪,admin 可
    await svc.finish_work_order(no, ACTOR, action_taken="done", at=_ts(12))
    with pytest.raises(WorkOrderError, match="frozen"):
        await svc.delete_note(n2.id, ACTOR)
    admin = await _seed_admin(session, "del-admin")
    assert await svc.delete_note(n2.id, admin) == no
    assert await svc.list_notes(no) == []


async def test_set_hold_from_open_and_switch_reason(session) -> None:
    """#3a:等待 chip 可從 OPEN 一鍵轉等待(隱式 start)、ON_HOLD 換原因(resume+hold 原子);
    status_history 忠實記錄每步(契約形狀不變),hold note 連結最終轉移。"""
    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no

    # OPEN → 一鍵等料(中間補 IN_PROGRESS,兩步同時刻)
    wo = await svc.set_hold(no, "WAITING_PARTS", ACTOR, at=_ts(10), note_body="泵 7/10 到")
    assert wo.status == "ON_HOLD" and wo.hold_reason == "WAITING_PARTS"
    hist = await svc.get_status_history(no)
    assert [h.to_status for h in hist] == ["OPEN", "IN_PROGRESS", "ON_HOLD"]
    assert hist[1].changed_at == hist[2].changed_at  # 隱式 start 零長度,不影響 downtime
    notes = await svc.list_notes(no)
    assert len(notes) == 1 and notes[0].entry_type == "hold"
    assert notes[0].status_history_id == hist[2].id  # 說明連結最終 hold 轉移

    # ON_HOLD → 點另一個等待 chip = 換原因(resume+hold 兩轉移原子完成)
    wo = await svc.set_hold(no, "TEST_RUN", ACTOR, at=_ts(11))
    assert wo.status == "ON_HOLD" and wo.hold_reason == "TEST_RUN"
    hist = await svc.get_status_history(no)
    assert [h.to_status for h in hist] == [
        "OPEN", "IN_PROGRESS", "ON_HOLD", "IN_PROGRESS", "ON_HOLD",
    ]
    assert hist[4].hold_reason == "TEST_RUN"

    # IN_PROGRESS → 直接 hold(單步)
    await svc.resume_work(no, ACTOR, at=_ts(12))
    await svc.set_hold(no, "WAITING_VENDOR", ACTOR, at=_ts(13))
    hist = await svc.get_status_history(no)
    assert hist[-1].to_status == "ON_HOLD" and hist[-1].hold_reason == "WAITING_VENDOR"

    # 守門:無原因拒;終態拒
    with pytest.raises(WorkOrderError):
        await svc.set_hold(no, "", ACTOR, at=_ts(14))
    await svc.resume_work(no, ACTOR, at=_ts(14))
    await svc.finish_work_order(no, ACTOR, action_taken="ok", at=_ts(15))
    with pytest.raises(InvalidTransition):
        await svc.set_hold(no, "WAITING_PARTS", ACTOR, at=_ts(16))


async def test_resume_or_start_dispatch(session) -> None:
    """#3a:「處理中」chip —— OPEN→start、ON_HOLD→resume、IN_PROGRESS 冪等 no-op、終態拒。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no

    wo = await svc.resume_or_start(no, ACTOR, at=_ts(10))  # OPEN → start
    assert wo.status == "IN_PROGRESS"
    wo = await svc.resume_or_start(no, ACTOR, at=_ts(11))  # 已處理中 → no-op(不多記轉移)
    assert wo.status == "IN_PROGRESS"
    assert len(await svc.get_status_history(no)) == 2  # OPEN + IN_PROGRESS

    await svc.hold_work(no, "WAITING_PARTS", ACTOR, at=_ts(12))
    wo = await svc.resume_or_start(no, ACTOR, at=_ts(13))  # ON_HOLD → resume
    assert wo.status == "IN_PROGRESS"

    await svc.complete_work(no, ACTOR, at=_ts(14))
    await svc.close_work_order(no, ACTOR, at=_ts(15))
    with pytest.raises(InvalidTransition):
        await svc.resume_or_start(no, ACTOR, at=_ts(16))


async def test_finish_work_order_one_click(session) -> None:
    """#3b:單一「結單」= 同交易 COMPLETED→CLOSED 兩段(history 兩筆保留、downtime 精算);
    處置摘要選填(Jordan 2026-07-07:不強制總結);壞工時交易外先擋(狀態不動);
    OPEN 亦可直接結單(隱式 start)。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, ACTOR, at=_ts(10))

    # 守門:壞工時先驗(狀態不動、無新轉移);處置摘要不再是必填(空/None 不擋)
    with pytest.raises(WorkOrderError, match="labor_hours"):
        await svc.finish_work_order(no, ACTOR, action_taken="x", labor_hours="2,5", at=_ts(12))
    assert (await svc.get_work_order(no)).status == "IN_PROGRESS"

    wo = await svc.finish_work_order(
        no, ACTOR, action_taken="更換真空泵", labor_hours="1.5", at=_ts(12)
    )
    assert wo.status == "CLOSED"
    assert wo.action_taken == "更換真空泵" and wo.labor_hours == Decimal("1.5")
    hist = await svc.get_status_history(no)
    assert [h.to_status for h in hist] == ["OPEN", "IN_PROGRESS", "COMPLETED", "CLOSED"]
    assert hist[2].changed_at == hist[3].changed_at  # 同一時刻,COMPLETED 段零長度
    assert wo.downtime_minutes == 180  # 9:00→12:00 全生產時段
    assert wo.downtime_estimated is False

    # OPEN 直接結單 + 不填處置摘要(「開始」鍵已移除,downtime 從開單起算;action_taken 選填)
    wo2 = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(13)
    )
    wo2 = await svc.finish_work_order(wo2.work_order_no, ACTOR, at=_ts(14))
    assert wo2.status == "CLOSED" and wo2.downtime_minutes == 60
    assert wo2.action_taken is None  # 未填 → 保持 None,不擋結單
    hist2 = await svc.get_status_history(wo2.work_order_no)
    assert [h.to_status for h in hist2] == ["OPEN", "IN_PROGRESS", "COMPLETED", "CLOSED"]

    # 終態再結單 → 拒
    with pytest.raises(InvalidTransition):
        await svc.finish_work_order(no, ACTOR, action_taken="again", at=_ts(15))


async def test_update_part_issue_quantity(session) -> None:
    """#9:改量差額連動 —— 增=補 ISSUE 扣庫(不足拒)、減=RETURN 回庫;守門 <=0 / 終態 / 冪等。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    inv = InventoryService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.issue_part_to_work_order(
        work_order_no=no, item_code="ES001", quantity="3", actor=ACTOR, idempotency_key="i1"
    )  # on_hand 10 → 7
    part_id = (await svc.get_parts(no))[0].id  # 取純值:守門 rollback 會 expire ORM 物件

    # 增量 3→5:補 ISSUE -2 → on_hand 5
    ok = await svc.update_part_issue_quantity(
        work_order_no=no, part_id=part_id, new_quantity="5", actor=ACTOR, idempotency_key="u1"
    )
    assert ok is True
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("5.000")
    assert (await svc.get_parts(no))[0].quantity == Decimal("5.000")
    # 冪等:同 key 重送不再動
    assert await svc.update_part_issue_quantity(
        work_order_no=no, part_id=part_id, new_quantity="5", actor=ACTOR, idempotency_key="u1"
    ) is False

    # 減量 5→2:RETURN +3 → on_hand 8
    await svc.update_part_issue_quantity(
        work_order_no=no, part_id=part_id, new_quantity="2", actor=ACTOR, idempotency_key="u2"
    )
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("8.000")
    assert (await svc.get_parts(no))[0].quantity == Decimal("2.000")
    ret = (
        await session.execute(
            text("SELECT qty_delta FROM stock_transaction WHERE kind='RETURN'")
        )
    ).scalar()
    assert ret == Decimal("3.000")  # 補償帳留痕

    # 守門:<=0 拒(移除走 cancel)、增量超過庫存拒、數量未變 no-op
    with pytest.raises(WorkOrderError, match="positive"):
        await svc.update_part_issue_quantity(
            work_order_no=no, part_id=part_id, new_quantity="0", actor=ACTOR
        )
    with pytest.raises(WorkOrderError, match="insufficient"):
        await svc.update_part_issue_quantity(
            work_order_no=no, part_id=part_id, new_quantity="100", actor=ACTOR
        )
    assert await svc.update_part_issue_quantity(
        work_order_no=no, part_id=part_id, new_quantity="2", actor=ACTOR
    ) is False
    # 歸屬守門 + 終態拒
    with pytest.raises(WorkOrderError, match="not found"):
        await svc.update_part_issue_quantity(
            work_order_no=999999, part_id=part_id, new_quantity="1", actor=ACTOR
        )
    await svc.finish_work_order(no, ACTOR, action_taken="done", at=_ts(12))
    with pytest.raises(WorkOrderError, match="terminal"):
        await svc.update_part_issue_quantity(
            work_order_no=no, part_id=part_id, new_quantity="1", actor=ACTOR
        )


async def test_cancel_part_issue(session) -> None:
    """#9:取消領料 = RETURN 全數回庫 + 摘要列軟刪(get_parts 消失、ledger 留痕);冪等。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    inv = InventoryService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    await svc.issue_part_to_work_order(
        work_order_no=no, item_code="ES001", quantity="4", actor=ACTOR, idempotency_key="i2"
    )  # on_hand 10 → 6
    part = (await svc.get_parts(no))[0]

    ok = await svc.cancel_part_issue(
        work_order_no=no, part_id=part.id, actor=ACTOR, idempotency_key="c1"
    )
    assert ok is True
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("10.000")  # 全數回庫
    assert await svc.get_parts(no) == []  # 摘要列軟刪 → 清單消失
    row = (
        await session.execute(
            text("SELECT deleted_at, deleted_by FROM work_order_part WHERE id = :i"),
            {"i": part.id},
        )
    ).one()
    assert row.deleted_at is not None and row.deleted_by == ACTOR.value
    # ledger 兩筆皆留痕(ISSUE -4 + RETURN +4)
    kinds = (
        await session.execute(
            text("SELECT kind, qty_delta FROM stock_transaction ORDER BY txn_id")
        )
    ).all()
    assert [(k, d) for k, d in kinds] == [
        ("ISSUE", Decimal("-4.000")), ("RETURN", Decimal("4.000")),
    ]
    # 冪等:已取消 → False(不重複回庫)
    assert await svc.cancel_part_issue(work_order_no=no, part_id=part.id, actor=ACTOR) is False
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("10.000")
    # 已取消列不可再改量
    with pytest.raises(WorkOrderError, match="not found"):
        await svc.update_part_issue_quantity(
            work_order_no=no, part_id=part.id, new_quantity="2", actor=ACTOR
        )

    # 終態工單拒取消(歷史回填領料掛 CLOSED 單 → 同一守門保護,不會誤回灌 on_hand)
    wo2 = await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(10)
    )
    await svc.issue_part_to_work_order(
        work_order_no=wo2.work_order_no, item_code="ES002", quantity="1", actor=ACTOR
    )
    part2 = (await svc.get_parts(wo2.work_order_no))[0]
    await svc.finish_work_order(wo2.work_order_no, ACTOR, action_taken="done", at=_ts(12))
    with pytest.raises(WorkOrderError, match="terminal"):
        await svc.cancel_part_issue(
            work_order_no=wo2.work_order_no, part_id=part2.id, actor=ACTOR
        )


async def test_backfill_part_issue_not_cancellable_or_amendable(session) -> None:
    """安全(對抗式 verify F-2/F-3):歷史回填領料(BACKFILL_ACTOR、adjust_on_hand=False、**從未扣
    on_hand**)不可取消/改量 —— RETURN 反灌會憑空灌爆庫存。刻意掛 **OPEN(非終態)**工單,證明
    擋下靠的是顯式 source_actor 標記,不是終態守門(legacy 確有 OPEN 單掛回填 part)。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    inv = InventoryService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    async with svc.write(BACKFILL_ACTOR):  # loader 在單一 write() 交易內呼叫;本方法不自開交易
        outcome = await svc.backfill_part_issue(
            work_order_no=no, item_code="ES001", quantity="2",
            occurred_at=_ts(9), actor=BACKFILL_ACTOR, idempotency_key="bf1",
        )
    assert outcome is PartIssueOutcome.INSERTED
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("10.000")  # 回填不動 on_hand
    bf = (await svc.get_parts(no))[0]
    bf_id, bf_actor = bf.id, bf.source_actor  # 取純值:守門 raise → rollback 會 expire ORM 物件
    assert bf_actor == BACKFILL_ACTOR.value  # 標記確為 human:data-migration

    with pytest.raises(WorkOrderError, match="backfill"):
        await svc.cancel_part_issue(work_order_no=no, part_id=bf_id, actor=ACTOR)
    with pytest.raises(WorkOrderError, match="backfill"):
        await svc.update_part_issue_quantity(
            work_order_no=no, part_id=bf_id, new_quantity="3", actor=ACTOR, idempotency_key="u9"
        )
    assert (await inv.get_item("ES001")).quantity_on_hand == Decimal("10.000")  # 仍未灌爆

    # 回歸:同工單的 governed 領料(有扣 on_hand)照常可取消
    await svc.issue_part_to_work_order(
        work_order_no=no, item_code="ES002", quantity="1", actor=ACTOR, idempotency_key="g1"
    )  # ES002 5 → 4
    gov_id = next(p.id for p in await svc.get_parts(no) if p.source_actor == ACTOR.value)
    assert await svc.cancel_part_issue(
        work_order_no=no, part_id=gov_id, actor=ACTOR, idempotency_key="gc1"
    ) is True
    assert (await inv.get_item("ES002")).quantity_on_hand == Decimal("5.000")  # 回庫


# ---- 2026-07-07 WorkOrderDetail 契約 additive 擴充(分析平台 §19.6):notes[] / notes_truncated /
#      action_taken —— 直接驗真路由 get_work_order_detail(DTO 序列化 = golden fixture 路徑)。


async def test_detail_route_notes_action_taken_and_cap(session, monkeypatch) -> None:
    """/work-orders/{no}/detail 新增欄:notes 升冪(軟刪排除)、action_taken 浮現、cap 保留最新 N。"""
    from cmms.api.routes import work_orders as wo_routes

    await load_wo(WO_ROWS, session)
    await _seed_note_types(session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no

    # 亂序插入以證明排序依 occurred_at(非插入序);另加一筆待軟刪以證排除。
    await svc.add_note(no, entry_type="progress", body="C-14h", actor=ACTOR, occurred_at=_ts(14))
    await svc.add_note(no, entry_type="report", body="A-09h", actor=ACTOR, occurred_at=_ts(9, 30))
    await svc.add_note(no, entry_type="progress", body="B-11h", actor=ACTOR, occurred_at=_ts(11))
    dead = await svc.add_note(
        no, entry_type="note", body="DEAD-10h", actor=ACTOR, occurred_at=_ts(10)
    )
    await svc.delete_note(dead.id, ACTOR, work_order_no=no)
    await svc.finish_work_order(no, ACTOR, action_taken="更換真空泵,真空度回復正常。", at=_ts(15))

    detail = await wo_routes.get_work_order_detail(no, session)
    # action_taken 浮現(WorkOrderRead.model_validate 自動帶入)
    assert detail.action_taken == "更換真空泵,真空度回復正常。"
    # notes 升冪、軟刪(DEAD-10h)排除
    assert [n.body for n in detail.notes] == ["A-09h", "B-11h", "C-14h"]
    assert detail.notes[0].source_actor == ACTOR.value
    assert detail.notes_truncated is False

    # cap:保留**最新** N 筆(升冪 tail)、notes_truncated=true
    monkeypatch.setattr(wo_routes, "_DETAIL_NOTES_CAP", 2)
    capped = await wo_routes.get_work_order_detail(no, session)
    assert [n.body for n in capped.notes] == ["B-11h", "C-14h"]
    assert capped.notes_truncated is True


# ---- D6 confirmed_reason 回流(migration 0027;efc 軸,人工確認真因)----


async def _seed_efc(session, *codes: tuple[str, bool]) -> None:
    """種 efc 故障碼(code, is_active);測試 setup,非 domain 路徑。"""
    for code, active in codes:
        session.add(EquipmentFailureCode(code=code, is_active=active))
    await session.flush()


async def test_finish_with_confirmed_reason_persists(session) -> None:
    """結單順帶確認真因(efc 有效碼)→ 落 confirmed_reason_code + 契約可讀。"""
    await load_wo(WO_ROWS, session)
    await _seed_efc(session, ("efcPickupVacuumFault", True))
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    wo = await svc.finish_work_order(
        no, ACTOR, action_taken="換泵", confirmed_reason_code="efcPickupVacuumFault", at=_ts(11)
    )
    assert wo.status == "CLOSED" and wo.confirmed_reason_code == "efcPickupVacuumFault"


async def test_finish_unknown_efc_rejected_before_transition(session) -> None:
    """未知 efc 碼 → WorkOrderError,且結單**未半套用**(交易外先驗,WO 仍 OPEN)。"""
    await load_wo(WO_ROWS, session)
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    with pytest.raises(WorkOrderError):
        await svc.finish_work_order(no, ACTOR, confirmed_reason_code="efcNope", at=_ts(11))
    reloaded = await svc.get_work_order(no)
    assert reloaded.status == "OPEN" and reloaded.confirmed_reason_code is None


async def test_finish_inactive_efc_rejected(session) -> None:
    """退役 efc 碼(is_active=False)不得選為新真因 → WorkOrderError。"""
    await load_wo(WO_ROWS, session)
    await _seed_efc(session, ("efcRetired", False))
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    with pytest.raises(WorkOrderError):
        await svc.finish_work_order(
            wo.work_order_no, ACTOR, confirmed_reason_code="efcRetired", at=_ts(11)
        )


async def test_confirmed_reason_rejected_on_non_reactive(session) -> None:
    """真因僅 REACTIVE:PM 工單帶碼結單 → 拒(且未套用)。"""
    await load_wo(WO_ROWS, session)
    await _seed_efc(session, ("efcX", True))
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="PM", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    with pytest.raises(WorkOrderError):
        await svc.finish_work_order(no, ACTOR, confirmed_reason_code="efcX", at=_ts(11))
    assert (await svc.get_work_order(no)).status == "OPEN"


async def test_set_confirmed_reason_gates_clear_and_idempotent(session) -> None:
    """set_confirmed_reason:非終態任何登入者可設 → 清除 → 冪等 → CLOSED 後 engineer 拒/admin OK。"""
    await load_wo(WO_ROWS, session)
    await _seed_efc(session, ("efcA", True), ("efcB", True))
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    no = wo.work_order_no
    # 非終態:engineer 補填
    wo = await svc.set_confirmed_reason(no, code="efcA", actor=ACTOR)
    assert wo.confirmed_reason_code == "efcA"
    # 冪等 no-op(值未變)
    wo = await svc.set_confirmed_reason(no, code="efcA", actor=ACTOR)
    assert wo.confirmed_reason_code == "efcA"
    # 清除(code=None)
    wo = await svc.set_confirmed_reason(no, code=None, actor=ACTOR)
    assert wo.confirmed_reason_code is None
    # 結單後:engineer 更正被凍結,admin 可更正
    await svc.finish_work_order(no, ACTOR, at=_ts(11))
    with pytest.raises(WorkOrderError):
        await svc.set_confirmed_reason(no, code="efcB", actor=ACTOR)
    admin = await _seed_admin(session, "reason-admin")
    wo = await svc.set_confirmed_reason(no, code="efcB", actor=admin)
    assert wo.confirmed_reason_code == "efcB"


async def test_confirmed_reason_options_asset_history_first(session) -> None:
    """下拉候選:本設備用過的 active 碼優先、其餘 active 字母序在後;退役碼不建議。"""
    await load_wo(WO_ROWS, session)
    await _seed_efc(session, ("efcZ", True), ("efcA", True), ("efcOld", False))
    svc = WorkOrderService(session)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=ACTOR, at=_ts(9))
    await svc.finish_work_order(
        wo.work_order_no, ACTOR, confirmed_reason_code="efcZ", at=_ts(11)
    )
    opts = await svc.list_confirmed_reason_options("EID-001")
    codes = [o.code for o in opts]
    assert codes[0] == "efcZ"  # 本設備用過 → 排頭
    assert opts[0].used is True  # 排頭者標「曾用」
    assert "efcA" in codes and "efcOld" not in codes  # 其餘 active 入列;退役排除
    assert opts[codes.index("efcA")].used is False  # 未用過者不標「曾用」
