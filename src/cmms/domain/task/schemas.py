"""Task 讀取 DTO(pydantic v2)。API / MCP 回傳這些,不直接吐 ORM 物件。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_no: str
    description: str
    is_active: bool
