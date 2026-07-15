"""Inventory 讀取 API(thin client,只呼叫 InventoryService)。寫入留待後續切片。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.domain.inventory.schemas import InventoryItemDetail, InventoryItemRead
from cmms.domain.inventory.service import InventoryService

router = APIRouter(tags=["inventory"])


@router.get("/inventory-items", response_model=list[InventoryItemRead])
async def list_inventory_items(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    item_category: str | None = None,
    supplier: str | None = None,
    is_stocked: bool | None = None,
    is_obsolete: bool | None = None,
    below_reorder: bool = False,
    asset_subtype: str | None = Query(None, description="只列適用此設備子類型的品項"),
) -> list[InventoryItemRead]:
    items = await InventoryService(session).list_items(
        limit=limit,
        offset=offset,
        item_category=item_category,
        supplier=supplier,
        is_stocked=is_stocked,
        is_obsolete=is_obsolete,
        below_reorder=below_reorder,
        asset_subtype=asset_subtype,
    )
    return [InventoryItemRead.model_validate(i) for i in items]


@router.get("/inventory-items/{item_code}", response_model=InventoryItemDetail)
async def get_inventory_item(
    item_code: str, session: AsyncSession = Depends(get_session)
) -> InventoryItemDetail:
    service = InventoryService(session)
    item = await service.get_item(item_code)
    if item is None:
        raise HTTPException(status_code=404, detail=f"inventory_item {item_code} not found")
    return InventoryItemDetail(
        **InventoryItemRead.model_validate(item).model_dump(),
        applicable_subtypes=await service.get_applicable_subtypes(item_code),
        alternatives=await service.get_alternatives(item_code),
        kit_children=await service.get_kit_children(item_code),
    )
