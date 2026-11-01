"""#4b-2 gated write DB 整合測試(ADR-016 兩階段 + ADR-017 on-box)。無 Docker 自動 skip。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

pytest.importorskip("testcontainers.postgres")
pytest.importorskip("jwt")
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
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
from cmms.domain.pm_schedule import models as _p  # noqa: E402, F401
from cmms.domain.work_order import models as _w  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.onbox import ONBOX_PRINCIPAL, OnboxVerificationError  # noqa: E402
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))
_PRIV = Ed25519PrivateKey.generate()
_PUB = _PRIV.public_key()


def _resolver(kid: str):
    return _PUB if kid == "k1" else None


def _sign(claims: dict, *, kid: str = "k1", key: Ed25519PrivateKey | None = None) -> str:
    return jwt.encode(claims, key or _PRIV, algorithm="EdDSA", headers={"kid": kid})


def _onbox_claims(**over) -> dict:
    now = datetime.now(UTC)
    c = {
        "iss": "analytics",
        "sub": ONBOX_PRINCIPAL,
        "op": "open_reactive_work_order",
        "asset_id": "EID-001",
        "idempotency_key": "onbox:WET01:EID-001:1719000000:abc",
        "origin_station": "WET01",
        "evidence_ref": "board-evidence:v1:onbox:WET01:EID-001:1719000000:abc",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "jti": "j1",
    }
    c.update(over)
    return c


ASSET_ROWS = [
    {
        "compid": "EID-001",
        "comp_desc": "Rig",
        "assettype": "Production",
        "department": "EQ",
        "line_no": "Zone B",
        "available": "Yes",
    },
]
# 一筆種子工單(讓 work_type=REACTIVE 進 lookup + 種狀態機 lookup)
SEED_WO = [
    {
        "wo": "90012",
        "compid": "EID-001",
        "comp_desc": "y",
        "assetsubtp": "",
        "brief_desc": "PM",
        "diag": "",
        "comments": "",
        "date_wo": "05/23/26",
        "sch_date": "",
        "wo_type": "REACTIVE",
        "workstatus": "O",
        "miscreated": "F",
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
            await load_assets(ASSET_ROWS, s)
            await load_wo(SEED_WO, s)  # seed lookups + REACTIVE work_type
            yield s
        await engine.dispose()


def _ts(h: int, m: int = 0) -> datetime:
    return datetime(2026, 5, 20, h, m, tzinfo=TAIPEI)


async def _seed_admin(session, user_id: str = "alice", role: str = "admin") -> Actor:
    """種一個 active 帳號(所有 confirm 一律要求現行 active admin;F-1a 收緊)。"""
    await IdentityService(session).create_user(
        user_id=user_id, username=user_id, display_name=user_id, password="pw-123456",
        org="Shopfloor", actor=Actor.human("bootstrap"), role=role,
    )
    return Actor.human(user_id)


# ---- ADR-016 兩階段 ----


async def test_propose_confirm_open(session) -> None:
    svc = WorkOrderService(session)
    await _seed_admin(session)  # alice = active admin(confirm 一律驗 admin)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE", "brief_description": "leak"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(10),
    )
    assert p.status == "PENDING" and p.dry_run_diff["result_status"] == "OPEN"
    wo = await svc.confirm(
        pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 5)
    )
    assert wo.status == "OPEN" and wo.source_actor == "human:alice"
    # 已 CONFIRMED,再 confirm 失敗
    with pytest.raises(WorkOrderError, match="not pending"):
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 6)
        )


async def test_confirm_requires_human(session) -> None:
    svc = WorkOrderService(session)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(10),
    )
    with pytest.raises(WorkOrderError, match="human"):  # 拒匿名/agent confirm
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.agent("analytics"), at=_ts(10, 1)
        )


async def test_propose_idempotent(session) -> None:
    svc = WorkOrderService(session)
    kw = dict(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"),
        idempotency_key="prop-1",
    )
    p1 = await svc.propose(**kw, at=_ts(10))
    p2 = await svc.propose(**kw, at=_ts(10, 1))
    assert p1.pending_token == p2.pending_token


async def test_expired_proposal_rejected(session) -> None:
    svc = WorkOrderService(session)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"),
        ttl_seconds=60,
        at=_ts(10),
    )
    with pytest.raises(WorkOrderError, match="expired"):
        await svc.confirm(pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(11))


async def test_high_risk_not_proposable(session) -> None:
    svc = WorkOrderService(session)
    with pytest.raises(WorkOrderError, match="not proposable"):
        await svc.propose(
            operation="void_work_order", params={}, proposed_by=Actor.agent("analytics"), at=_ts(10)
        )


async def test_reject_then_cannot_confirm(session) -> None:
    svc = WorkOrderService(session)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(10),
    )
    await svc.reject(pending_token=p.pending_token, by=Actor.human("alice"), at=_ts(10, 1))
    with pytest.raises(WorkOrderError, match="not pending"):
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 2)
        )


async def test_confirm_requires_active_admin(session) -> None:
    """F-1a 安全收緊:非 admin 的已驗證 human 不得 confirm(即使是 PROPOSABLE_OPS 開/結工單)。
    `/mcp` 上公網後,持有效 token 者不得自我確認自己的提案。"""
    svc = WorkOrderService(session)
    await _seed_admin(session, user_id="eng1", role="engineer")  # 非 admin
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(10),
    )
    with pytest.raises(AuthorizationError, match="admin"):
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("eng1"), at=_ts(10, 1)
        )
    # 無帳號的 human 亦拒(自報 id ≠ 審核權)
    with pytest.raises(AuthorizationError, match="admin"):
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("ghost"), at=_ts(10, 2)
        )


async def test_reject_requires_human(session) -> None:
    """F-1a:reject 拒匿名/agent(agent 不得自行丟棄待人審的提案)。"""
    svc = WorkOrderService(session)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(10),
    )
    with pytest.raises(WorkOrderError, match="human"):
        await svc.reject(pending_token=p.pending_token, by=Actor.agent("analytics"), at=_ts(10, 1))


async def test_propose_close_rejects_non_completed(session) -> None:
    """結案提案在建立階段就擋非 COMPLETED 的單:拋 InvalidTransition、proposal 表 0 列。"""
    from sqlalchemy import func, select

    from cmms.domain.work_order.models import PendingProposal
    from cmms.domain.work_order.service import InvalidTransition

    svc = WorkOrderService(session)
    bob = Actor.human("bob")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=bob, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, bob, at=_ts(9, 30))
    kw = dict(
        operation="close_work_order", params={"work_order_no": no},
        proposed_by=Actor.agent("analytics"),
    )
    with pytest.raises(InvalidTransition, match="IN_PROGRESS -> CLOSED not allowed.*COMPLETED"):
        await svc.propose(**kw, at=_ts(10))
    with pytest.raises(WorkOrderError, match="not found"):
        await svc.propose(
            operation="close_work_order", params={"work_order_no": 99999999},
            proposed_by=Actor.agent("analytics"), at=_ts(10),
        )
    n = await session.scalar(select(func.count()).select_from(PendingProposal))
    assert n == 0
    await svc.complete_work(no, bob, at=_ts(10, 30))
    p = await svc.propose(**kw, at=_ts(11))
    assert p.status == "PENDING" and p.dry_run_diff["from_status"] == "COMPLETED"


async def test_confirm_close_via_two_phase(session) -> None:
    svc = WorkOrderService(session)
    await _seed_admin(session)  # alice = active admin
    bob = Actor.human("bob")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=bob, at=_ts(9))
    await svc.start_work(wo.work_order_no, bob, at=_ts(9, 30))
    await svc.complete_work(wo.work_order_no, bob, at=_ts(10))
    p = await svc.propose(
        operation="close_work_order",
        params={"work_order_no": wo.work_order_no},
        proposed_by=Actor.agent("analytics"),
        at=_ts(10, 1),
    )
    assert p.dry_run_diff["from_status"] == "COMPLETED"
    closed = await svc.confirm(
        pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 30)
    )
    assert (
        closed.status == "CLOSED" and closed.confirmed_by is None
    )  # WO 稽核;proposal 記 confirmer
    # proposal 稽核面
    from cmms.domain.work_order.models import PendingProposal

    pp = await session.get(PendingProposal, p.pending_token)
    assert (
        pp.status == "CONFIRMED"
        and pp.confirmed_by == "human:alice"
        and pp.proposed_by == "agent:analytics"
    )


# ---- ADR-017 on-box ----


async def test_confirm_close_stale_after_reopen(session) -> None:
    """提案後工單被重開(COMPLETED→IN_PROGRESS,另一個 session 改的):confirm 不執行、
    拋 ProposalStale、提案落 EXPIRED(confirmed_by=嘗試確認的 admin);之後不可再 confirm。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from cmms.domain.work_order.models import PendingProposal, WorkOrder
    from cmms.domain.work_order.service import InvalidTransition, ProposalStale

    svc = WorkOrderService(session)
    await _seed_admin(session)
    bob = Actor.human("bob")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=bob, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, bob, at=_ts(9, 30))
    await svc.complete_work(no, bob, at=_ts(10))
    p = await svc.propose(
        operation="close_work_order", params={"work_order_no": no},
        proposed_by=Actor.agent("analytics"), at=_ts(10, 1),
    )
    # 另一個 session 重開工單:本 session identity map 仍是 COMPLETED → 驗 confirm 重讀現況
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await WorkOrderService(other).resume_work(no, bob, at=_ts(10, 10))
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 30)
        )
    assert isinstance(ei.value, InvalidTransition)  # 既有只接 WorkOrderError 的呼叫端不受影響
    assert ei.value.work_order_no == no and ei.value.current_status == "IN_PROGRESS"
    # 以新 session 讀:EXPIRED 標記已落庫(活過 raise,不隨交易回滾)
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        pp = await fresh.get(PendingProposal, p.pending_token)
        assert pp.status == "EXPIRED" and pp.confirmed_by == "human:alice"
        assert pp.resolved_at is not None
        # 原因落庫(提案人頁據此顯示),原 dry-run diff 保留
        assert pp.dry_run_diff["stale"] == {
            "reason": "wo_status", "subject": None,
            "work_order_no": no, "current_status": "IN_PROGRESS",
        }
        assert pp.dry_run_diff["action"] == "close_work_order"
        assert (await fresh.get(WorkOrder, no)).status == "IN_PROGRESS"
    cur = await session.get(WorkOrder, no)
    assert cur.status == "IN_PROGRESS"  # 未被結案
    with pytest.raises(WorkOrderError, match="not pending"):
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 31)
        )


