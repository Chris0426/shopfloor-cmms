"""feedback DB 測試(續-16;testcontainers)。無 Docker 自動 skip。

覆蓋:
- create 快樂路徑(落庫、audit 欄)。
- 空留言 / 純空白拒;超長(>2000)拒。
- list_open 排序(舊→新;已處理不現)。
- mark_resolved:admin-only(非 admin → AuthorizationError)/ 冪等(已處理不重設)。
- list_recent_resolved(新→舊 + limit)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402

# 匯入全部 model 模組,讓 Base.metadata 收錄(FK 目標表齊全才能 create_all)。
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.assistant import models as _assistant_models  # noqa: E402, F401
from cmms.domain.attachment import models as _attachment_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.failure_vocab import models as _failure_vocab_models  # noqa: E402, F401
from cmms.domain.feedback import models as _feedback_models  # noqa: E402, F401
from cmms.domain.feedback.service import FeedbackError, FeedbackService  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.service import (  # noqa: E402
    AuthorizationError,
    IdentityService,
)
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.notify import models as _notify_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.procurement import models as _proc_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.telegram_bridge import models as _tg_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401

A_ADMIN = Actor.human("admin")
A_ALICE = Actor.human("alice")


@pytest.fixture
async def ctx():
    """PG 容器 + 種子帳號(admin / admin2 = admin、alice = engineer,皆 active)。"""
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            ident = IdentityService(s)
            await ident.create_user(
                user_id="admin", username="admin", display_name="Admin",
                password="password8", org="plant", role="admin", actor=Actor.human("cli"),
            )
            await ident.create_user(
                user_id="admin2", username="admin2", display_name="Admin 2",
                password="password8", org="plant", role="admin", actor=Actor.human("cli"),
            )
            await ident.create_user(
                user_id="alice", username="alice", display_name="Alice",
                password="password8", org="plant", role="engineer", actor=Actor.human("cli"),
            )
            yield s
        await engine.dispose()


# ---- create ----

async def test_create_happy(ctx) -> None:
    fb = await FeedbackService(ctx).create("alice", "  想要一份掃碼 SOP  ", A_ALICE)
    assert fb.id is not None
    assert fb.message == "想要一份掃碼 SOP"  # strip
    assert fb.user_id == "alice"
    assert fb.resolved_at is None
    assert fb.source_actor == A_ALICE.value


async def test_create_empty_rejected(ctx) -> None:
    svc = FeedbackService(ctx)
    with pytest.raises(FeedbackError, match="required"):
        await svc.create("alice", "   ", A_ALICE)
    with pytest.raises(FeedbackError, match="required"):
        await svc.create("alice", "", A_ALICE)


async def test_create_too_long_rejected(ctx) -> None:
    with pytest.raises(FeedbackError, match="too long"):
        await FeedbackService(ctx).create("alice", "x" * 2001, A_ALICE)


# ---- list_open 排序 ----

async def test_list_open_order_oldest_first(ctx) -> None:
    svc = FeedbackService(ctx)
    f1 = await svc.create("alice", "first", A_ALICE)
    f2 = await svc.create("alice", "second", A_ALICE)
    f3 = await svc.create("alice", "third", A_ALICE)
    ids = [f.id for f in await svc.list_open()]
    assert ids == [f1.id, f2.id, f3.id]  # 舊→新
    # 標其一已處理 → 不現於 open
    await svc.mark_resolved(f2.id, A_ADMIN)
    ids2 = [f.id for f in await svc.list_open()]
    assert ids2 == [f1.id, f3.id]


# ---- mark_resolved ----

async def test_mark_resolved_admin_only(ctx) -> None:
    svc = FeedbackService(ctx)
    fb = await svc.create("alice", "please", A_ALICE)
    with pytest.raises(AuthorizationError):
        await svc.mark_resolved(fb.id, A_ALICE)  # engineer 非 admin
    # DB 未變(仍開放)
    assert [f.id for f in await svc.list_open()] == [fb.id]


async def test_mark_resolved_and_idempotent(ctx) -> None:
    svc = FeedbackService(ctx)
    fb = await svc.create("alice", "please", A_ALICE)
    r1 = await svc.mark_resolved(fb.id, A_ADMIN)
    assert r1.resolved_at is not None
    assert r1.resolved_by == A_ADMIN.value
    first_at = r1.resolved_at
    # 第二次(換另一位真 admin)冪等:不覆蓋原處理者 / 時間
    r2 = await svc.mark_resolved(fb.id, Actor.human("admin2"))
    assert r2.resolved_at == first_at
    assert r2.resolved_by == A_ADMIN.value  # 仍是首位處理者


async def test_mark_resolved_not_found(ctx) -> None:
    with pytest.raises(FeedbackError, match="not found"):
        await FeedbackService(ctx).mark_resolved(999999, A_ADMIN)


# ---- list_recent_resolved ----

async def test_list_recent_resolved(ctx) -> None:
    svc = FeedbackService(ctx)
    fs = [await svc.create("alice", f"msg{i}", A_ALICE) for i in range(3)]
    # 依序處理 → resolved_at 遞增;list 應新→舊
    for f in fs:
        await svc.mark_resolved(f.id, A_ADMIN)
    recent = await svc.list_recent_resolved()
    ids = [f.id for f in recent]
    assert ids == [fs[2].id, fs[1].id, fs[0].id]
    # limit 生效
    assert len(await svc.list_recent_resolved(limit=2)) == 2
