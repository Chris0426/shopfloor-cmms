"""task_steps_parts.csv 載入器(migration 0016 資料輸入)。

經 TaskService 單一寫入路徑(ADR-001),idempotent(可重跑)。每列 = 一個步驟 + 可選一個零件。

★ 編碼:cp1252(eMaint 匯出;utf-8 會解碼失敗)。
★ 冪等:task_step 依 `taskstep:v1:<task_no>:<occurrence>`(occurrence = 該 task_no 內檔案順序);
  task_part 依 (task_step_id, item_code)。假設檔為凍結檔、列序穩定(同 part_issue_backfill)。
★ FK 落空為正常(非錯誤):task 不在主檔 → MISSING_TASK(skip 整列);item 不在庫存主檔 →
  MISSING_ITEM(保留 step、只 skip 該 part)。承 ADR-018「不鑄 phantom id」。
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.task.service import TaskService
from cmms.domain.task.transform import row_to_step_import

# 一次性 migration 匯入:人工執行的資料載入(比照 task/pm loader)
MIGRATION_ACTOR = Actor.human("migration")


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 task_steps_parts.csv(cp1252)。表頭:task_no,proc_seq,task_desc,item,replaceqty。"""
    with path.open(encoding="cp1252", newline="") as fh:
        return list(csv.DictReader(fh))


def make_step_key(task_no: str, occurrence: int) -> str:
    """occurrence-based 冪等鍵:同 task_no 第 n 列得獨立鍵 → 重跑冪等。"""
    return f"taskstep:v1:{task_no}:{occurrence}"


@dataclass(frozen=True, slots=True)
class LoadResult:
    rows_read: int
    steps_loaded: int
    parts_loaded: int
    missing_task_skipped: int  # task 不在主檔 → skip 整列
    missing_item_skipped: int  # item 不在庫存 → 保留 step、skip part
    malformed_skipped: int
    missing_task_samples: list[str] = field(default_factory=list)
    missing_item_samples: list[str] = field(default_factory=list)


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    """載入保養細項(idempotent)。前置:tasks + inventory 已載(FK)。單一 write() 交易批次。"""
    rows = list(rows)
    service = TaskService(session)
    seen: Counter[str] = Counter()
    steps = parts = missing_task = missing_item = malformed = 0
    miss_task_samples: list[str] = []
    miss_item_samples: list[str] = []
    async with service.write(MIGRATION_ACTOR):
        for raw in rows:
            try:
                imp = row_to_step_import(raw)
            except ValueError:
                malformed += 1
                continue
            seen[imp.task_no] += 1
            step_id = await service.upsert_task_step(
                task_no=imp.task_no,
                proc_seq=imp.proc_seq,
                task_desc=imp.task_desc,
                idempotency_key=make_step_key(imp.task_no, seen[imp.task_no]),
                actor=MIGRATION_ACTOR,
            )
            if step_id is None:  # task 不在主檔 → skip 整列
                missing_task += 1
                if len(miss_task_samples) < 20:
                    miss_task_samples.append(imp.task_no)
                continue
            steps += 1
            if imp.item_code:
                ok = await service.upsert_task_part(
                    task_step_id=step_id,
                    item_code=imp.item_code,
                    replace_qty=imp.replace_qty,
                    actor=MIGRATION_ACTOR,
                )
                if ok:
                    parts += 1
                else:  # item 不在庫存 → 保留 step、skip part
                    missing_item += 1
                    if len(miss_item_samples) < 20:
                        miss_item_samples.append(imp.item_code)
    return LoadResult(
        rows_read=len(rows),
        steps_loaded=steps,
        parts_loaded=parts,
        missing_task_skipped=missing_task,
        missing_item_skipped=missing_item,
        malformed_skipped=malformed,
        missing_task_samples=miss_task_samples,
        missing_item_samples=miss_item_samples,
    )
