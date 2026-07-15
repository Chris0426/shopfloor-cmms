"""AttachmentService —— 媒體切片唯一寫入路徑(ADR-001/003、ADR-019)。

讀:get / list(預設排除 soft-deleted)+ presign(委派 StorageBackend)。
寫:add_attachment(治理:驗 owner → 上傳 R2 → 寫指標,冪等)、soft_delete_attachment。
StorageBackend 注入(預設真實 R2;測試傳 InMemory)。

★ 上傳在 self.write() 交易**外** orchestrate(不在 DB 交易內持有網路 I/O);DB 指標寫入
  才進 self.write() 受治理交易。冪等靠 (owner_type, owner_id, r2_key) 唯一鍵(內嵌 sha8)
  + on_conflict_do_nothing,重送同內容不重傳不重寫(護欄 #4)。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.asset.models import Asset
from cmms.domain.attachment.models import Attachment
from cmms.domain.attachment.transform import OWNER_PREFIX, make_r2_key, sha256_hex
from cmms.domain.base import DomainService
from cmms.domain.inventory.models import InventoryItem
from cmms.domain.work_order.models import WorkOrder, WorkOrderNote
from cmms.storage import StorageBackend, get_storage_backend, media_bucket, url_ttl_seconds

# owner_type → (model, pk 欄位);owner 存在性把關用(多型軟參照無 hard FK)。
_OWNER_MODELS = {
    "inventory_item": (InventoryItem, InventoryItem.item_code),
    "asset": (Asset, Asset.asset_id),
    "work_order": (WorkOrder, WorkOrder.work_order_no),
    "work_order_note": (WorkOrderNote, WorkOrderNote.id),  # 逐筆日誌照片(§1.6)
}


class AttachmentError(Exception):
    pass


class AttachmentService(DomainService):
    def __init__(self, session: AsyncSession, backend: StorageBackend | None = None) -> None:
        super().__init__(session)
        self._backend = backend or get_storage_backend()

    # ---- 讀取(ADR-004)----

    async def get_attachment(self, attachment_id: int) -> Attachment | None:
        return await self.session.get(Attachment, attachment_id)

    async def list_attachments(
        self,
        owner_type: str,
        owner_id: str,
        *,
        include_deleted: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Attachment]:
        stmt = select(Attachment).where(
            Attachment.owner_type == owner_type,
            Attachment.owner_id == owner_id.upper(),
        )
        if not include_deleted:
            stmt = stmt.where(Attachment.is_deleted.is_(False))
        stmt = stmt.order_by(Attachment.id).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def first_attachment_map(
        self, owner_type: str, owner_ids: list[str]
    ) -> dict[str, Attachment]:
        """批次取每個 owner 的第一張附件(browse 縮圖用;單一查詢,避免 N+1)。

        回 {owner_id: attachment};無附件的 owner 不入 dict(呼叫端 fallback 圖示)。
        """
        if not owner_ids:
            return {}
        stmt = (
            select(Attachment)
            .where(
                Attachment.owner_type == owner_type,
                Attachment.owner_id.in_([o.upper() for o in owner_ids]),
                Attachment.is_deleted.is_(False),
            )
            .order_by(Attachment.owner_id, Attachment.id)
        )
        out: dict[str, Attachment] = {}
        for att in (await self.session.scalars(stmt)).all():
            out.setdefault(att.owner_id, att)  # 每 owner 取最早一張
        return out

    async def attachments_map(
        self, owner_type: str, owner_ids: list[str]
    ) -> dict[str, list[Attachment]]:
        """批次取一群 owner 的全部附件(工單詳情時間線照片用;單一 IN 查詢,免 N+1)。

        回 {owner_id: [attachments…有序]};無附件的 owner 不入 dict。
        """
        if not owner_ids:
            return {}
        stmt = (
            select(Attachment)
            .where(
                Attachment.owner_type == owner_type,
                Attachment.owner_id.in_([o.upper() for o in owner_ids]),
                Attachment.is_deleted.is_(False),
            )
            .order_by(Attachment.owner_id, Attachment.id)
        )
        out: dict[str, list[Attachment]] = {}
        for att in (await self.session.scalars(stmt)).all():
            out.setdefault(att.owner_id, []).append(att)
        return out

    # ---- 治理總覽讀取(ADR-019 /admin/attachments;讀取開放)----

    async def counts_by_owner_type(self) -> dict[str, int]:
        """各 owner_type 的現存(未軟刪)附件數(治理總覽)。"""
        stmt = (
            select(Attachment.owner_type, func.count())
            .where(Attachment.is_deleted.is_(False))
            .group_by(Attachment.owner_type)
        )
        return {row[0]: row[1] for row in (await self.session.execute(stmt)).all()}

    async def list_recent_uploads(self, *, limit: int = 20) -> list[Attachment]:
        """最近上傳的附件(未軟刪),新到舊。"""
        stmt = (
            select(Attachment)
            .where(Attachment.is_deleted.is_(False))
            .order_by(Attachment.created_at.desc(), Attachment.id.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_soft_deleted(self, *, limit: int = 50) -> list[Attachment]:
        """已軟刪的附件(R2 物件仍保留供稽核 / 可還原),新到舊。"""
        stmt = (
            select(Attachment)
            .where(Attachment.is_deleted.is_(True))
            .order_by(Attachment.updated_at.desc(), Attachment.id.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    def presigned_url(
        self, att: Attachment, *, ttl_seconds: int | None = None
    ) -> tuple[str, int]:
        """回 (presigned GET url, ttl 秒)。委派 backend;bucket 維持私有(不外洩座標)。"""
        ttl = ttl_seconds or url_ttl_seconds()
        url = self._backend.presigned_get_url(
            bucket=att.r2_bucket, key=att.r2_key, ttl_seconds=ttl
        )
        return url, ttl

    # ---- 寫入(指標寫入經 self.write() 交易;上傳在交易外)----

    async def add_attachment(
        self,
        *,
        owner_type: str,
        owner_id: str,
        data: bytes,
        ext: str,
        content_type: str,
        actor: Actor,
        caption: str | None = None,
        original_filename: str | None = None,
        idempotency_key: str | None = None,
    ) -> tuple[Attachment, bool]:
        """治理寫入:上傳二進位到 R2 + 記指標列。回 (attachment, created)。

        冪等(護欄 #4):key 內嵌 sha8;同 (owner_type, owner_id, r2_key) 已存在
        → 回現有列、created=False,不重傳、不重寫。`idempotency_key` 介面位保留給
        agent 寫入路徑(ADR-006 一致;content-addressed key 已提供回填冪等)。
        """
        if owner_type not in OWNER_PREFIX:
            raise AttachmentError(f"unknown owner_type: {owner_type}")
        owner_id = owner_id.upper()
        if not await self._owner_exists(owner_type, owner_id):
            raise AttachmentError(f"{owner_type} {owner_id} 不存在(拒記孤兒附件)")

        sha = sha256_hex(data)
        bucket = media_bucket()
        key = make_r2_key(OWNER_PREFIX[owner_type], owner_id, sha, ext)

        # 1. 冪等短路:已記過同 owner 同 key → 直接回(省重傳)
        existing = await self._get_by_owner_key(owner_type, owner_id, key)
        if existing is not None:
            return existing, False

        # 2. 上傳 R2(DB 交易外;content-addressed → 重傳同 bytes 等冪)
        await self._backend.put_object(
            bucket=bucket, key=key, data=data, content_type=content_type
        )

        # 3. 治理 DB 寫入(pointer);on_conflict 擋並發競態
        async with self.write(actor):
            stmt = (
                pg_insert(Attachment)
                .values(
                    owner_type=owner_type,
                    owner_id=owner_id,
                    r2_bucket=bucket,
                    r2_key=key,
                    content_type=content_type,
                    caption=caption,
                    byte_size=len(data),
                    sha256=sha,
                    original_filename=original_filename,
                    source_actor=actor.value,
                    created_by=actor.value,
                )
                .on_conflict_do_nothing(index_elements=["owner_type", "owner_id", "r2_key"])
                .returning(Attachment.id)
            )
            new_id = await self.session.scalar(stmt)
        if new_id is None:  # 競態:別人剛插入 → 取回現有
            row = await self._get_by_owner_key(owner_type, owner_id, key)
            assert row is not None  # on_conflict 命中 → 該列必存在
            return row, False
        created = await self.session.get(Attachment, new_id)
        assert created is not None
        return created, True

    async def soft_delete_attachment(
        self, attachment_id: int, actor: Actor, *, reason: str | None = None
    ) -> Attachment:
        """軟刪除(is_deleted=true + 稽核 updated_by)。R2 物件保留供稽核 / undelete;
        硬 GC 留未來批次。重複刪除冪等(已刪再刪 = no-op 維持終態)。"""
        async with self.write(actor):
            att = await self.session.get(Attachment, attachment_id)
            if att is None:
                raise AttachmentError(f"attachment {attachment_id} not found")
            att.is_deleted = True
            att.updated_by = actor.value
            att.source_actor = actor.value
        return att

    async def restore_attachment(self, attachment_id: int, actor: Actor) -> Attachment:
        """還原軟刪的附件(is_deleted=false + 稽核 updated_by;R2 物件本就保留,無需重傳)。
        重複還原冪等(未刪再還原 = no-op 維持現狀)。admin-only(呼叫端 require_admin)。"""
        async with self.write(actor):
            att = await self.session.get(Attachment, attachment_id)
            if att is None:
                raise AttachmentError(f"attachment {attachment_id} not found")
            att.is_deleted = False
            att.updated_by = actor.value
            att.source_actor = actor.value
        return att

    # ---- 私有 ----

    async def _owner_exists(self, owner_type: str, owner_id: str) -> bool:
        model, pk = _OWNER_MODELS[owner_type]
        if owner_type in ("work_order", "work_order_note"):
            try:
                key: object = int(owner_id)  # work_order_no / note.id 為 BigInteger PK
            except ValueError:
                return False  # 非數字 id → 視為不存在(A6 硬化)
        else:
            key = owner_id
        return (await self.session.scalar(select(pk).where(pk == key))) is not None

    async def _get_by_owner_key(
        self, owner_type: str, owner_id: str, r2_key: str
    ) -> Attachment | None:
        return await self.session.scalar(
            select(Attachment).where(
                Attachment.owner_type == owner_type,
                Attachment.owner_id == owner_id,
                Attachment.r2_key == r2_key,
            )
        )