async def test_confirm_void_stale_after_complete(session) -> None:
    """請求作廢提案後工單已完工(COMPLETED 不可作廢):confirm 同樣不執行、提案 EXPIRED。"""
    from cmms.domain.work_order.models import PendingProposal, WorkOrder
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    await _seed_admin(session)
    bob = Actor.human("bob")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=bob, at=_ts(9))
    no = wo.work_order_no
    p = await svc.propose(
        operation="void_work_order", params={"work_order_no": no, "reason": "dup"},
        proposed_by=bob, at=_ts(9, 5),
    )
    await svc.start_work(no, bob, at=_ts(9, 30))
    await svc.complete_work(no, bob, at=_ts(10))
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(
            pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 30)
        )
    assert ei.value.current_status == "COMPLETED"
    pp = await session.get(PendingProposal, p.pending_token)
    assert pp.status == "EXPIRED" and pp.confirmed_by == "human:alice"
    assert (await session.get(WorkOrder, no)).status == "COMPLETED"


async def test_confirm_close_still_valid_unaffected(session) -> None:
    """重驗不誤擋:提案後工單被重開又再完工(回到 COMPLETED)→ 仍可 confirm 結案。"""
    svc = WorkOrderService(session)
    await _seed_admin(session)
    bob = Actor.human("bob")
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=bob, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, bob, at=_ts(9, 30))
    await svc.complete_work(no, bob, at=_ts(10))
    p = await svc.propose(
        operation="close_work_order", params={"work_order_no": no},
        proposed_by=Actor.agent("analytics"), at=_ts(10, 1),
    )
    await svc.resume_work(no, bob, at=_ts(10, 10))
    await svc.complete_work(no, bob, at=_ts(10, 20))
    closed = await svc.confirm(
        pending_token=p.pending_token, confirmer=Actor.human("alice"), at=_ts(10, 30)
    )
    assert closed.status == "CLOSED"



