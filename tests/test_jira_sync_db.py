"""jira_sync DB 測試(ADR-020 決策 1 修訂;testcontainers)。無 Docker 自動 skip。

覆蓋:
- forward dry-run 零寫入 + 預覽形狀 + readiness。
- forward 執行:1 issue + N comment 按**全域 occurred_at 時序** + links forwarded + outbox sent。
- WO 缺 → 錯;PAT 缺 → dry-run readiness false / 執行誠實 fail;冪等重跑不重開 issue。
- add_note enqueue:連結後新增 note → outbox → flush 送出;軟刪 note 不同步;無連結不 enqueue。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

pytest.importorskip("testcontainers.postgres")
from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.config import get_settings  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.attachment.service import AttachmentService  # noqa: E402
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.service import IdentityService  # noqa: E402
from cmms.domain.identity.vault import CredentialVault  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.jira_sync.service import JiraSyncError, JiraSyncService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.models import (  # noqa: E402
    JiraOutbox,
    WoNoteType,
    WorkOrderExternalLink,
)
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402
from cmms.jira_forwarder import InMemoryJiraForwarder, JiraForwardError  # noqa: E402
from cmms.storage import InMemoryStorageBackend  # noqa: E402

HERMES = Actor.agent("hermes")


class FlakyCommentForwarder(InMemoryJiraForwarder):
    """append_mrq_comment 前 N 次拋錯(模擬 comment 失敗但附件已上傳)→ 測「重試不重上附件」。"""

    def __init__(self, fail_comments: int = 1) -> None:
        super().__init__()
        self._fail_left = fail_comments

    async def append_mrq_comment(self, *, external_key, body, idempotency_key=None):
        if self._fail_left > 0:
            self._fail_left -= 1
            raise JiraForwardError("comment boom")
        return await super().append_mrq_comment(
            external_key=external_key, body=body, idempotency_key=idempotency_key
        )

_ASSET_ROWS = [
    {"compid": "EID-002", "comp_desc": "Rig", "assettype": "Production", "department": "EQ",
     "line_no": "10K", "available": "Yes"},
]
# 兩張工單(同設備),各給不同 workstatus;note 由測試以明確 occurred_at 補入。
_WO_ROWS = [
    {"wo": "30167", "compid": "EID-002", "comp_desc": "x", "assetsubtp": "", "brief_desc": "a",
     "diag": "", "comments": "", "date_wo": "05/21/26", "sch_date": "", "wo_type": "REACTIVE",
     "workstatus": "H", "miscreated": "F", "assignto": "CMA", "edittime": "15:00:00",
     "editdate": "05/21/26", "edituser": "T", "time": "10:00:00", "time_cmpl": "15:00:00"},
    {"wo": "30168", "compid": "EID-002", "comp_desc": "x", "assetsubtp": "", "brief_desc": "b",
     "diag": "", "comments": "", "date_wo": "05/21/26", "sch_date": "", "wo_type": "REACTIVE",
     "workstatus": "H", "miscreated": "F", "assignto": "CMA", "edittime": "15:00:00",
     "editdate": "05/21/26", "edituser": "T", "time": "10:00:00", "time_cmpl": "15:00:00"},
]


def _fake_factory(fake: InMemoryJiraForwarder):
    return lambda _pat: fake


@pytest.fixture
async def ctx():
    """PG 容器 + master key/jira config env(cache_clear)+ 種子(2 WO + user jlee + PAT)。"""
    env_saved = {k: os.environ.get(k) for k in
                 ("CMMS_CREDENTIAL_MASTER_KEY", "CMMS_JIRA_BASE_URL", "CMMS_JIRA_MRQ_PROJECT_KEY")}
    os.environ["CMMS_CREDENTIAL_MASTER_KEY"] = Fernet.generate_key().decode()
    os.environ["CMMS_JIRA_BASE_URL"] = "https://jira.example"
    os.environ["CMMS_JIRA_MRQ_PROJECT_KEY"] = "MRQ"
    get_settings.cache_clear()
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load_assets(_ASSET_ROWS, s)
            await load_wo(_WO_ROWS, s)
            wo_svc = WorkOrderService(s)
            async with wo_svc.write(Actor.human("cli")):  # wo_note_type lookup(prod=migration seed)
                for code in ("report", "progress", "hold", "resume", "note"):
                    await wo_svc.upsert_lookup(WoNoteType, code, code)
            # attachment_owner_type lookup(create_all 不跑 migration 的 bulk_insert;照片測試需要)
            await s.execute(
                text("INSERT INTO attachment_owner_type (code, label) VALUES (:c, :l)"),
                {"c": "work_order_note", "l": "work_order_note"},
            )
            await s.commit()
            ident = IdentityService(s)
            await ident.create_user(
                user_id="jlee", username="jlee", display_name="C", password="password8",
                org="plant", actor=Actor.human("cli"),
            )
            await CredentialVault(s).store_credential(
                user_id="jlee", system="jira", secret="PAT-XYZ", actor=Actor.human("jlee"),
            )
            yield s
        await engine.dispose()
    for k, v in env_saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


async def _add_note(session, wo_no: int, body: str, hh: int) -> None:
    await WorkOrderService(session).add_note(
        wo_no, entry_type="progress", body=body, actor=Actor.human("jlee"),
        occurred_at=datetime(2026, 5, 22, hh, 0, tzinfo=UTC),
    )


async def _add_photo_note(session, storage, wo_no: int, body: str, hh: int, filename: str):
    """新增一筆 note 並掛一張照片(用同一 InMemory storage);回 (note, attachment)。"""
    note = await WorkOrderService(session).add_note(
        wo_no, entry_type="progress", body=body, actor=Actor.human("jlee"),
        occurred_at=datetime(2026, 5, 22, hh, 0, tzinfo=UTC),
    )
    att, _ = await AttachmentService(session, backend=storage).add_attachment(
        owner_type="work_order_note", owner_id=str(note.id), data=b"PHOTOBYTES-" + body.encode(),
        ext="jpg", content_type="image/jpeg", actor=Actor.human("jlee"),
        original_filename=filename,
    )
    return note, att


async def test_forward_dry_run_zero_writes(ctx) -> None:
    await _add_note(ctx, 30167, "first", 10)
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(InMemoryJiraForwarder()))
    result = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167, 30168], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=True,
    )
    assert result.dry_run is True and result.external_key is None
    assert result.total_comments == 1  # 只有 30167 有一筆 note
    assert {w.work_order_no for w in result.work_orders} == {30167, 30168}
    assert result.pat_ready is True and result.config_ready is True and result.warnings == []
    # 零寫入:無 link、無 outbox
    assert (await ctx.scalars(select(WorkOrderExternalLink))).all() == []
    assert (await ctx.scalars(select(JiraOutbox))).all() == []


async def test_forward_executes_global_time_order(ctx) -> None:
    # 跨工單交錯時間:WO30167@10、WO30168@09、WO30167@11 → 期望 comment 序 = 09,10,11
    await _add_note(ctx, 30167, "note-A-10", 10)
    await _add_note(ctx, 30168, "note-B-09", 9)
    await _add_note(ctx, 30167, "note-C-11", 11)
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake))
    result = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167, 30168], summary="Pump", description="Consolidated",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="batch-1",
    )
    assert result.external_key == "MRQ-1"
    assert result.total_comments == 3 and result.flush.sent == 3 and result.flush.failed == 0
    # 一張 issue
    assert len(fake.issues) == 1 and fake.issues["MRQ-1"]["summary"] == "Pump"
    bodies = [c.body for c in fake.comments["MRQ-1"]]
    assert len(bodies) == 3
    assert "note-B-09" in bodies[0] and "note-A-10" in bodies[1] and "note-C-11" in bodies[2]
    assert "[WO 30168 · 2026-05-22 17:00 · jlee]" in bodies[0]  # 台北 = UTC+8
    # links forwarded(兩張 WO)+ outbox 全 sent
    links = (await ctx.scalars(
        select(WorkOrderExternalLink).where(WorkOrderExternalLink.link_type == "forwarded")
    )).all()
    assert {link.work_order_no for link in links} == {30167, 30168}
    assert all(link.forward_idem_key == "batch-1" for link in links)
    outbox = (await ctx.scalars(select(JiraOutbox))).all()
    assert len(outbox) == 3 and all(o.status == "sent" and o.sent_comment_id for o in outbox)


async def test_forward_idempotent_rerun(ctx) -> None:
    await _add_note(ctx, 30167, "x", 10)
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake))
    r1 = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k1",
    )
    r2 = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k1",
    )
    assert r1.external_key == r2.external_key
    assert r2.already_forwarded is True
    assert len(fake.issues) == 1  # 不重開 MRQ


async def test_forward_missing_wo_errors(ctx) -> None:
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(InMemoryJiraForwarder()))
    with pytest.raises(JiraSyncError):
        await svc.forward_work_orders_to_mrq(
            work_order_nos=[30167, 99999], summary="S", description="D",
            acting_user="jlee", actor=HERMES, dry_run=True,
        )


async def test_forward_pat_missing_honest(ctx) -> None:
    await IdentityService(ctx).create_user(
        user_id="nopat", username="nopat", display_name="N", password="password8",
        org="plant", actor=Actor.human("cli"),
    )
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(InMemoryJiraForwarder()))
    # dry-run:readiness 誠實 false
    dry = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="nopat", actor=HERMES, dry_run=True,
    )
    assert dry.pat_ready is False and dry.warnings
    # 執行:誠實 fail(不假成功)
    with pytest.raises(JiraSyncError):
        await svc.forward_work_orders_to_mrq(
            work_order_nos=[30167], summary="S", description="D",
            acting_user="nopat", actor=HERMES, dry_run=False, idempotency_key="k",
        )


async def test_add_note_after_link_auto_enqueues_and_flushes(ctx) -> None:
    await _add_note(ctx, 30167, "initial", 10)
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake))
    await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    assert len(fake.comments["MRQ-1"]) == 1
    # 連結後新增 note → add_note 於同交易 enqueue(需求 ②)
    await _add_note(ctx, 30167, "follow-up-later", 12)
    pending = (await ctx.scalars(
        select(JiraOutbox).where(JiraOutbox.status == "pending")
    )).all()
    assert len(pending) == 1
    # flush 立即送出到既有 MRQ
    flush = await svc.flush_outbox(actor=Actor.human("jlee"))
    assert flush.sent == 1
    assert len(fake.comments["MRQ-1"]) == 2
    assert "follow-up-later" in fake.comments["MRQ-1"][1].body


async def test_no_link_no_enqueue(ctx) -> None:
    await _add_note(ctx, 30167, "orphan", 10)  # 無 forwarded link
    assert (await ctx.scalars(select(JiraOutbox))).all() == []


async def test_soft_deleted_note_not_synced(ctx) -> None:
    await _add_note(ctx, 30167, "keep", 10)
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake))
    await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    # 新增一筆後軟刪 → enqueue 了但 flush 時偵測 deleted → 不送(標 failed note-deleted)
    wo_svc = WorkOrderService(ctx)
    note = await wo_svc.add_note(
        30167, entry_type="progress", body="to-delete", actor=Actor.human("jlee"),
        occurred_at=datetime(2026, 5, 22, 13, 0, tzinfo=UTC),
    )
    note.deleted_at = datetime.now(UTC)
    await ctx.commit()
    flush = await svc.flush_outbox(actor=Actor.human("jlee"))
    assert flush.sent == 0 and flush.failed == 1
    assert len(fake.comments["MRQ-1"]) == 1  # 只有原本那筆,軟刪的沒送


async def test_flush_invalid_master_key_marks_outbox(ctx) -> None:
    """主鑰有設但格式無效(誤用 token_urlsafe)→ flush 誠實把 outbox 標 master-key-invalid。"""
    import secrets

    await _add_note(ctx, 30167, "initial", 10)
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake))
    await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    # 連結後新增一筆 → pending outbox
    await _add_note(ctx, 30167, "follow-up", 12)
    # 主鑰改成非法格式(43 字元 token_urlsafe;PAT 已用合法鑰存好)
    os.environ["CMMS_CREDENTIAL_MASTER_KEY"] = secrets.token_urlsafe(32)
    get_settings.cache_clear()
    flush = await svc.flush_outbox(actor=Actor.human("jlee"))
    assert flush.sent == 0 and flush.failed == 1
    ob = (await ctx.scalars(
        select(JiraOutbox).where(JiraOutbox.status == "failed")
    )).one()
    assert ob.last_error == "master-key-invalid"


# ---- 照片同步(工作紀錄照片 → MRQ comment 內嵌)----


async def test_forward_uploads_photos_then_comments(ctx) -> None:
    storage = InMemoryStorageBackend()
    note, _ = await _add_photo_note(ctx, storage, 30167, "with-photo", 10, "pump.jpg")
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake), storage=storage)
    result = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    assert result.flush.sent == 1 and result.total_photos == 1
    # 附件先上傳(1 張,防碰撞檔名),再送 comment(內嵌 + photos 標注)
    fn = f"wo30167-note{note.id}-pump.jpg"
    assert fake.attachments["MRQ-1"] == [(fn, b"PHOTOBYTES-with-photo", "image/jpeg")]
    body = fake.comments["MRQ-1"][0].body
    assert f"!{fn}|thumbnail!" in body and "(photos: 1)" in body  # 縮圖語法
    # 旗標落定
    ob = (await ctx.scalars(select(JiraOutbox))).one()
    assert ob.status == "sent" and ob.attachments_uploaded is True


async def test_dry_run_reports_photo_counts(ctx) -> None:
    storage = InMemoryStorageBackend()
    await _add_photo_note(ctx, storage, 30167, "p1", 10, "a.jpg")
    await _add_photo_note(ctx, storage, 30167, "p2", 11, "b.jpg")
    await _add_note(ctx, 30168, "no-photo", 12)
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(InMemoryJiraForwarder()),
                          storage=storage)
    result = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167, 30168], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=True,
    )
    assert result.total_photos == 2
    per = {w.work_order_no: w.photo_count for w in result.work_orders}
    assert per == {30167: 2, 30168: 0}
    # dry-run 零寫入
    assert (await ctx.scalars(select(JiraOutbox))).all() == []


async def test_retry_does_not_reupload_attachments(ctx) -> None:
    """comment 首次失敗但附件已上傳 → 旗標落定;重試只重送 comment、不重上附件。"""
    storage = InMemoryStorageBackend()
    note, _ = await _add_photo_note(ctx, storage, 30167, "photo", 10, "x.jpg")
    fake = FlakyCommentForwarder(fail_comments=1)
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake), storage=storage)
    r1 = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    assert r1.flush.sent == 0 and r1.flush.failed == 1
    assert len(fake.attachments["MRQ-1"]) == 1  # 附件上了一次
    ob = (await ctx.scalars(select(JiraOutbox))).one()
    assert ob.status == "failed" and ob.attachments_uploaded is True  # 旗標已落
    # 重試:comment 這次成功,附件不再上傳
    flush = await svc.flush_outbox(actor=Actor.human("jlee"))
    assert flush.sent == 1
    assert len(fake.attachments["MRQ-1"]) == 1  # 仍只有一次 = 不重上
    fn = f"wo30167-note{note.id}-x.jpg"
    assert f"!{fn}|thumbnail!" in fake.comments["MRQ-1"][0].body


async def test_missing_object_fails_honestly(ctx) -> None:
    storage = InMemoryStorageBackend()
    await _add_photo_note(ctx, storage, 30167, "photo", 10, "y.jpg")
    storage.objects.clear()  # R2 缺物件(下載會失敗)
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake), storage=storage)
    result = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    assert result.flush.sent == 0 and result.flush.failed == 1
    assert fake.comments.get("MRQ-1", []) == []  # comment 沒送(誠實 fail)
    ob = (await ctx.scalars(select(JiraOutbox))).one()
    assert ob.status == "failed" and ob.attachments_uploaded is False
    assert "attachment" in (ob.last_error or "")


async def test_soft_deleted_attachment_skipped(ctx) -> None:
    storage = InMemoryStorageBackend()
    note, att = await _add_photo_note(ctx, storage, 30167, "keep-note", 10, "keep.jpg")
    # 軟刪這張照片 → 同步時應跳過(comment 無內嵌、無上傳)
    await AttachmentService(ctx, backend=storage).soft_delete_attachment(
        att.id, actor=Actor.human("jlee")
    )
    fake = InMemoryJiraForwarder()
    svc = JiraSyncService(ctx, forwarder_factory=_fake_factory(fake), storage=storage)
    result = await svc.forward_work_orders_to_mrq(
        work_order_nos=[30167], summary="S", description="D",
        acting_user="jlee", actor=HERMES, dry_run=False, idempotency_key="k",
    )
    assert result.flush.sent == 1 and result.total_photos == 0
    assert "MRQ-1" not in fake.attachments  # 未上傳任何附件
    body = fake.comments["MRQ-1"][0].body
    assert "photos:" not in body  # 無照片標注
