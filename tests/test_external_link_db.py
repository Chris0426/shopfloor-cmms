"""work_order_external_link DB 測試(ADR-020 決策 3;testcontainers)。無 Docker 自動 skip。

驗:record_external_link 冪等 + dual attribution、allowlist-shape 守門(system/MRQ-key/link_type)、
backfill_legacy_mrq_links(external_ref MRQ-xxxx → referenced,冪等)。需全 model(work_order FK 鏈)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService  # noqa: E402

HERMES = Actor.agent("hermes")

ASSET_ROWS = [
    {"compid": "EID-002", "comp_desc": "Rig", "assettype": "Production", "department": "EQ",
     "line_no": "10K", "available": "Yes"},
]
WO_ROWS = [
    {
        "wo": "30167", "compid": "EID-002", "comp_desc": "x", "assetsubtp": "",
        "brief_desc": "fix", "diag": "", "comments": "MRQ-1234 MRQ-5678",  # legacy 外部單
        "date_wo": "05/21/26", "sch_date": "", "wo_type": "REACTIVE", "workstatus": "H",
        "miscreated": "F", "assignto": "CMA (Tester)", "edittime": "15:00:00",
        "editdate": "05/21/26", "edituser": "T", "time": "10:00:00", "time_cmpl": "15:00:00",
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
            await load_wo(WO_ROWS, s)
            yield s
        await engine.dispose()


async def test_record_link_idempotent_dual_attribution(session) -> None:
    svc = WorkOrderService(session)
    l1 = await svc.record_external_link(
        work_order_no=30167, external_key="MRQ-9999", link_type="forwarded",
        actor=HERMES, on_behalf_of="human:jlee", title="吸嘴堵塞",
    )
    l2 = await svc.record_external_link(
        work_order_no=30167, external_key="MRQ-9999", link_type="forwarded",
        actor=HERMES, on_behalf_of="human:jlee",
    )
    assert l1.id == l2.id  # 冪等
    # dual attribution:誰轉發(agent)+ 代表誰(human)
    assert l1.source_actor == "agent:hermes" and l1.created_by == "human:jlee"
    assert len(await svc.list_external_links(30167)) == 1


async def test_shape_guard(session) -> None:
    svc = WorkOrderService(session)
    with pytest.raises(WorkOrderError):  # key 非 MRQ-<n>
        await svc.record_external_link(
            work_order_no=30167, external_key="ABC-1", link_type="forwarded", actor=HERMES
        )
    with pytest.raises(WorkOrderError):  # system 非 jira
        await svc.record_external_link(
            work_order_no=30167, external_key="MRQ-1", link_type="forwarded",
            actor=HERMES, system="github",
        )
    with pytest.raises(WorkOrderError):  # link_type 非法
        await svc.record_external_link(
            work_order_no=30167, external_key="MRQ-1", link_type="bogus", actor=HERMES
        )


async def test_scoped_token_delegated_link(session) -> None:
    """驗證式委派(ADR-020 決策 5):session → mint scoped token → resolve → 據以 record link。"""
    from cmms.domain.identity.service import IdentityService

    ident = IdentityService(session)
    await ident.create_user(
        user_id="jlee", username="jlee", display_name="C", password="password8",
        org="plant", actor=Actor.human("cli"),
    )
    _uid, stok = await ident.authenticate("jlee", "password8")
    token = await ident.mint_scoped_token(session_token=stok, agent="agent:hermes", scope="wo:link")
    resolved = await ident.resolve_scoped_token(token)
    assert resolved == ("jlee", "wo:link")
    link = await WorkOrderService(session).record_external_link(
        work_order_no=30167, external_key="MRQ-77", link_type="forwarded",
        actor=HERMES, on_behalf_of=f"human:{resolved[0]}",
    )
    # token 內身分 → created_by(驗證式,而非純斷言)
    assert link.created_by == "human:jlee" and link.source_actor == "agent:hermes"


async def test_list_proposals(session) -> None:
    """ADR-025 Lane 1 讀取:PENDING 清單;reject 後移出。"""
    svc = WorkOrderService(session)
    p1 = await svc.propose(
        operation="open_work_order",
        params={"asset_id": "EID-002", "work_type": "REACTIVE"},
        proposed_by=HERMES,
    )
    p2 = await svc.propose(
        operation="close_work_order", params={"work_order_no": 30167}, proposed_by=HERMES
    )
    pending = await svc.list_proposals()
    assert {p.pending_token for p in pending} == {p1.pending_token, p2.pending_token}
    await svc.reject(pending_token=p1.pending_token, by=Actor.human("adm"))
    assert {p.pending_token for p in await svc.list_proposals()} == {p2.pending_token}


async def test_backfill_legacy_mrq(session) -> None:
    svc = WorkOrderService(session)
    n = await svc.backfill_legacy_mrq_links(Actor.human("migration"))
    assert n == 2  # external_ref="MRQ-1234 MRQ-5678" → 2 referenced links
    assert await svc.backfill_legacy_mrq_links(Actor.human("migration")) == 0  # 冪等
    links = await svc.list_external_links(30167)
    assert {link.external_key for link in links} == {"MRQ-1234", "MRQ-5678"}
    assert all(link.link_type == "referenced" for link in links)