async def _wo_count(session) -> int:
    from sqlalchemy import func, select

    from cmms.domain.work_order.models import WorkOrder

    return await session.scalar(select(func.count()).select_from(WorkOrder))


async def _assert_expired_stale(session, token: str, reason: str, subject: str) -> None:
    """以新 session 讀:提案 EXPIRED、confirmed_by = 嘗試確認的 admin、原因已落 dry_run_diff。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from cmms.domain.work_order.models import PendingProposal

    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        pp = await fresh.get(PendingProposal, token)
        assert pp.status == "EXPIRED" and pp.confirmed_by == "human:alice"
        assert pp.resolved_at is not None
        assert pp.dry_run_diff["stale"]["reason"] == reason
        assert pp.dry_run_diff["stale"]["subject"] == subject


async def test_confirm_open_stale_after_asset_retired(session) -> None:
    """REACTIVE 開單提案後設備被退役(is_active=false,另一個 session):confirm 不開單、
    拋 ProposalStale(asset_retired, subject=asset_id)、提案 EXPIRED;之後不可再 confirm。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from cmms.domain.asset.service import AssetService
    from cmms.domain.work_order.service import InvalidTransition, ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-001", "work_type": "REACTIVE", "brief_description": "leak"},
        proposed_by=Actor.agent("analytics"), at=_ts(10),
    )
    before = await _wo_count(session)
    # 另一個 session 退役設備:本 session identity map 仍是 is_active=True → 驗 confirm 重讀
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await AssetService(other).set_asset_active("EID-001", False, alice)
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert isinstance(ei.value, InvalidTransition)  # MCP 只接 WorkOrderError,不受影響
    assert ei.value.reason == "asset_retired" and ei.value.subject == "EID-001"
    assert ei.value.work_order_no is None
    await _assert_expired_stale(session, p.pending_token, "asset_retired", "EID-001")
    assert await _wo_count(session) == before  # 未開單
    with pytest.raises(WorkOrderError, match="not pending"):
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 6))


