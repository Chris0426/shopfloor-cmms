"""Task 讀取 API(thin client,只呼叫 TaskService)。寫入留待後續切片。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.domain.task.schemas import TaskRead
from cmms.domain.task.service import TaskService

router = APIRouter(tags=["tasks"])


@router.get("/tasks", response_model=list[TaskRead])
async def list_tasks(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    is_active: bool | None = None,
    search: str | None = Query(None, description="以任務描述關鍵字過濾"),
) -> list[TaskRead]:
    tasks = await TaskService(session).list_tasks(
        limit=limit, offset=offset, is_active=is_active, search=search
    )
    return [TaskRead.model_validate(t) for t in tasks]


@router.get("/tasks/{task_no}", response_model=TaskRead)
async def get_task(task_no: str, session: AsyncSession = Depends(get_session)) -> TaskRead:
    task = await TaskService(session).get_task(task_no)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_no} not found")
    return TaskRead.model_validate(task)
