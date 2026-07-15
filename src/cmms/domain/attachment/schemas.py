"""Attachment 讀取 DTO(pydantic v2)。API / MCP 回這些,不直接吐 ORM。

`r2_bucket` / `r2_key`(內部座標)不外洩;對外只給短期 presigned url。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_type: str
    owner_id: str
    content_type: str
    caption: str | None
    byte_size: int
    sha256: str
    original_filename: str | None
    is_deleted: bool
    created_at: datetime


class AttachmentWithUrl(AttachmentRead):
    """讀取時附 presigned GET url(短期、TTL 內有效)。bucket 維持私有。"""

    url: str
    url_expires_in: int  # 秒
