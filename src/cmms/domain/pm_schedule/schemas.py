"""PmSchedule 讀取 DTO(pydantic v2)。API / MCP 回傳這些,不直接吐 ORM 物件。

工時 / 窗口以 float 呈現(DB 為 Numeric→Decimal,pydantic 轉 float);日期以 date
呈現(MCP 端用 model_dump(mode="json") 轉 ISO 字串)。恆空的 [UI] 欄不列入 DTO。
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict


class PmScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    pm_id: str
    asset_id: str
    task_id: str
    frequency_interval: int
    frequency_unit: str | None
    next_due_date: date | None
    last_pm_date: date | None
    last_work_order_no: int | None
    completion_window_days: float | None
    standard_hours: float | None
    estimated_labor_hours: float | None
    assigned_vendor: str | None
    assigned_person: str | None
    is_suppressed: bool
