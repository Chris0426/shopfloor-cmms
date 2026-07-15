"""PM 排程 API(thin client,只呼叫 domain service)。

讀取:list(含到期清單過濾)/ get。寫入:按需生成 PM 工單(ADR-021,經 WorkOrderService 單一寫入路徑)。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.audit import Actor
from cmms.domain.pm_schedule.schemas import PmScheduleRead
from cmms.domain.pm_schedule.service import PmScheduleService
from cmms.domain.work_order.schemas import WorkOrderRead
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService

router = APIRouter(tags=["pm-schedules"])


@router.get("/pm-schedules", response_model=list[PmScheduleRead])
async def list_pm_schedules(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    asset_id: str | None = None,
    task_id: str | None = None,
    assigned_vendor: str | None = None,
    is_suppressed: bool | None = None,
    due_on_or_before: date | None = Query(None, description="只列到期日 <= 此日且非空者"),
) -> list[PmScheduleRead]:
    schedules = await PmScheduleService(session).list_pm_schedules(
        limit=limit,
        offset=offset,
        asset_id=asset_id,
        task_id=task_id,
        assigned_vendor=assigned_vendor,
        is_suppressed=is_suppressed,
        due_on_or_before=due_on_or_before,
    )
    return [PmScheduleRead.model_validate(s) for s in schedules]


@router.get("/pm-schedules/{pm_id}", response_model=PmScheduleRead)
async def get_pm_schedule(
    pm_id: str, session: AsyncSession = Depends(get_session)
) -> PmScheduleRead:
    schedule = await PmScheduleService(session).get_pm_schedule(pm_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail=f"pm_schedule {pm_id} not found")
    return PmScheduleRead.model_validate(schedule)


@router.post(
    "/pm-schedules/{pm_id}/generate-work-order",
    response_model=WorkOrderRead,
    status_code=201,
)
async def generate_pm_work_order(
    pm_id: str,
    actor_id: str = Query(..., description="發起工程師 id(記為 source_actor=human:<id>)"),
    session: AsyncSession = Depends(get_session),
) -> WorkOrderRead:
    """按需生成 PM 工單(ADR-021):工程師對到期 PM 按「執行」→ 開一張 PM 工單。

    冪等:本週期已有未結案 PM 工單 → 回該單(不重複生成)。即使該 PM 被 suppress 仍允許
    (真人明確覆寫;suppress 僅治理自動排程器 + 到期清單)。
    """
    try:
        wo = await WorkOrderService(session).generate_pm_work_order(
            pm_id=pm_id, actor=Actor.human(actor_id)
        )
    except WorkOrderError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return WorkOrderRead.model_validate(wo)
