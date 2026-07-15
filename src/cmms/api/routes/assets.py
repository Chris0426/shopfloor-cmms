"""Asset 讀取 API(thin client,只呼叫 AssetService)。寫入留待後續切片。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.domain.asset.schemas import (
    AssetIdentityRead,
    AssetRead,
    AssetRelationshipRead,
    AssetTreeRead,
    ExternalIdRead,
)
from cmms.domain.asset.service import AssetService
from cmms.domain.work_order.schemas import WorkOrderRead

router = APIRouter(tags=["assets"])


@router.get("/assets", response_model=list[AssetRead])
async def list_assets(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    department: str | None = None,
    asset_type: str | None = None,
    line: str | None = None,
    available: bool | None = None,
) -> list[AssetRead]:
    svc = AssetService(session)
    assets = await svc.list_assets(
        limit=limit,
        offset=offset,
        department=department,
        asset_type=asset_type,
        line=line,
        available=available,
    )
    # 0031:批次填設備負責人清單(owners 全部 + owner 首位相容),免 N+1
    omap = await svc.owners_map([a.asset_id for a in assets])
    return [_asset_read(a, omap.get(a.asset_id, [])) for a in assets]


def _asset_read(asset, owners: list[str]) -> AssetRead:
    """AssetRead + 0031 負責人(owners 全部依 position;owner=首位 back-compat)。"""
    return AssetRead.model_validate(asset).model_copy(
        update={"owners": owners, "owner": owners[0] if owners else None}
    )


@router.get("/assets/{asset_id}", response_model=AssetRead)
async def get_asset(asset_id: str, session: AsyncSession = Depends(get_session)) -> AssetRead:
    svc = AssetService(session)
    asset = await svc.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    return _asset_read(asset, await svc.get_owners(asset_id))


@router.get("/assets/{asset_id}/identity", response_model=AssetIdentityRead)
async def get_asset_identity(
    asset_id: str, session: AsyncSession = Depends(get_session)
) -> AssetIdentityRead:
    """canonical 身分(ADR-015):asset_id + 所有外部系統 id。"""
    service = AssetService(session)
    if await service.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    ext = await service.list_external_ids(asset_id)
    return AssetIdentityRead(
        asset_id=asset_id,
        external_ids=[ExternalIdRead.model_validate(e) for e in ext],
    )


@router.get("/assets/{asset_id}/relationships", response_model=list[AssetRelationshipRead])
async def list_asset_relationships(
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    relationship_type: str | None = Query(None),
    direction: str = Query("both", pattern="^(from|to|both)$"),
    active_only: bool = Query(True),
) -> list[AssetRelationshipRead]:
    """資產組成邊(ADR-018):機台⊃模組 / 共用資源↔機台。"""
    service = AssetService(session)
    if await service.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    rels = await service.list_relationships(
        asset_id,
        relationship_type=relationship_type,
        direction=direction,
        active_only=active_only,
    )
    return [AssetRelationshipRead.model_validate(r) for r in rels]


@router.get("/assets/{asset_id}/tree", response_model=AssetTreeRead)
async def get_asset_tree(
    asset_id: str, session: AsyncSession = Depends(get_session)
) -> AssetTreeRead:
    """機台組成子樹(ADR-018):所有 contains_module 後代 + 本機台的現行關係邊。"""
    service = AssetService(session)
    if await service.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    descendants = await service.get_contained_descendants(asset_id)
    rels = await service.list_relationships(asset_id, direction="both", active_only=True)
    return AssetTreeRead(
        asset_id=asset_id,
        descendant_asset_ids=descendants,
        relationships=[AssetRelationshipRead.model_validate(r) for r in rels],
    )


@router.get("/assets/{asset_id}/work-orders/rollup", response_model=list[WorkOrderRead])
async def rollup_asset_work_orders(
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> list[WorkOrderRead]:
    """機台維護 rollup(ADR-018):自身 + contains_module 後代的工單(新到舊)。"""
    service = AssetService(session)
    if await service.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail=f"asset {asset_id} not found")
    wos = await service.rollup_work_orders(asset_id, limit=limit, offset=offset)
    return [WorkOrderRead.model_validate(w) for w in wos]


@router.get("/identity/resolve", response_model=AssetRead)
async def resolve_identity(
    namespace: str,
    external_id: str,
    session: AsyncSession = Depends(get_session),
) -> AssetRead:
    """用任一外部系統 id 反查同一台機器(ADR-015)。"""
    svc = AssetService(session)
    asset = await svc.resolve_by_external_id(namespace, external_id)
    if asset is None:
        raise HTTPException(status_code=404, detail=f"no asset for {namespace}:{external_id}")
    return _asset_read(asset, await svc.get_owners(asset.asset_id))
