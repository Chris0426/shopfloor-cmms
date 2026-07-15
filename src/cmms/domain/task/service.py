"""TaskService — Task 切片的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:get_task / list_tasks(對外開放,ADR-004)。
寫入:upsert_task(載入器用,idempotent)。建立/修改 task 的 governed write 留後續切片。
所有寫入經 `DomainService.write()` 交易並填稽核欄(source_actor 等,ADR-005)。
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.base import DomainService
from cmms.domain.inventory.models import InventoryItem
from cmms.domain.task.models import Task, TaskPart, TaskStep
from cmms.domain.task.transform import TaskImport

# 新建任務代碼:沿 eMaint 慣例(大寫英數縮寫);限制字元使 URL/顯示安全。
_TASK_NO_RE = re.compile(r"^[A-Za-z0-9_-]{2,20}$")


@dataclass(frozen=True)
class TaskDeletionEvent:
    """稽核 feed 用的正規化軟刪事件(step / part 合流成一種形狀,route 不必辨型)。"""

    kind: str  # "step" | "part"
    task_no: str
    detail: str  # step 的描述 / part 的料號
    deleted_at: datetime
    deleted_by: str | None


class TaskError(Exception):
    """保養任務寫入錯誤(代碼不合法/重複、步驟/用料不存在等)。"""


class TaskService(DomainService):
    # ---- 讀取(ADR-004:讀取低風險,直接開放)----

    async def get_task(self, task_no: str) -> Task | None:
        return await self.session.get(Task, task_no)

    async def list_tasks(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[Task]:
        stmt = select(Task)
        if is_active is not None:
            stmt = stmt.where(Task.is_active == is_active)
        if search:
            # 以 description 或 task_no 關鍵字查任務(參數化,無注入風險)
            like = f"%{search}%"
            stmt = stmt.where(or_(Task.description.ilike(like), Task.task_no.ilike(like)))
        stmt = stmt.order_by(Task.task_no).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def get_task_steps(self, task_no: str) -> list[TaskStep]:
        """某任務的保養細項(有序:proc_seq → id;NULL 序號置後;不含軟刪)。讀取,開放(ADR-004)。"""
        stmt = (
            select(TaskStep)
            .where(TaskStep.task_no == task_no, TaskStep.deleted_at.is_(None))
            .order_by(TaskStep.proc_seq.nulls_last(), TaskStep.id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_parts_for_steps(self, step_ids: list[int]) -> dict[int, list[TaskPart]]:
        """一批步驟的用料,依 task_step_id 分組(一步 0..N 料;不含軟刪)。讀取,開放。"""
        if not step_ids:
            return {}
        stmt = (
            select(TaskPart)
            .where(TaskPart.task_step_id.in_(step_ids), TaskPart.deleted_at.is_(None))
            .order_by(TaskPart.id)
        )
        out: dict[int, list[TaskPart]] = {}
        for p in (await self.session.scalars(stmt)).all():
            out.setdefault(p.task_step_id, []).append(p)
        return out

    async def list_recent_deletions(
        self, *, limit: int = 50, actor_like: str | None = None
    ) -> list[TaskDeletionEvent]:
        """近期軟刪的 PM 範本細項(task_step)與用料(task_part)(0020 稽核 feed;讀取開放)。
        `actor_like` = deleted_by 子字串過濾。兩源合併後依 deleted_at 新到舊、截 limit。"""
        events: list[TaskDeletionEvent] = []
        step_stmt = select(TaskStep).where(TaskStep.deleted_at.is_not(None))
        if actor_like:
            step_stmt = step_stmt.where(TaskStep.deleted_by.ilike(f"%{actor_like}%"))
        step_stmt = step_stmt.order_by(TaskStep.deleted_at.desc()).limit(limit)
        for s in (await self.session.scalars(step_stmt)).all():
            events.append(
                TaskDeletionEvent("step", s.task_no, s.task_desc, s.deleted_at, s.deleted_by)
            )
        # task_part 無 task_no 欄 → join task_step 取所屬任務(供 UI 連回 /admin/pm/<task_no>)
        part_stmt = (
            select(TaskPart, TaskStep.task_no)
            .join(TaskStep, TaskPart.task_step_id == TaskStep.id)
            .where(TaskPart.deleted_at.is_not(None))
        )
        if actor_like:
            part_stmt = part_stmt.where(TaskPart.deleted_by.ilike(f"%{actor_like}%"))
        part_stmt = part_stmt.order_by(TaskPart.deleted_at.desc()).limit(limit)
        for part, task_no in (await self.session.execute(part_stmt)).all():
            events.append(
                TaskDeletionEvent("part", task_no, part.item_code, part.deleted_at, part.deleted_by)
            )
        events.sort(key=lambda e: e.deleted_at, reverse=True)
        return events[:limit]

    # ---- governed 寫入(admin PM 大項目管理;PM epic 2026-07-03,每個自帶 write() 交易)----

    async def create_task(self, *, task_no: str, description: str, actor: Actor) -> Task:
        """建保養大項目(範本)。task_no 為 owner 自訂的不可變代碼(沿 eMaint 縮寫慣例)。"""
        task_no = task_no.strip().upper()
        description = description.strip()
        if not _TASK_NO_RE.match(task_no):
            raise TaskError(f"invalid task code (A-Z/0-9/_-, 2-20 chars): {task_no}")
        if not description:
            raise TaskError("description is required")
        if await self.session.get(Task, task_no) is not None:
            raise TaskError(f"task {task_no} already exists")
        task = Task(
            task_no=task_no,
            description=description,
            source_actor=actor.value,
            created_by=actor.value,
        )
        async with self.write(actor):
            self.session.add(task)
        return task

    async def update_task_description(
        self, task_no: str, *, description: str, actor: Actor
    ) -> Task:
        task = await self.session.get(Task, task_no)
        if task is None:
            raise TaskError(f"task {task_no} not found")
        description = description.strip()
        if not description:
            raise TaskError("description is required")
        async with self.write(actor):
            task.description = description
            task.updated_by = actor.value
            task.source_actor = actor.value
        return task

    async def set_task_active(self, task_no: str, active: bool, actor: Actor) -> Task:
        """啟用/停用任務範本(T3 淘汰旗標的人工面;冪等)。"""
        task = await self.session.get(Task, task_no)
        if task is None:
            raise TaskError(f"task {task_no} not found")
        if task.is_active == active:
            return task
        async with self.write(actor):
            task.is_active = active
            task.updated_by = actor.value
            task.source_actor = actor.value
        return task

    async def add_task_step(self, task_no: str, *, task_desc: str, actor: Actor) -> TaskStep:
        """加一個保養細項步驟(排在最後:proc_seq = max+10,沿 eMaint 10/20/30 慣例)。"""
        if await self.session.get(Task, task_no) is None:
            raise TaskError(f"task {task_no} not found")
        task_desc = task_desc.strip()
        if not task_desc:
            raise TaskError("step description is required")
        max_seq = await self.session.scalar(
            select(func.max(TaskStep.proc_seq)).where(TaskStep.task_no == task_no)
        )
        step = TaskStep(
            task_no=task_no,
            proc_seq=(max_seq or 0) + 10,
            task_desc=task_desc,
            source_actor=actor.value,
            created_by=actor.value,
        )
        async with self.write(actor):
            self.session.add(step)
            await self.session.flush()
            step_id = step.id
        return await self.session.get(TaskStep, step_id)

    async def _get_live_step(self, step_id: int, task_no: str | None) -> TaskStep:
        """取一個未軟刪的步驟;`task_no` 提供時強制路徑歸屬(review f14cf8d:防
        stale 分頁/跨任務 URL 誤改別的任務的步驟 —— 歸屬守門在 domain,非只靠 UI)。"""
        step = await self.session.get(TaskStep, step_id)
        if step is None or step.deleted_at is not None:
            raise TaskError(f"task_step {step_id} not found")
        if task_no is not None and step.task_no != task_no:
            raise TaskError(f"task_step {step_id} does not belong to task {task_no}")
        return step

    async def update_task_step(
        self, step_id: int, *, task_desc: str, actor: Actor, task_no: str | None = None
    ) -> TaskStep:
        step = await self._get_live_step(step_id, task_no)
        task_desc = task_desc.strip()
        if not task_desc:
            raise TaskError("step description is required")
        async with self.write(actor):
            step.task_desc = task_desc
            step.updated_by = actor.value
            step.source_actor = actor.value
        return step

    async def delete_task_step(
        self, step_id: int, actor: Actor, *, task_no: str | None = None
    ) -> str:
        """移除一個步驟(連同其用料列)。回所屬 task_no(供 UI 轉返)。

        軟刪(0020,護欄 #4):範本細項也是歷史 PM 對帳依據,留 `deleted_at`/`deleted_by`
        可查「誰、何時移除」;讀取面(get_task_steps / get_parts_for_steps)過濾。
        """
        step = await self._get_live_step(step_id, task_no)
        owner_task_no = step.task_no
        now = self._now()
        async with self.write(actor):
            step.deleted_at = now
            step.deleted_by = actor.value
            step.updated_by = actor.value
            step.source_actor = actor.value
            await self.session.execute(
                update(TaskPart)
                .where(TaskPart.task_step_id == step_id, TaskPart.deleted_at.is_(None))
                .values(deleted_at=now, deleted_by=actor.value, updated_by=actor.value)
            )
        return owner_task_no

    async def add_task_part(
        self,
        step_id: int,
        *,
        item_code: str,
        replace_qty: Decimal | str | None,
        actor: Actor,
        task_no: str | None = None,
    ) -> TaskPart:
        """步驟掛一個用料(item 必在庫存主檔,承 ADR-018 不鑄 phantom;同 (step,item) 冪等更新量;
        先前軟刪過的同組合 → 復活既有列,unique 約束含軟刪列)。"""
        await self._get_live_step(step_id, task_no)
        item_code = item_code.strip().upper()
        if await self.session.get(InventoryItem, item_code) is None:
            raise TaskError(f"inventory_item {item_code} not found")
        qty = Decimal(str(replace_qty)) if replace_qty not in (None, "") else None
        existing = await self.session.scalar(
            select(TaskPart).where(
                TaskPart.task_step_id == step_id, TaskPart.item_code == item_code
            )
        )
        async with self.write(actor):
            if existing is not None:
                existing.replace_qty = qty
                existing.deleted_at = None  # 軟刪復活(同組合再掛)
                existing.deleted_by = None
                existing.updated_by = actor.value
                existing.source_actor = actor.value
                return existing
            part = TaskPart(
                task_step_id=step_id,
                item_code=item_code,
                replace_qty=qty,
                source_actor=actor.value,
                created_by=actor.value,
            )
            self.session.add(part)
        return part

    async def remove_task_part(
        self, step_id: int, item_code: str, actor: Actor, *, task_no: str | None = None
    ) -> None:
        """移除步驟的一個用料(軟刪;冪等:不存在/已移除 = no-op)。"""
        await self._get_live_step(step_id, task_no)
        async with self.write(actor):
            await self.session.execute(
                update(TaskPart)
                .where(
                    TaskPart.task_step_id == step_id,
                    TaskPart.item_code == item_code.strip().upper(),
                    TaskPart.deleted_at.is_(None),
                )
                .values(deleted_at=self._now(), deleted_by=actor.value, updated_by=actor.value)
            )

    # ---- 寫入(經 self.write() 交易;此處只下語句,交易邊界在呼叫端)----

    async def upsert_task_step(
        self,
        *,
        task_no: str,
        proc_seq: int | None,
        task_desc: str,
        idempotency_key: str,
        actor: Actor,
    ) -> int | None:
        """載入器用:upsert 一個 task_step(依 idempotency_key 冪等)。回 step id;
        task 不存在(FK 落空)→ None,由 loader 記數 + skip(ADR-018 不鑄 phantom)。"""
        if await self.session.get(Task, task_no) is None:
            return None
        values = {
            "task_no": task_no,
            "proc_seq": proc_seq,
            "task_desc": task_desc,
            "idempotency_key": idempotency_key,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        stmt = (
            pg_insert(TaskStep)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["idempotency_key"],
                set_={"proc_seq": proc_seq, "task_desc": task_desc, "updated_by": actor.value},
            )
            .returning(TaskStep.id)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def upsert_task_part(
        self, *, task_step_id: int, item_code: str, replace_qty, actor: Actor
    ) -> bool:
        """載入器用:upsert 一個 task_part(依 (task_step_id,item_code) 冪等)。
        item 不在庫存主檔 → False(loader 記數 + skip 該 part,保留 step;ADR-018)。"""
        if await self.session.get(InventoryItem, item_code) is None:
            return False
        values = {
            "task_step_id": task_step_id,
            "item_code": item_code,
            "replace_qty": replace_qty,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        stmt = (
            pg_insert(TaskPart)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["task_step_id", "item_code"],
                set_={"replace_qty": replace_qty, "updated_by": actor.value},
            )
        )
        await self.session.execute(stmt)
        return True

    async def upsert_task(self, data: TaskImport, actor: Actor) -> None:
        """載入器用:依 task_no upsert。re-run 不重複(idempotent migration)。

        不帶 is_active → 由 server_default(true)決定;閒置標記延到 SA 切片(T3)。
        """
        values = {
            "task_no": data.task_no,
            "description": data.description,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("task_no", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(Task)
            .values(**values)
            .on_conflict_do_update(index_elements=["task_no"], set_=update_cols)
        )
        await self.session.execute(stmt)

    async def sync_active_status(self, referenced_task_nos: Collection[str], actor: Actor) -> int:
        """依外部引用同步 `is_active`:被引用→true,未被引用→false。回傳本次新標記為
        inactive 的筆數(T3)。

        供 ScheduledActivity 切片(#3)載入後對帳:未被任何 SA 引用的 task = 已淘汰
        (所屬設備棄用 / 任務經驗證不需執行,Jordan 2026-06-20)。只更新「狀態確實改變」
        的列,故 idempotent(再跑回傳 0)且不無謂污染稽核欄。`referenced` 為空時不動作
        (避免把全部 task 誤標 inactive)。
        """
        referenced = set(referenced_task_nos)
        if not referenced:
            return 0
        ref = list(referenced)

        deactivate = (
            update(Task)
            .where(Task.task_no.notin_(ref), Task.is_active.is_(True))
            .values(is_active=False, updated_by=actor.value, source_actor=actor.value)
        )
        result = await self.session.execute(deactivate)

        reactivate = (
            update(Task)
            .where(Task.task_no.in_(ref), Task.is_active.is_(False))
            .values(is_active=True, updated_by=actor.value, source_actor=actor.value)
        )
        await self.session.execute(reactivate)

        return result.rowcount or 0
