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
        "evidence_ref": "onbox-evidence:v1:onbox:WET01:EID-001:1719000000:abc",
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
        "line_no": "10K",
        "available": "Yes",
    },
]
# 一筆種子工單(讓 work_type=REACTIVE 進 lookup + 種狀態機 lookup)
SEED_WO = [
    {
        "wo": "24172",
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


async def test_onbox_open_and_idempotent(session) -> None:
    svc = WorkOrderService(session)
    wo = await svc.open_reactive_work_order_onbox(
        jws_token=_sign(_onbox_claims()), key_resolver=_resolver, at=_ts(10)
    )
    assert wo.status == "OPEN" and wo.work_type == "REACTIVE"
    assert wo.source_actor == ONBOX_PRINCIPAL  # agent:analytics-onbox(機台歸屬,非個人)
    assert wo.origin_station == "WET01"
    assert wo.idempotency_key == "onbox:WET01:EID-001:1719000000:abc"
    assert wo.evidence_ref.startswith("onbox-evidence:v1:")
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
            _onbox_claims(idempotency_key=key, evidence_ref="onbox-evidence:v1:" + key)
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
    session.add(EquipmentFailureCode(code="efcHeadCrash", is_active=True))
    await session.flush()
    # 先把一張 REACTIVE 工單推到 COMPLETED(close 提案只做 COMPLETED→CLOSED)
    wo = await svc.open_work_order(asset_id="EID-001", work_type="REACTIVE", actor=admin, at=_ts(9))
    no = wo.work_order_no
    await svc.start_work(no, admin, at=_ts(10))
    await svc.complete_work(no, admin, at=_ts(11))
    p = await svc.propose(
        operation="close_work_order",
        params={"work_order_no": no, "confirmed_reason_code": "efcHeadCrash"},
        proposed_by=Actor.agent("analytics"),
        at=_ts(11, 30),
    )
    assert p.dry_run_diff["confirmed_reason_code"] == "efcHeadCrash"
    closed = await svc.confirm(
        pending_token=p.pending_token, confirmer=admin, at=_ts(12)
    )
    assert closed.status == "CLOSED" and closed.confirmed_reason_code == "efcHeadCrash"