async def test_confirm_open_stale_asset_missing(session) -> None:
    """開單提案的設備不在主檔(propose 不驗設備;confirm 原本撞 domain 拒 / FK)→ asset_missing。"""
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    p = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-404", "work_type": "REACTIVE"},
        proposed_by=Actor.agent("analytics"), at=_ts(10),
    )
    before = await _wo_count(session)
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert ei.value.reason == "asset_missing" and ei.value.subject == "EID-404"
    await _assert_expired_stale(session, p.pending_token, "asset_missing", "EID-404")
    assert await _wo_count(session) == before


async def _seed_item(session, alice: Actor, **kw):
    from cmms.domain.inventory.service import InventoryService

    return await InventoryService(session).create_item("ES900", actor=alice, name="Seal", **kw)


def _item_params(**over) -> dict:
    params = {
        "item_code": "ES900", "name": "Seal v2", "description": "", "vendor_part_no": "",
        "bin_location": "", "reorder_point": "", "reorder_quantity": "",
        "lead_time_weeks": "", "unit_cost": "", "supplier": "", "supplier_org_id": "",
        "weblink": "", "comment": "", "is_stocked": True, "is_obsolete": False,
    }
    params.update(over)
    return params


async def test_confirm_update_item_stale_bin_deactivated(session) -> None:
    """品項修改提案把儲位改成 B-07,提案後 B-07 被停用:confirm 不改主檔、bin_unavailable。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from cmms.domain.inventory.models import InventoryItem
    from cmms.domain.inventory.service import InventoryService
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await InventoryService(session).add_storage_bin("B-07", actor=alice)
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(bin_location="B-07"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await InventoryService(other).set_storage_bin_active("B-07", is_active=False, actor=alice)
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert ei.value.reason == "bin_unavailable" and ei.value.subject == "ES900"
    await _assert_expired_stale(session, p.pending_token, "bin_unavailable", "ES900")
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        item = await fresh.get(InventoryItem, "ES900")
        assert item.name == "Seal" and item.bin_location is None  # 未執行


async def test_confirm_update_item_stale_supplier_renamed(session) -> None:
    """提案的 supplier 對得上 organization 名,提案後該 org 更名(無其他品項用舊名):
    confirm 不改主檔、supplier_unknown。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from cmms.domain.contacts.service import ContactsService
    from cmms.domain.inventory.models import InventoryItem
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    org = await ContactsService(session).create_organization(actor=alice, name="Acme Seals")
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(supplier="Acme Seals"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await ContactsService(other).update_organization(
            org.org_id, actor=alice, name="Acme Sealing Co",
            org_type=None, website=None, address=None, phone=None,
        )
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert ei.value.reason == "supplier_unknown" and ei.value.subject == "ES900"
    await _assert_expired_stale(session, p.pending_token, "supplier_unknown", "ES900")
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        item = await fresh.get(InventoryItem, "ES900")
        assert item.name == "Seal" and item.supplier is None


async def test_confirm_update_item_stale_item_missing(session) -> None:
    """提案後品項列不見了(domain 無刪除路徑;以 SQL 模擬庫外移除)→ item_missing,不 500。"""
    from sqlalchemy import text

    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    await session.execute(text("DELETE FROM inventory_item WHERE item_code = 'ES900'"))
    await session.commit()
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert ei.value.reason == "item_missing" and ei.value.subject == "ES900"
    await _assert_expired_stale(session, p.pending_token, "item_missing", "ES900")


async def test_confirm_update_item_bin_still_valid_unaffected(session) -> None:
    """重驗不誤擋:儲位仍 active、supplier 仍對得上 → confirm 照常執行。"""
    from cmms.domain.contacts.service import ContactsService
    from cmms.domain.inventory.service import InventoryService

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await InventoryService(session).add_storage_bin("B-07", actor=alice)
    await ContactsService(session).create_organization(actor=alice, name="Acme Seals")
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item",
        params=_item_params(bin_location="b-07", supplier="Acme Seals"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    item = await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert item.name == "Seal v2" and item.bin_location == "B-07"
    assert item.supplier == "Acme Seals"


def _hook_before_execute(monkeypatch, hook) -> None:
    """在 confirm 的「前提檢查之後、執行之前」(write() 交易內、鎖已取得)插入 hook。"""
    orig = WorkOrderService._execute_proposed

    async def hooked(self, *a, **k):
        await hook()
        return await orig(self, *a, **k)

    monkeypatch.setattr(WorkOrderService, "_execute_proposed", hooked)


async def _blocked_by_lock(session, write) -> bool:
    """另一個 session 以 lock_timeout 嘗試寫入:撞到 confirm 持有的鎖 → True(寫入已回滾)。"""
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await other.execute(text("SET LOCAL lock_timeout = '300ms'"))
        try:
            await write(other)
        except DBAPIError as e:
            return "lock" in str(e).lower()
    return False


async def test_confirm_update_item_bin_locked_until_commit(session, monkeypatch) -> None:
    """檢查後、執行前有人要停用提案中的儲位:被 confirm 的鎖擋住(等不到),提案照常執行;
    confirm 交易結束後停用才落得下去 —— 檢查與執行看到同一份儲位狀態。"""
    from cmms.domain.inventory.service import InventoryService
    from cmms.domain.work_order.models import PendingProposal

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await InventoryService(session).add_storage_bin("B-07", actor=alice)
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(bin_location="b-07"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    blocked: list[bool] = []

    async def deactivate(other):
        await InventoryService(other).set_storage_bin_active(
            "B-07", is_active=False, actor=alice
        )

    async def hook():
        blocked.append(await _blocked_by_lock(session, deactivate))

    _hook_before_execute(monkeypatch, hook)
    item = await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert blocked == [True]
    assert item.bin_location == "B-07"
    assert (await session.get(PendingProposal, p.pending_token)).status == "CONFIRMED"
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await deactivate(other)  # 鎖已釋放 → 停用照常


async def test_confirm_update_item_org_locked_until_commit(session, monkeypatch) -> None:
    """檢查後、執行前有人要把 supplier 對應的 organization 更名:被鎖擋住,提案照常執行。"""
    from cmms.domain.contacts.service import ContactsService

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    org = await ContactsService(session).create_organization(actor=alice, name="Acme Seals")
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(supplier="acme seals"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    blocked: list[bool] = []

    async def rename(other):
        await ContactsService(other).update_organization(
            org.org_id, actor=alice, name="Acme Sealing Co",
            org_type=None, website=None, address=None, phone=None,
        )

    async def hook():
        blocked.append(await _blocked_by_lock(session, rename))

    _hook_before_execute(monkeypatch, hook)
    item = await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert blocked == [True]
    assert item.supplier == "acme seals"
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
        await rename(other)  # 鎖已釋放 → 更名照常


async def test_confirm_update_item_supplier_witness_changed_mid_confirm(
    session, monkeypatch
) -> None:
    """supplier 只靠「另一個品項的 supplier 現值」對上(無對應 org,此依據不上鎖);檢查後、執行前
    該品項的 supplier 被清掉(另一個 session 提交):執行端拒絕 → 重驗後轉 ProposalStale
    (supplier_unknown)、提案 EXPIRED、主檔未改,不冒原始錯誤。"""
    from sqlalchemy import text

    from cmms.domain.inventory.models import InventoryItem
    from cmms.domain.inventory.service import InventoryService
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await _seed_item(session, alice)
    await InventoryService(session).create_item("ES901", actor=alice, name="Gasket")
    # legacy 未連 org 的供應商名(create_item 不收未知 supplier → 以 SQL 模擬既有資料)
    await session.execute(
        text("UPDATE inventory_item SET supplier = 'Old Vendor' WHERE item_code = 'ES901'")
    )
    await session.commit()
    p = await svc.propose(
        operation="update_item", params=_item_params(supplier="Old Vendor"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )

    async def hook():
        async with async_sessionmaker(session.bind, expire_on_commit=False)() as other:
            await InventoryService(other).update_item(
                "ES901", actor=alice, name="Gasket", description=None, vendor_part_no=None,
                bin_location=None, reorder_point=None, reorder_quantity=None,
                lead_time_weeks=None, unit_cost=None, supplier=None, weblink=None,
                comment=None, is_stocked=True, is_obsolete=False,
            )

    _hook_before_execute(monkeypatch, hook)
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert ei.value.reason == "supplier_unknown" and ei.value.subject == "ES900"
    await _assert_expired_stale(session, p.pending_token, "supplier_unknown", "ES900")
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        item = await fresh.get(InventoryItem, "ES900")
        assert item.name == "Seal" and item.supplier is None


async def test_confirm_update_item_bin_fails_at_execute_becomes_stale(
    session, monkeypatch
) -> None:
    """儲位驗證在檢查時通過、執行時失敗(模擬檢查與寫入之間儲位失效):轉 ProposalStale
    (bin_unavailable)+ EXPIRED,不留 PENDING、不冒原始錯誤。"""
    from cmms.domain.inventory.models import InventoryItem
    from cmms.domain.inventory.service import InventoryError, InventoryService
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await InventoryService(session).add_storage_bin("B-07", actor=alice)
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(bin_location="B-07"),
        proposed_by=Actor.human("eng1"), at=_ts(10),
    )
    orig = InventoryService._validate_bin_location
    calls: list[int] = []

    async def flaky(self, value, *, current=None):
        calls.append(1)
        if len(calls) == 1:  # confirm 的前提檢查
            return await orig(self, value, current=current)
        raise InventoryError(f"bin_location {value!r} is not a registered storage bin")

    monkeypatch.setattr(InventoryService, "_validate_bin_location", flaky)
    with pytest.raises(ProposalStale) as ei:
        await svc.confirm(pending_token=p.pending_token, confirmer=alice, at=_ts(10, 5))
    assert ei.value.reason == "bin_unavailable"
    assert len(calls) == 3  # 檢查 → 執行(失敗)→ 失敗後重驗
    await _assert_expired_stale(session, p.pending_token, "bin_unavailable", "ES900")
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        item = await fresh.get(InventoryItem, "ES900")
        assert item.name == "Seal" and item.bin_location is None


async def test_confirm_update_item_execute_error_not_stale_keeps_pending(
    session, monkeypatch
) -> None:
    """執行失敗但重驗前提仍成立(非前提失效)→ 維持原錯誤、提案留 PENDING(不誤標 EXPIRED)。"""
    from cmms.domain.inventory.service import InventoryError, InventoryService
    from cmms.domain.work_order.models import PendingProposal
    from cmms.domain.work_order.service import ProposalStale

    svc = WorkOrderService(session)
    alice = await _seed_admin(session)
    await _seed_item(session, alice)
    p = await svc.propose(
        operation="update_item", params=_item_params(), proposed_by=Actor.human("eng1"),
        at=_ts(10),
    )

    async def boom(self, *a, **k):
        raise InventoryError("simulated write failure")

    token = p.pending_token  # 失敗回滾後 ORM 物件已過期,先取值
    monkeypatch.setattr(InventoryService, "_update_item_impl", boom)
    with pytest.raises(WorkOrderError, match="simulated write failure") as ei:
        await svc.confirm(pending_token=token, confirmer=alice, at=_ts(10, 5))
    assert not isinstance(ei.value, ProposalStale)
    async with async_sessionmaker(session.bind, expire_on_commit=False)() as fresh:
        assert (await fresh.get(PendingProposal, token)).status == "PENDING"


async def test_onbox_open_and_idempotent(session) -> None:
    svc = WorkOrderService(session)
    wo = await svc.open_reactive_work_order_onbox(
        jws_token=_sign(_onbox_claims()), key_resolver=_resolver, at=_ts(10)
    )
    assert wo.status == "OPEN" and wo.work_type == "REACTIVE"
    assert wo.source_actor == ONBOX_PRINCIPAL  # agent:analytics-onbox(機台歸屬,非個人)
    assert wo.origin_station == "WET01"
    assert wo.idempotency_key == "onbox:WET01:EID-001:1719000000:abc"
    assert wo.evidence_ref.startswith("board-evidence:v1:")
    # idempotent:同 idempotency_key(重簽)→ 回既有 WO,不開第二張
    wo2 = await svc.open_reactive_work_order_onbox(
        jws_token=_sign(_onbox_claims(jti="j2")), key_resolver=_resolver, at=_ts(10, 5)
    )
    assert wo2.work_order_no == wo.work_order_no


async def test_onbox_unknown_eid_rejected(session) -> None:
    svc = WorkOrderService(session)
    tok = _sign(_onbox_claims(asset_id="EID-999", idempotency_key="onbox:WET01:EID-999:1:x"))
    with pytest.raises(OnboxVerificationError, match="unknown EID"):
        await svc.open_reactive_work_order_onbox(jws_token=tok, key_resolver=_resolver, at=_ts(10))


async def test_onbox_bad_signature_rejected(session) -> None:
    svc = WorkOrderService(session)
    tok = _sign(_onbox_claims(), key=Ed25519PrivateKey.generate())  # 非 JWKS 內的 key
    with pytest.raises(OnboxVerificationError):
        await svc.open_reactive_work_order_onbox(jws_token=tok, key_resolver=_resolver, at=_ts(10))


async def test_onbox_cancel_soft(session) -> None:
    svc = WorkOrderService(session)
    key = "onbox:WET01:EID-001:777:zzz"
    wo = await svc.open_reactive_work_order_onbox(
        jws_token=_sign(
            _onbox_claims(idempotency_key=key, evidence_ref="board-evidence:v1:" + key)
        ),
        key_resolver=_resolver,
        at=_ts(10),
    )
    cancelled = await svc.cancel_reactive_report_onbox(
        jws_token=_sign(_onbox_claims(op="cancel_reactive_report", idempotency_key=key)),
        key_resolver=_resolver,
        at=_ts(10, 5),
    )
    assert cancelled.work_order_no == wo.work_order_no and cancelled.status == "CANCELLED"


async def test_propose_confirm_close_with_confirmed_reason(session) -> None:
    """propose_close 帶 confirmed_reason_code(D6)→ confirm 執行時落 efc 真因(dry-run 亦見)。"""
    from cmms.domain.failure_vocab.models import EquipmentFailureCode

    svc = WorkOrderService(session)
    admin = await _seed_admin(session)
    session.add(EquipmentFailureCode(code="eqfHeadCrash", is_active=True))
    await session.flush()
    # 先把一張 REACTIVE 工單推到 COMPLETED(close 提案只做 COMPLETED→CLOSED)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=admin, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, admin, at=_ts(10))
    await svc.complete_work(no, admin, at=_ts(11))
    p = await svc.propose(
        operation="close_work_order",
        params={"work_order_no": no, "confirmed_reason_code": "eqfHeadCrash"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(11, 30),
    )
    assert p.dry_run_diff["confirmed_reason_code"] == "eqfHeadCrash"
    closed = await svc.confirm(
        pending_token=p.pending_token, confirmer=admin, at=_ts(12)
    )
    assert closed.status == "CLOSED" and closed.confirmed_reason_code == "eqfHeadCrash"
