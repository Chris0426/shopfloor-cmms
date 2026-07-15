"""scheduled_activity.csv 載入器(migration 資料輸入)。

經 PmScheduleService 單一寫入路徑寫入(ADR-001),idempotent(可重跑)。
lookup(freq_unit / vendor)由資料相異值動態種子(label 先＝code,之後 enrich,ADR-007)。

★ T3 兌現:載入完成後,於同一交易內經 TaskService 把未被任何 SA 引用的 task 標
is_active=false(關聯此時才完整,正是對帳時機)。前置:asset + task 已載入(FK)。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.pm_schedule.models import FreqUnit, Vendor
from cmms.domain.pm_schedule.service import PmScheduleService
from cmms.domain.pm_schedule.transform import PmScheduleImport, row_to_import
from cmms.domain.task.service import TaskService

# 一次性 migration 匯入：人工執行的資料載入
MIGRATION_ACTOR = Actor.human("migration")


@dataclass(frozen=True, slots=True)
class LoadResult:
    pm_schedules: int
    freq_units: int
    vendors: int
    idle_tasks_marked: int  # 本次新標記為 inactive 的 task 數(T3)


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 scheduled_activity.csv(utf-8-sig)。以表頭名對應欄位。"""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    imports: list[PmScheduleImport] = [row_to_import(r) for r in rows]

    freq_units = sorted({i.frequency_unit for i in imports if i.frequency_unit})
    vendors = sorted({i.assigned_vendor for i in imports if i.assigned_vendor})

    service = PmScheduleService(session)
    task_service = TaskService(session)
    async with service.write(MIGRATION_ACTOR):  # 單一交易涵蓋 SA upsert + task 對帳
        for code in freq_units:
            await service.upsert_lookup(FreqUnit, code, code)
        for code in vendors:
            await service.upsert_lookup(Vendor, code, code)  # label 待 ADR-007 enrich
        for imp in imports:
            await service.upsert_pm_schedule(imp, MIGRATION_ACTOR)

        # T3:依 SA 引用同步 task.is_active(未引用 → 已淘汰)
        referenced = {i.task_id for i in imports}
        idle_marked = await task_service.sync_active_status(referenced, MIGRATION_ACTOR)

    return LoadResult(
        pm_schedules=len(imports),
        freq_units=len(freq_units),
        vendors=len(vendors),
        idle_tasks_marked=idle_marked,
    )
