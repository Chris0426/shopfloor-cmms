"""Inventory 讀取 DTO(pydantic v2)。API / MCP 回傳這些,不直接吐 ORM 物件。

decimal 以 float 呈現(讀取便利)。關聯(子類型/替代品/套件)以 code 清單另行查詢。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class InventoryItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    item_code: str
    item_category: str | None
    name: str | None
    description: str | None
    vendor_part_no: str | None
    quantity_on_hand: float | None
    reorder_point: float | None
    lead_time_weeks: int | None
    unit_cost: float | None
    currency: str
    bin_location: str | None
    supplier: str | None
    is_stocked: bool
    is_obsolete: bool


class InventoryItemDetail(InventoryItemRead):
    """品項 + 關聯(適用子類型 / 替代品 / 套件子項)。"""

    applicable_subtypes: list[str]
    alternatives: list[str]
    kit_children: list[str]
