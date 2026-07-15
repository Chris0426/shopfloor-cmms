"""Attachment 讀取 API(thin client,唯讀)。回 presigned url;不外洩 R2 座標。

寫入(上傳)初期只走 loader / CLI(高風險不先暴露於 API);此處只開放 list / get。

接線:本檔由 Integrate 在 cmms.api.app 註冊(`app.include_router(attachments.router)`)。
`get_storage` DI 就地定義(避免改共用 deps.py;Integrate 可選擇上提至 deps.py 統一)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.domain.attachment.models import Attachment
from cmms.domain.attachment.schemas import AttachmentRead, AttachmentWithUrl
from cmms.domain.attachment.service import AttachmentService
from cmms.storage import StorageBackend, get_storage_backend

router = APIRouter(tags=["attachments"])


def get_storage() -> StorageBackend:
    """媒體 storage backend DI(預設真實 R2;未配置 → InMemory)。"""
    return get_storage_backend()


def _with_url(svc: AttachmentService, att: Attachment) -> AttachmentWithUrl:
    base = AttachmentRead.model_validate(att).model_dump()
    url, ttl = svc.presigned_url(att)
    return AttachmentWithUrl(**base, url=url, url_expires_in=ttl)


@router.get("/attachments", response_model=list[AttachmentWithUrl])
async def list_attachments(
    owner_type: str = Query(..., description="inventory_item / work_order / asset"),
    owner_id: str = Query(...),
    include_deleted: bool = False,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> list[AttachmentWithUrl]:
    svc = AttachmentService(session, storage)
    rows = await svc.list_attachments(
        owner_type, owner_id, include_deleted=include_deleted, limit=limit, offset=offset
    )
    return [_with_url(svc, a) for a in rows]


@router.get("/attachments/{attachment_id}", response_model=AttachmentWithUrl)
async def get_attachment(
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
    storage: StorageBackend = Depends(get_storage),
) -> AttachmentWithUrl:
    svc = AttachmentService(session, storage)
    att = await svc.get_attachment(attachment_id)
    if att is None:
        raise HTTPException(status_code=404, detail=f"attachment {attachment_id} not found")
    return _with_url(svc, att)
