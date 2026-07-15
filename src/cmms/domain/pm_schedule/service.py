"""PmScheduleService — ScheduledActivity 切片的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:get / list(可依 asset / task / vendor / suppress / 到期日過濾)。
寫入:upsert(載入器用,idempotent)。suppress/unsuppress、PM 產生等 governed write
延到後續切片(本切片讀取為主)。所有寫入經 `DomainService.write()` 交易並填稽核欄。
"""

from __future__ import annotations

import secrets
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.asset.models import Asset, AssetOwner
from cmms.domain.base import DomainService, clean_person_name
from cmms.domain.pm_schedule.models import FreqUnit, PmSchedule
from cmms.domain.pm_schedule.transform import PmScheduleImport
from cmms.domain.task.models import Task


class PmScheduleError(Exception):
    """PM 排程寫入錯誤(找不到、週期不合法、(asset, task) 重複等)。"""


class PmScheduleService(DomainService):
    # ---- 讀取(ADR-004:讀取低風險,直接開放)----

    async def _owner_names(self, asset_id: str) -> list[str]:
        """設備負責人清單(依 position;0031 asset_owner 交叉表)。"""
        rows = await self.session.execute(
            select(AssetOwner.person_name)
            .where(AssetOwner.asset_id == asset_id)
            .order_by(AssetOwner.position, AssetOwner.person_name)
        )
        return [r[0] for r in rows]

    @staticmethod
    def _attach_effective(pm: PmSchedule, names: list[str]) -> None:
        """透明附掛有效 assignee(0031):`effective_assignees` list + `effective_assignee` 「、」串
        (顯示層,非 mapped 欄)。"""
        pm.effective_assignees = names
        pm.effective_assignee = "、".join(names) if names else None

    async def get_pm_schedule(self, pm_id: str) -> PmSchedule | None:
        pm = await self.session.get(PmSchedule, pm_id)
        if pm is not None:
            # 0031:有效 assignee = per-PM 覆寫(單值)否則設備負責人(全部);供顯示 / 補開工單預填。
            override = clean_person_name(pm.assigned_person)
            names = [override] if override else await self._owner_names(pm.asset_id)
            self._attach_effective(pm, names)
        return pm

    async def list_freq_units(self) -> list[FreqUnit]:
        """週期單位受控詞彙(唯讀,ADR-004)。admin 詞彙頁純顯示。"""
        stmt = select(FreqUnit).order_by(FreqUnit.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_pm_schedules(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        asset_id: str | None = None,
        task_id: str | None = None,
        assigned_vendor: str | None = None,
        assigned_person: str | None = None,
        is_suppressed: bool | None = None,
        due_on_or_before: date | None = None,
        due_on_or_after: date | None = None,
    ) -> list[PmSchedule]:
        # 0031:有效 assignee = per-PM 覆寫(單值)否則設備負責人(全部,asset_owner)。
        # Mine 過濾:pm.assigned_person == X,或 pm.assigned_person 為空且該人在設備負責人清單。
        stmt = select(PmSchedule)
        if asset_id is not None:
            stmt = stmt.where(PmSchedule.asset_id == asset_id)
        if task_id is not None:
            stmt = stmt.where(PmSchedule.task_id == task_id)
        if assigned_vendor is not None:
            stmt = stmt.where(PmSchedule.assigned_vendor == assigned_vendor)
        if assigned_person is not None:
            owns = (
                select(AssetOwner.person_name)
                .where(
                    AssetOwner.asset_id == PmSchedule.asset_id,
                    AssetOwner.person_name == assigned_person,
                )
                .exists()
            )
            stmt = stmt.where(
                or_(
                    PmSchedule.assigned_person == assigned_person,
                    and_(PmSchedule.assigned_person.is_(None), owns),
                )
            )
        if is_suppressed is not None:
            stmt = stmt.where(PmSchedule.is_suppressed == is_suppressed)
        if due_on_or_before is not None:
            # 只取有排到期日者;空值(被 suppress / 未排)不算到期
            stmt = stmt.where(
                PmSchedule.next_due_date.is_not(None),
                PmSchedule.next_due_date <= due_on_or_before,
            )
        if due_on_or_after is not None:
            # 月曆視窗下界(Slice 3 PM 月曆);同樣只取有到期日者
            stmt = stmt.where(
                PmSchedule.next_due_date.is_not(None),
                PmSchedule.next_due_date >= due_on_or_after,
            )
        stmt = stmt.order_by(PmSchedule.pm_id).limit(limit).offset(offset)
        rows = list((await self.session.scalars(stmt)).all())
        # 有效 assignee:override PM 用其單值;其餘批次取設備負責人(免 N+1)
        need_owner = [pm.asset_id for pm in rows if clean_person_name(pm.assigned_person) is None]
        omap: dict[str, list[str]] = {}
        if need_owner:
            orows = await self.session.execute(
                select(AssetOwner.asset_id, AssetOwner.person_name)
                .where(AssetOwner.asset_id.in_(set(need_owner)))
                .order_by(AssetOwner.asset_id, AssetOwner.position, AssetOwner.person_name)
            )
            for aid, name in orows:
                omap.setdefault(aid, []).append(name)
        for pm in rows:
            override = clean_person_name(pm.assigned_person)
            self._attach_effective(pm, [override] if override else omap.get(pm.asset_id, []))
        return rows

    # ---- governed 寫入(admin 面;PM epic 2026-07-03,每個自帶 write() 交易)----

    async def _validate_frequency(
        self, frequency_interval: int, frequency_unit: str | None
    ) -> None:
        """週期不變量:interval>0 ⟺ 有 unit(排程器只跑週期性 PM,ADR-021);unit 必在 lookup。"""
        if frequency_interval < 0:
            raise PmScheduleError("frequency_interval must be >= 0")
        if frequency_interval > 0 and not frequency_unit:
            raise PmScheduleError("periodic PM needs a frequency_unit")
        if frequency_interval == 0 and frequency_unit:
            raise PmScheduleError("frequency_unit given but interval is 0")
        if frequency_unit and await self.session.get(FreqUnit, frequency_unit) is None:
            raise PmScheduleError(f"unknown frequency_unit: {frequency_unit}")

    async def create_pm_schedule(
        self,
        *,
        asset_id: str,
        task_id: str,
        actor: Actor,
        frequency_interval: int = 0,
        frequency_unit: str | None = None,
        next_due_date: date | None = None,
        assigned_person: str | None = None,
    ) -> PmSchedule:
        """建 PM 排程(任務 × 設備,governed)。pm_id 走 `PMW-` 前綴新命名空間
        (legacy eMaint 代理鍵形如 `_7ZX412Q88`,不混用、不碰撞)。(asset, task) 唯一。"""
        if await self.session.get(Asset, asset_id) is None:
            raise PmScheduleError(f"asset {asset_id} not found")
        if await self.session.get(Task, task_id) is None:
            raise PmScheduleError(f"task {task_id} not found")
        await self._validate_frequency(frequency_interval, frequency_unit)
        if frequency_interval > 0 and next_due_date is None:
            # review f14cf8d:週期排程沒 next_due 會靜默休眠(到期清單/排程器都濾
            # next_due IS NOT NULL),看起來設好了卻永遠不會發生 → 建立時強制。
            raise PmScheduleError("periodic PM needs a next_due_date (else it never fires)")
        dup = await self.session.scalar(
            select(PmSchedule.pm_id).where(
                PmSchedule.asset_id == asset_id, PmSchedule.task_id == task_id
            )
        )
        if dup is not None:
            raise PmScheduleError(f"pm_schedule for ({asset_id}, {task_id}) already exists: {dup}")
        pm = PmSchedule(
            pm_id=f"PMW-{secrets.token_hex(4).upper()}",
            asset_id=asset_id,
            task_id=task_id,
            frequency_interval=frequency_interval,
            frequency_unit=frequency_unit,
            next_due_date=next_due_date,
            assigned_person=clean_person_name(assigned_person),
            is_suppressed=False,
            source_actor=actor.value,
            created_by=actor.value,
        )
        async with self.write(actor):
            self.session.add(pm)
        return pm

    async def update_pm_schedule(
        self,
        pm_id: str,
        *,
        actor: Actor,
        frequency_interval: int,
        frequency_unit: str | None,
        next_due_date: date | None,
        assigned_person: str | None,
        task_id: str | None = None,
    ) -> PmSchedule:
        """改 PM 排程(週期 / 下次到期日 / 負責人;governed,可反覆修改)。
        `task_id` 提供時強制歸屬(review f14cf8d:防跨任務 URL 誤改別的排程)。"""
        pm = await self.session.get(PmSchedule, pm_id)
        if pm is None:
            raise PmScheduleError(f"pm_schedule {pm_id} not found")
        if task_id is not None and pm.task_id != task_id:
            raise PmScheduleError(f"pm_schedule {pm_id} does not belong to task {task_id}")
        await self._validate_frequency(frequency_interval, frequency_unit)
        if frequency_interval > 0 and next_due_date is None:
            raise PmScheduleError("periodic PM needs a next_due_date (else it never fires)")
        async with self.write(actor):
            pm.frequency_interval = frequency_interval
            pm.frequency_unit = frequency_unit
            pm.next_due_date = next_due_date
            pm.assigned_person = clean_person_name(assigned_person)
            pm.updated_by = actor.value
            pm.source_actor = actor.value
        return pm

    async def set_suppressed(
        self, pm_id: str, suppressed: bool, actor: Actor, *, task_id: str | None = None
    ) -> PmSchedule:
        """暫停/恢復 PM(is_suppressed;暫停 = 排程器與到期清單不出現,按需生成仍可)。冪等。"""
        pm = await self.session.get(PmSchedule, pm_id)
        if pm is None:
            raise PmScheduleError(f"pm_schedule {pm_id} not found")
        if task_id is not None and pm.task_id != task_id:
            raise PmScheduleError(f"pm_schedule {pm_id} does not belong to task {task_id}")
        if pm.is_suppressed == suppressed:
            return pm
        async with self.write(actor):
            pm.is_suppressed = suppressed
            pm.updated_by = actor.value
            pm.source_actor = actor.value
        return pm

    # ---- 寫入(經 self.write() 交易;此處只下語句,交易邊界在呼叫端)----

    async def upsert_lookup(self, model: type, code: str, label: str) -> None:
        """idempotent 種子 lookup(freq_unit / vendor)。"""
        stmt = (
            pg_insert(model)
            .values(code=code, label=label)
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await self.session.execute(stmt)

    async def upsert_pm_schedule(self, data: PmScheduleImport, actor: Actor) -> None:
        """載入器用:依 pm_id upsert。re-run 不重複(idempotent migration)。"""
        values = {
            "pm_id": data.pm_id,
            "asset_id": data.asset_id,
            "task_id": data.task_id,
            "frequency_interval": data.frequency_interval,
            "frequency_unit": data.frequency_unit,
            "next_due_date": data.next_due_date,
            "last_pm_date": data.last_pm_date,
            "last_work_order_no": data.last_work_order_no,
            "completion_window_days": data.completion_window_days,
            "standard_hours": data.standard_hours,
            "estimated_labor_hours": data.estimated_labor_hours,
            "assigned_vendor": data.assigned_vendor,
            "assigned_person": data.assigned_person,
            "is_suppressed": data.is_suppressed,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("pm_id", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(PmSchedule)
            .values(**values)
            .on_conflict_do_update(index_elements=["pm_id"], set_=update_cols)
        )
        await self.session.execute(stmt)
