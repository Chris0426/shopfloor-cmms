"""Attachment 切片的 DB 整合測試(testcontainers-postgres + InMemory fake backend)。

本機無 Docker 時自動 skip。驗證:add_attachment 上傳 + 指標、冪等短路、多圖 1:N、
未對應 owner 拒記(含 work_order 非數字 id 硬化)、soft-delete + list 過濾、presigned url、
loader 計數(matched / unmatched / unparseable / 多圖 / 冪等重跑)。

★ 不需真 R2:InMemoryStorageBackend 隨 app 出貨,bytes 存記憶體。
★ 不需 config 接線:bucket / ttl 走 storage.media_bucket()/url_ttl_seconds() 的安全預設。
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402

# create_all 需完整 Base.metadata(隔離跑時無全套件 collection 自動註冊);比照 migrations/env.py
# 全量 import 所有切片 model,確保 work_order→vendor(pm_schedule)等跨切片 FK 目標都在。
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.attachment.loader import load as load_media  # noqa: E402
from cmms.domain.attachment.models import Attachment  # noqa: E402
from cmms.domain.attachment.service import AttachmentError, AttachmentService  # noqa: E402
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.storage import InMemoryStorageBackend  # noqa: E402

ACTOR = Actor.human("test")


async def _seed(session) -> None:
    """種子:attachment_owner_type(create_all 不跑 migration 的 bulk_insert)+ owner 主表列。"""
    for code, label in (
        ("inventory_item", "備品"),
        ("work_order", "工單"),
        ("asset", "設備"),
    ):
        await session.execute(
            text("INSERT INTO attachment_owner_type (code, label) VALUES (:c, :l)"),
            {"c": code, "l": label},
        )
    # inventory_item owner(currency/is_stocked/is_obsolete 有 server_default)
    for code in ("ES0001", "ES0002"):
        await session.execute(
            text("INSERT INTO inventory_item (item_code) VALUES (:c)"), {"c": code}
        )
    # asset owner(需 asset_type FK 目標)
    await session.execute(text("INSERT INTO asset_type (code, label) VALUES ('Production','p')"))
    # site 為 nullable=False 但僅 Python-side default(無 server_default)→ 裸 INSERT 須帶值
    await session.execute(
        text(
            "INSERT INTO asset (asset_id, description, asset_type, site) "
            "VALUES ('EID-001', 'Pump', 'Production', 'PLANT-1')"
        )
    )
    await session.commit()


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await _seed(s)
            yield s
        await engine.dispose()


async def _count_rows(session) -> int:
    return len((await session.scalars(select(Attachment.id))).all())


async def test_add_uploads_and_records_pointer(session) -> None:
    backend = InMemoryStorageBackend()
    svc = AttachmentService(session, backend)
    att, created = await svc.add_attachment(
        owner_type="inventory_item",
        owner_id="es0001",  # 小寫 → 服務正規化為 ES0001
        data=b"img-a",
        ext="jpg",
        content_type="image/jpeg",
        actor=ACTOR,
        caption="pump photo",
    )
    assert created is True
    assert att.owner_id == "ES0001"  # canonical 大寫
    assert att.r2_bucket == "cmms-media"
    assert att.r2_key.startswith("inventory/ES0001/") and att.r2_key.endswith(".jpg")
    assert att.byte_size == 5 and len(att.sha256) == 64
    assert att.source_actor == "human:test" and att.created_by == "human:test"
    # 二進位確實進 backend(content-addressed key)
    assert backend.objects[("cmms-media", att.r2_key)] == (b"img-a", "image/jpeg")


async def test_idempotent_same_bytes_no_duplicate(session) -> None:
    backend = InMemoryStorageBackend()
    svc = AttachmentService(session, backend)
    a1, c1 = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"same",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    a2, c2 = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"same",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    assert c1 is True and c2 is False  # 第二次冪等短路
    assert a1.id == a2.id
    assert await _count_rows(session) == 1
    assert len(backend.objects) == 1  # 不重傳


async def test_multi_image_same_owner_is_one_to_many(session) -> None:
    backend = InMemoryStorageBackend()
    svc = AttachmentService(session, backend)
    a1, c1 = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"image-1",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    a2, c2 = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"image-2",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    assert c1 and c2  # 不同內容 → 兩列(唯一鍵含 r2_key,不擋同 owner)
    assert a1.id != a2.id
    rows = await svc.list_attachments("inventory_item", "ES0001")
    assert len(rows) == 2
    assert len(backend.objects) == 2


async def test_unknown_owner_rejected(session) -> None:
    svc = AttachmentService(session, InMemoryStorageBackend())
    # inventory_item 不存在 → 拒記孤兒附件
    with pytest.raises(AttachmentError):
        await svc.add_attachment(
            owner_type="inventory_item", owner_id="NOPE9999", data=b"x",
            ext="jpg", content_type="image/jpeg", actor=ACTOR,
        )
    # 未知 owner_type
    with pytest.raises(AttachmentError):
        await svc.add_attachment(
            owner_type="spaceship", owner_id="ES0001", data=b"x",
            ext="jpg", content_type="image/jpeg", actor=ACTOR,
        )
    # work_order 非數字 id → A6 硬化(回 False → 拒,不丟未捕捉 ValueError)
    with pytest.raises(AttachmentError):
        await svc.add_attachment(
            owner_type="work_order", owner_id="not-an-int", data=b"x",
            ext="jpg", content_type="image/jpeg", actor=ACTOR,
        )


async def test_asset_owner_supported(session) -> None:
    svc = AttachmentService(session, InMemoryStorageBackend())
    att, created = await svc.add_attachment(
        owner_type="asset", owner_id="EID-001", data=b"machine",
        ext="png", content_type="image/png", actor=ACTOR,
    )
    assert created and att.owner_type == "asset"
    assert att.r2_key.startswith("asset/EID-001/")


async def test_soft_delete_and_list_filter(session) -> None:
    svc = AttachmentService(session, InMemoryStorageBackend())
    att, _ = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"d",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    deleted = await svc.soft_delete_attachment(att.id, ACTOR)
    assert deleted.is_deleted is True and deleted.updated_by == "human:test"
    # 預設排除 soft-deleted
    assert await svc.list_attachments("inventory_item", "ES0001") == []
    incl = await svc.list_attachments("inventory_item", "ES0001", include_deleted=True)
    assert len(incl) == 1
    # 重複刪除冪等
    again = await svc.soft_delete_attachment(att.id, ACTOR)
    assert again.is_deleted is True


async def test_governance_counts_recent_deleted_restore(session) -> None:
    """ADR-019 /admin/attachments 讀取 + 還原:counts_by_owner_type / list_recent_uploads /
    list_soft_deleted / restore_attachment 對真 DB 的正確性。"""
    svc = AttachmentService(session, InMemoryStorageBackend())
    a1, _ = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"one",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0002", data=b"two",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    await svc.add_attachment(
        owner_type="asset", owner_id="EID-001", data=b"three",
        ext="png", content_type="image/png", actor=ACTOR,
    )
    # counts:未軟刪,按 owner_type 分組
    counts = await svc.counts_by_owner_type()
    assert counts == {"inventory_item": 2, "asset": 1}
    # 最近上傳(未軟刪)
    recent = await svc.list_recent_uploads(limit=10)
    assert len(recent) == 3
    # 軟刪 a1 → 移出 counts/recent、進 soft-deleted 清單
    await svc.soft_delete_attachment(a1.id, ACTOR)
    assert (await svc.counts_by_owner_type()) == {"inventory_item": 1, "asset": 1}
    deleted = await svc.list_soft_deleted(limit=10)
    assert [d.id for d in deleted] == [a1.id]
    # 還原 → 回到現存(冪等:再還原 no-op)
    restored = await svc.restore_attachment(a1.id, ACTOR)
    assert restored.is_deleted is False and restored.updated_by == "human:test"
    assert (await svc.list_soft_deleted()) == []
    assert (await svc.counts_by_owner_type()) == {"inventory_item": 2, "asset": 1}
    # 還原不存在的 → AttachmentError
    with pytest.raises(AttachmentError):
        await svc.restore_attachment(999999, ACTOR)


async def test_presigned_url(session) -> None:
    svc = AttachmentService(session, InMemoryStorageBackend())
    att, _ = await svc.add_attachment(
        owner_type="inventory_item", owner_id="ES0001", data=b"d",
        ext="jpg", content_type="image/jpeg", actor=ACTOR,
    )
    url, ttl = svc.presigned_url(att)
    assert ttl == 900
    assert url == f"memory://cmms-media/{att.r2_key}?ttl=900"


async def test_loader_counts_and_idempotency(session, tmp_path: Path) -> None:
    backend = InMemoryStorageBackend()
    media = tmp_path
    (media / "es0001 pump.jpg").write_bytes(b"AAAA")
    (media / "es0001.jpg").write_bytes(b"BBBB")  # 同 owner 不同內容 → 多圖
    (media / "es0002.png").write_bytes(b"CCCC")  # caption-less
    (media / "nope0001 ghost.jpg").write_bytes(b"DDDD")  # 未對應 item_code
    (media / "es0001").write_bytes(b"EEEE")  # 無副檔名 → unparseable
    (media / ".gitkeep").write_bytes(b"")  # 忽略

    r = await load_media(media, session, backend=backend)
    assert r.scanned == 5  # 不含 .gitkeep
    assert r.created == 3  # es0001 pump / es0001 / es0002
    assert r.existing == 0
    assert r.unmatched == 1 and r.unmatched_samples == ["NOPE0001"]
    assert r.unparseable == 1
    assert r.owners == 2  # ES0001, ES0002

    # 重跑冪等:全部短路為 existing,不新增列、不重傳
    r2 = await load_media(media, session, backend=backend)
    assert r2.created == 0 and r2.existing == 3
    assert await _count_rows(session) == 3
