"""Task 切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip;CI 有 postgres service / Docker 時執行。
驗證:載入器 idempotent、讀取、is_active server_default、稽核欄、描述搜尋。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.db import Base  # noqa: E402
from cmms.domain.task import models as _models  # noqa: E402, F401
from cmms.domain.task.loader import load  # noqa: E402
from cmms.domain.task.service import TaskService  # noqa: E402

ROWS = [
    {"task_no": "CAL1DM", "task_desc": "Calibrator Gen1 Daily Maintenance", "": ""},
    {"task_no": "CAL1DMDS", "task_desc": "Calibrator Gen1 Daily Maintenance Day Shift", "": ""},
    {"task_no": "CAL1DR", "task_desc": "Calibrator Gen1 Drive Belt Replacement", "": ""},
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
            yield s
        await engine.dispose()


async def test_load_is_idempotent(session) -> None:
    r1 = await load(ROWS, session)
    assert r1.tasks == 3

    # 再跑一次不應重複(on_conflict_do_update)
    r2 = await load(ROWS, session)
    assert r2.tasks == 3
    svc = TaskService(session)
    assert len(await svc.list_tasks(limit=1000)) == 3


async def test_reads_defaults_and_audit(session) -> None:
    await load(ROWS, session)
    svc = TaskService(session)

    t = await svc.get_task("CAL1DM")
    assert t is not None
    assert t.description == "Calibrator Gen1 Daily Maintenance"
    assert t.is_active is True  # server_default(載入不帶值,閒置標記延到 SA 切片)
    assert t.source_actor == "human:migration"  # 稽核(ADR-005)

    assert await svc.get_task("NOPE") is None


async def test_search_by_description(session) -> None:
    await load(ROWS, session)
    svc = TaskService(session)

    daily = await svc.list_tasks(search="Daily Maintenance", limit=1000)
    assert {t.task_no for t in daily} == {"CAL1DM", "CAL1DMDS"}

    degas = await svc.list_tasks(search="drive belt", limit=1000)  # ilike 大小寫不敏感
    assert {t.task_no for t in degas} == {"CAL1DR"}


async def test_filter_by_is_active(session) -> None:
    await load(ROWS, session)
    svc = TaskService(session)

    # 載入後全為 active;手動停用一筆驗證過濾(模擬未來 SA 切片的閒置標記)
    one = await svc.get_task("CAL1DR")
    assert one is not None
    one.is_active = False
    await session.commit()

    active = await svc.list_tasks(is_active=True, limit=1000)
    assert {t.task_no for t in active} == {"CAL1DM", "CAL1DMDS"}
    inactive = await svc.list_tasks(is_active=False, limit=1000)
    assert {t.task_no for t in inactive} == {"CAL1DR"}
