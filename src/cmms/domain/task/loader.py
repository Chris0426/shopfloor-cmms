"""tasks.csv 載入器(migration 資料輸入)。

經 TaskService 單一寫入路徑寫入(ADR-001),idempotent(可重跑)。
tasks.csv 無 lookup 需種子(不像 assets 的 asset_type/department/line)— 只載 task 本體。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.task.service import TaskService
from cmms.domain.task.transform import TaskImport, row_to_import

# 一次性 migration 匯入：人工執行的資料載入
MIGRATION_ACTOR = Actor.human("migration")


@dataclass(frozen=True, slots=True)
class LoadResult:
    tasks: int


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 tasks.csv(utf-8-sig)。以表頭名對應欄位。"""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    imports: list[TaskImport] = [row_to_import(r) for r in rows]

    service = TaskService(session)
    async with service.write(MIGRATION_ACTOR):
        for imp in imports:
            await service.upsert_task(imp, MIGRATION_ACTOR)

    return LoadResult(tasks=len(imports))
