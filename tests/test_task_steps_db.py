"""task_step/task_part DB 整合測試(migration 0016;testcontainers-postgres)。無 Docker 自動 skip。

驗:載入步驟 + 用料、有序讀取、FK 落空(missing task → skip 整列 / missing item → 保留 step
skip part)、冪等重跑、一步多料(模型支援)。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402

# 註冊全切片 model(FK 鏈:inventory.stock_transaction→work_order→asset/pm_schedule 等;
# 比照 migrations/env.py,否則單獨跑 create_all 會 NoReferencedTableError)
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.loader import load as load_inv  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.task.service import TaskService  # noqa: E402
from cmms.domain.task.step_loader import load as load_steps  # noqa: E402
from cmms.domain.task.transform import TaskImport  # noqa: E402
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401

MIG = Actor.human("migration")


def _inv_row(item: str) -> dict[str, str | None]:
    return {
        "item": item, "asset_sub": "", "sf_desc": "", "vpartno": "", "descrip": "d",
        "location": "", "orderpt": "", "onhand": "", "cost": "", "lead_time": "",
        "obsol": "F", "stock": "T", "supplier": "", "weblink": "", "photo": "",
        "parnt_item": "", "child_item": "", "alt_item": "", "comment": "",
    }


def _step_row(task_no: str, seq: str, desc: str, item: str = "", qty: str = "") -> dict:
    return {"task_no": task_no, "proc_seq": seq, "task_desc": desc, "item": item,
            "replaceqty": qty}


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            svc = TaskService(s)
            async with svc.write(MIG):  # 前置:task 主檔(FK)
                await svc.upsert_task(TaskImport(task_no="TSK0007", description="Sorter PM"), MIG)
            await load_inv([_inv_row("EC000807"), _inv_row("EC000004")], s)  # 零件(FK)
            yield s
        await engine.dispose()


async def test_load_steps_and_parts(session) -> None:
    rows = [
        _step_row("TSK0007", "10", "Clean the pick and place head"),
        _step_row("TSK0007", "20", "Replace the shuttle wiper blade", "EC000807", "2"),
        _step_row("TSK0007", "30", "Clean the chips inside", "EC000004", ""),  # qty 缺
    ]
    r = await load_steps(rows, session)
    assert r.steps_loaded == 3 and r.parts_loaded == 2
    svc = TaskService(session)
    steps = await svc.get_task_steps("TSK0007")
    assert [s.proc_seq for s in steps] == [10, 20, 30]  # 有序
    parts = await svc.get_parts_for_steps([s.id for s in steps])
    s10, s20, s30 = steps
    assert s10.id not in parts  # 無料步驟
    assert parts[s20.id][0].item_code == "EC000807" and parts[s20.id][0].replace_qty == Decimal("2")
    assert parts[s30.id][0].replace_qty is None  # qty 缺 → 保持 NULL(不補)


async def test_missing_task_skips_row(session) -> None:
    r = await load_steps(
        [_step_row("NOSUCH", "10", "x"), _step_row("TSK0007", "10", "ok")], session
    )
    assert r.steps_loaded == 1 and r.missing_task_skipped == 1
    assert r.missing_task_samples == ["NOSUCH"]


async def test_missing_item_keeps_step_skips_part(session) -> None:
    r = await load_steps([_step_row("TSK0007", "10", "Replace X", "ZZZ999", "1")], session)
    assert r.steps_loaded == 1 and r.parts_loaded == 0 and r.missing_item_skipped == 1
    svc = TaskService(session)
    assert len(await svc.get_task_steps("TSK0007")) == 1  # 步驟保留(指令還在)


async def test_idempotent_rerun(session) -> None:
    rows = [_step_row("TSK0007", "10", "a"), _step_row("TSK0007", "20", "b", "EC000807", "1")]
    await load_steps(rows, session)
    await load_steps(rows, session)  # 重跑
    svc = TaskService(session)
    assert len(await svc.get_task_steps("TSK0007")) == 2  # 無重複步驟
    cnt = (await session.execute(text("SELECT count(*) FROM task_part"))).scalar()
    assert cnt == 1  # 無重複用料


# ---- 2026-07-03 批:admin PM 大項目 CRUD(governed;非 loader)----

ADMIN = Actor.human("jordan.lee")


async def test_task_crud_governed(session) -> None:
    from cmms.domain.task.service import TaskError

    svc = TaskService(session)
    # 建大項目(代碼正規化大寫;重複/非法代碼擋下)
    t = await svc.create_task(task_no="newpm1", description="新保養", actor=ADMIN)
    assert t.task_no == "NEWPM1" and t.created_by == ADMIN.value
    with pytest.raises(TaskError):
        await svc.create_task(task_no="NEWPM1", description="dup", actor=ADMIN)
    with pytest.raises(TaskError):
        await svc.create_task(task_no="bad code!", description="x", actor=ADMIN)
    # 改描述 + 停用/啟用(T3 人工面)
    t = await svc.update_task_description("NEWPM1", description="改名了", actor=ADMIN)
    assert t.description == "改名了"
    t = await svc.set_task_active("NEWPM1", False, ADMIN)
    assert t.is_active is False
    # 搜尋同時比對 task_no(自動完成用)
    assert [x.task_no for x in await svc.list_tasks(search="NEWPM")] == ["NEWPM1"]


async def test_step_and_part_crud_governed(session) -> None:
    from cmms.domain.task.service import TaskError

    svc = TaskService(session)
    # 加步驟:proc_seq 續 10/20/30 慣例
    s1 = await svc.add_task_step("TSK0007", task_desc="第一步", actor=ADMIN)
    s2 = await svc.add_task_step("TSK0007", task_desc="第二步", actor=ADMIN)
    assert (s1.proc_seq, s2.proc_seq) == (10, 20)
    # 改步驟
    s1 = await svc.update_task_step(s1.id, task_desc="第一步(修)", actor=ADMIN)
    assert s1.task_desc == "第一步(修)"
    # 掛用料(item 必在庫存主檔;同 (step,item) 冪等更新量)
    p = await svc.add_task_part(s2.id, item_code="ec000807", replace_qty="2", actor=ADMIN)
    assert p.item_code == "EC000807" and p.replace_qty == Decimal("2")
    p = await svc.add_task_part(s2.id, item_code="EC000807", replace_qty="3", actor=ADMIN)
    assert p.replace_qty == Decimal("3")  # 冪等更新
    with pytest.raises(TaskError):
        await svc.add_task_part(s2.id, item_code="ZZZ999", replace_qty=None, actor=ADMIN)
    # 移除用料(軟刪、冪等)→ 同組合再掛 = 復活同一列(unique 約束含軟刪列)
    await svc.remove_task_part(s2.id, "EC000807", ADMIN)
    await svc.remove_task_part(s2.id, "EC000807", ADMIN)  # no-op
    assert (await svc.get_parts_for_steps([s2.id])) == {}  # 讀取面已濾軟刪
    p2 = await svc.add_task_part(s2.id, item_code="EC000807", replace_qty="5", actor=ADMIN)
    assert p2.id == p.id and p2.deleted_at is None and p2.replace_qty == Decimal("5")
    # 歸屬守門:step 不屬該 task → 拒(防 stale 分頁跨任務誤改,review f14cf8d)
    with pytest.raises(TaskError):
        await svc.delete_task_step(s2.id, ADMIN, task_no="OTHERTASK")
    # 刪步驟 = 軟刪(連同用料;護欄 #4:留誰/何時,列保留可稽核)
    await svc.remove_task_part(s2.id, "EC000807", ADMIN)
    await svc.add_task_part(s2.id, item_code="EC000004", replace_qty=None, actor=ADMIN)
    task_no = await svc.delete_task_step(s2.id, ADMIN, task_no="TSK0007")
    assert task_no == "TSK0007"
    assert [s.id for s in await svc.get_task_steps("TSK0007")] == [s1.id]
    live = (await session.execute(
        text("SELECT count(*) FROM task_part WHERE deleted_at IS NULL")
    )).scalar()
    assert live == 0  # 用料隨步驟軟刪(讀取面消失)
    kept = (await session.execute(text(
        "SELECT count(*) FROM task_part WHERE deleted_at IS NOT NULL AND deleted_by IS NOT NULL"
    ))).scalar()
    assert kept == 2  # 列保留 + 記了誰刪(EC000807 + EC000004)
    who = (await session.execute(
        text("SELECT deleted_by FROM task_step WHERE id = :i"), {"i": s2.id}
    )).scalar()
    assert who == ADMIN.value  # 步驟本身也留刪除者
    # 已軟刪步驟不可再操作(視同不存在)
    with pytest.raises(TaskError):
        await svc.update_task_step(s2.id, task_desc="x", actor=ADMIN)


async def test_list_recent_deletions_feed(session) -> None:
    """ADR-019 /admin/audit:list_recent_deletions 合併 step+part 軟刪(join 取 task_no),
    依 deleted_at 逆時序,並支援 deleted_by 子字串過濾。"""
    svc = TaskService(session)
    s1 = await svc.add_task_step("TSK0007", task_desc="步驟一", actor=ADMIN)
    s2 = await svc.add_task_step("TSK0007", task_desc="步驟二", actor=ADMIN)
    await svc.add_task_part(s2.id, item_code="EC000807", replace_qty="1", actor=ADMIN)
    # 未刪任何東西 → 空
    assert await svc.list_recent_deletions() == []
    # 軟刪一個用料(part)+ 一個步驟(step,連同其用料)
    await svc.remove_task_part(s2.id, "EC000807", ADMIN)
    await svc.delete_task_step(s1.id, ADMIN, task_no="TSK0007")
    events = await svc.list_recent_deletions()
    kinds = {(e.kind, e.task_no, e.detail) for e in events}
    assert ("part", "TSK0007", "EC000807") in kinds       # part 經 join 取回 task_no
    assert ("step", "TSK0007", "步驟一") in kinds
    assert all(e.deleted_by == ADMIN.value for e in events)
    # 逆時序:deleted_at 由新到舊(非遞增)
    whens = [e.deleted_at for e in events]
    assert whens == sorted(whens, reverse=True)
    # actor 子字串過濾:對不上 → 空
    assert await svc.list_recent_deletions(actor_like="nobody") == []
    assert len(await svc.list_recent_deletions(actor_like="jordan")) >= 1


async def test_multi_part_per_step_supported(session) -> None:
    """一步多料:模型天生支援(Jordan 2026-07-02 決策)。手動掛第二個料驗證。"""
    await load_steps([_step_row("TSK0007", "10", "Replace both seals", "EC000807", "1")], session)
    svc = TaskService(session)
    step = (await svc.get_task_steps("TSK0007"))[0]
    async with svc.write(MIG):
        await svc.upsert_task_part(
            task_step_id=step.id, item_code="EC000004", replace_qty=Decimal("2"), actor=MIG
        )
    parts = (await svc.get_parts_for_steps([step.id]))[step.id]
    assert {p.item_code for p in parts} == {"EC000807", "EC000004"}  # 一步兩料
