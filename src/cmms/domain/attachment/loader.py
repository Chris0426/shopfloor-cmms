"""media loader(掃 data/media/inventory/ → 上 R2 → 記 attachment 指標)。

經 AttachmentService 單一寫入路徑(ADR-001),冪等(key 內嵌 sha8)。
- 多圖:同 owner 多檔 → 多列(不去重 owner;唯一鍵含 r2_key 故 1:N 成立)。
- 未對應 item_code:log 不爆(跳過、計數、回樣本)。
- 重跑:已存在 key → service 短路(不重傳、不重寫)。
前置:inventory 已載入(owner 存在性把關 + matched 判定)。

★ 交易模型(刻意偏離既有 loader):既有 loader 全批包單一 service.write();本 loader 改為
  **每檔一交易**(add_attachment 各自開 write())。理由:每檔含一次 R2 上傳(網路 I/O),
  不可在 DB 交易內跨網路 I/O 持有交易;且每檔獨立交易 → 中斷可續跑(content-addressed 冪等)。

★ Windows 長路徑警告:極少數檔名 + 絕對路徑超過 Windows MAX_PATH(260)時 p.is_file() 會
  回 False(stat 失敗)→ 該檔被靜默跳過;部署目標(Fly.io / Linux)無此限制,正常載入。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.attachment.service import AttachmentService
from cmms.domain.attachment.transform import content_type_for, parse_media_filename
from cmms.domain.inventory.models import InventoryItem
from cmms.storage import StorageBackend

MIGRATION_ACTOR = Actor.human("migration")
_IGNORE = {".gitkeep", "readme.md"}


@dataclass(frozen=True, slots=True)
class MediaLoadResult:
    scanned: int  # 掃到的候選檔
    created: int  # 新上傳 + 新指標
    existing: int  # 冪等短路(已存在)
    unmatched: int  # item_code 不在 inventory_item(跳過)
    unparseable: int  # 檔名無法解析(跳過)
    owners: int  # 觸及的 distinct owner
    unmatched_samples: list[str] = field(default_factory=list)  # 前 N 個未對應 item_code


def _scan_media_dir(media_dir: Path) -> list[Path]:
    """同步掃描候選媒體檔(排除忽略名)。供 asyncio.to_thread,使阻塞 FS I/O 不進 event loop。"""
    return sorted(
        p for p in media_dir.iterdir() if p.is_file() and p.name.lower() not in _IGNORE
    )


async def load(
    media_dir: Path,
    session: AsyncSession,
    *,
    owner_type: str = "inventory_item",
    backend: StorageBackend | None = None,
    sample_limit: int = 20,
) -> MediaLoadResult:
    service = AttachmentService(session, backend)
    # preload 已載入的 owner id(matched 判定;避免逐檔 owner 查詢)。
    # owner_type='asset' → 掃 data/media/asset/(檔名前導 token = EID,如 EID-70023.jpg)。
    if owner_type == "asset":
        from cmms.domain.asset.models import Asset  # 延遲匯入(僅此路徑需要)

        known = set((await session.scalars(select(Asset.asset_id))).all())
    else:
        known = set((await session.scalars(select(InventoryItem.item_code))).all())

    files = await asyncio.to_thread(_scan_media_dir, media_dir)
    created = existing = unmatched = unparseable = 0
    owners: set[str] = set()
    unmatched_samples: list[str] = []

    for path in files:
        parsed = parse_media_filename(path.name)
        if parsed is None:
            unparseable += 1
            continue
        if parsed.item_code not in known:  # 未對應 → log 不爆(計數 + 樣本)
            unmatched += 1
            if len(unmatched_samples) < sample_limit:
                unmatched_samples.append(parsed.item_code)
            continue
        data = await asyncio.to_thread(path.read_bytes)
        _att, was_created = await service.add_attachment(
            owner_type=owner_type,
            owner_id=parsed.item_code,
            data=data,
            ext=parsed.ext,
            content_type=content_type_for(parsed.ext),
            caption=parsed.caption,
            original_filename=path.name,
            actor=MIGRATION_ACTOR,
        )
        owners.add(parsed.item_code)
        created += int(was_created)
        existing += int(not was_created)

    return MediaLoadResult(
        scanned=len(files),
        created=created,
        existing=existing,
        unmatched=unmatched,
        unparseable=unparseable,
        owners=len(owners),
        unmatched_samples=unmatched_samples,
    )
