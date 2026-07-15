"""notify DB 測試(Slice B;testcontainers)。無 Docker 自動 skip。

覆蓋:
- 開 REACTIVE WO → 廣播 + 負責人比對收件人、僅填了的通道各一列;無收件人 → 零列。
- 結案 → 'closed' 列;唯一鍵去重(重複 enqueue 不重發)。
- flush(InMemory 兩通道)→ sent + provider id + 內文抽查(主旨前綴 / 台北時間 / 連結)。
- flush 失敗 sender → 誠實 failed + attempts++;未配置通道 → 整列略過(仍 pending、attempts 0)。
- 收件人 CRUD admin-only(engineer 拒)+ 驗證(無通道 → 錯)。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("testcontainers.postgres")
from datetime import UTC, datetime  # noqa: E402

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.config import get_settings  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.service import AuthorizationError, IdentityService  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.notify import models as _notify_models  # noqa: E402, F401
from cmms.domain.notify.models import (  # noqa: E402
    NotificationOutbox,
    NotifyRecipient,
    NotifyWatch,
)
from cmms.domain.notify.service import (  # noqa: E402
    NotificationService,
    NotifyError,
    enqueue_work_order_notifications,
)
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402
from cmms.email import InMemoryEmailSender  # noqa: E402
from cmms.telegram import InMemoryTelegramSender, TelegramError  # noqa: E402

ADMIN = Actor.human("admin")
OPENED_AT = datetime(2026, 7, 11, 6, 32, tzinfo=UTC)  # 台北 14:32

_ASSET_ROWS = [
    {"compid": "EID-002", "comp_desc": "Aligner46", "assettype": "Production", "department": "EQ",
     "line_no": "ASSY", "available": "Yes"},
]

_STATUSES = [
    ("OPEN", 0, False, False),
    ("IN_PROGRESS", 1, False, True),
    ("ON_HOLD", 2, False, False),
    ("COMPLETED", 3, False, False),
    ("CLOSED", 4, True, False),
    ("CANCELLED", 5, True, False),
    ("VOIDED", 6, True, False),
]


class FailTelegram(InMemoryTelegramSender):
    async def send(self, *, chat_id: str, text: str) -> str:
        raise TelegramError("telegram boom")


@pytest.fixture
async def ctx():
    """PG 容器 + notify_from env + 種子(asset / 狀態 / work_type / users admin+eng)。"""
    saved = {k: os.environ.get(k) for k in
             ("CMMS_NOTIFY_FROM", "CMMS_SMTP_HOST", "CMMS_TELEGRAM_BOT_TOKEN")}
    os.environ["CMMS_NOTIFY_FROM"] = "cmms@example.com.local"
    os.environ.pop("CMMS_SMTP_HOST", None)  # 確保非注入 email 通道不算已配置
    os.environ.pop("CMMS_TELEGRAM_BOT_TOKEN", None)
    get_settings.cache_clear()
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load_assets(_ASSET_ROWS, s)
            wo_svc = WorkOrderService(s)
            async with wo_svc.write(Actor.human("cli")):
                for code in ("REACTIVE", "PM"):
                    await wo_svc.upsert_lookup(_wo_models.WorkType, code, code)
                for code, rank, term, dt in _STATUSES:
                    await wo_svc.upsert_status(
                        code, code, rank=rank, is_terminal=term, is_downtime=dt
                    )
                for code in ("report", "progress", "hold", "resume", "note"):
                    await wo_svc.upsert_lookup(_wo_models.WoNoteType, code, code)
            ident = IdentityService(s)
            await ident.create_user(
                user_id="admin", username="admin", display_name="A", password="password8",
                org="plant", role="admin", actor=Actor.human("cli"),
            )
            await ident.create_user(
                user_id="eng", username="eng", display_name="E", password="password8",
                org="plant", role="engineer", actor=Actor.human("cli"),
            )
            yield s
        await engine.dispose()
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()


async def _seed_recipients(s) -> dict[str, int]:
    """team(廣播 open+close,email+tg)/ owner(比對 Alice Fang,email)/ other(比對 Nobody)/
    inactive(廣播但停用)。回 {name: id}。"""
    svc = NotificationService(s)
    team = await svc.create_recipient(
        name="team", email="team@x.com", telegram_chat_id="-100123",
        notify_on_open=True, notify_on_close=True, actor=ADMIN,
    )
    owner = await svc.create_recipient(
        name="owner", email="owner@x.com", assignee_name="Alice Fang", actor=ADMIN,
    )
    other = await svc.create_recipient(
        name="other", email="other@x.com", assignee_name="Nobody", actor=ADMIN,
    )
    inactive = await svc.create_recipient(
        name="inactive", email="inactive@x.com", notify_on_open=True, actor=ADMIN,
    )
    await svc.set_recipient_active(inactive.id, False, actor=ADMIN)
    return {"team": team.id, "owner": owner.id, "other": other.id, "inactive": inactive.id}


async def _open_wo(s, **kw):
    return await WorkOrderService(s).open_work_order(
        asset_id="EID-002", work_type="REACTIVE", actor=ADMIN,
        brief_description="馬達過熱", opened_by="assy-ipad",
        assigned_person="Alice Fang", at=OPENED_AT, **kw,
    )


async def _outbox(s) -> list[NotificationOutbox]:
    return list((await s.scalars(select(NotificationOutbox).order_by(NotificationOutbox.id))).all())


# ---- enqueue ----

async def test_open_enqueues_broadcast_and_owner(ctx) -> None:
    ids = await _seed_recipients(ctx)
    await _open_wo(ctx)
    rows = await _outbox(ctx)
    pairs = {(r.recipient_id, r.channel) for r in rows}
    # team:email+tg(廣播);owner:email(比對命中);other/inactive:無
    assert pairs == {
        (ids["team"], "email"), (ids["team"], "telegram"), (ids["owner"], "email"),
    }
    assert all(r.event == "opened" and r.status == "pending" for r in rows)


async def test_no_recipients_zero_rows(ctx) -> None:
    await _open_wo(ctx)
    assert await _outbox(ctx) == []


async def test_open_matches_any_assignee(ctx) -> None:
    """0031:通知比對工單**任一**負責人(非只 denormalized 首位)。第二位負責人的收件人亦收。"""
    svc = NotificationService(ctx)
    second = await svc.create_recipient(
        name="second", email="second@x.com", assignee_name="Ben Yeh", actor=ADMIN,
    )
    # 兩位負責人開單(首位 Alice Fang、次位 Ben Yeh)
    await WorkOrderService(ctx).open_work_order(
        asset_id="EID-002", work_type="REACTIVE", actor=ADMIN,
        assignees=["Alice Fang", "Ben Yeh"], at=OPENED_AT,
    )
    rows = await _outbox(ctx)
    # 第二位負責人的收件人 row 存在(email 通道)
    assert (second.id, "email") in {(r.recipient_id, r.channel) for r in rows}


async def test_close_enqueues_closed(ctx) -> None:
    ids = await _seed_recipients(ctx)
    wo = await _open_wo(ctx)
    await WorkOrderService(ctx).finish_work_order(wo.work_order_no, ADMIN, at=OPENED_AT)
    closed = [r for r in await _outbox(ctx) if r.event == "closed"]
    # team 廣播 close(email+tg);owner 比對命中(email);other/inactive 無
    assert {(r.recipient_id, r.channel) for r in closed} == {
        (ids["team"], "email"), (ids["team"], "telegram"), (ids["owner"], "email"),
    }


async def test_unique_key_dedup(ctx) -> None:
    await _seed_recipients(ctx)
    wo = await _open_wo(ctx)
    before = len(await _outbox(ctx))
    # 重複 enqueue 同事件 → 唯一鍵去重(reopen→re-close 不重發的機制)
    async with WorkOrderService(ctx).write(ADMIN):
        wo_row = await ctx.get(_wo_models.WorkOrder, wo.work_order_no)
        added = await enqueue_work_order_notifications(ctx, wo_row, "opened")
    assert added == 0
    assert len(await _outbox(ctx)) == before


# ---- flush ----

async def test_flush_sends_both_channels(ctx) -> None:
    await _seed_recipients(ctx)
    await _open_wo(ctx)
    email, tg = InMemoryEmailSender(), InMemoryTelegramSender()
    svc = NotificationService(ctx, email_sender=email, telegram_sender=tg)
    result = await svc.flush_outbox(actor=Actor.scheduler())
    assert result == {"sent": 3, "failed": 0, "skipped_unconfigured": 0}
    assert len(email.sent) == 2 and len(tg.sent) == 1
    rows = await _outbox(ctx)
    assert all(r.status == "sent" and r.provider_msg_id for r in rows)
    # 內文抽查:主旨前綴 + 台北時間 + 連結 URL
    body = email.sent[0]["body"]
    assert email.sent[0]["subject"].startswith("【報修】EID-002 Aligner46")
    assert "2026-07-11 14:32" in body
    assert "/app/work-orders/" in body
    assert tg.sent[0]["text"].startswith("【報修】")  # telegram = 主旨首行


async def test_flush_failing_sender_marks_failed(ctx) -> None:
    await _seed_recipients(ctx)
    await _open_wo(ctx)
    svc = NotificationService(
        ctx, email_sender=InMemoryEmailSender(), telegram_sender=FailTelegram()
    )
    result = await svc.flush_outbox(actor=Actor.scheduler())
    assert result == {"sent": 2, "failed": 1, "skipped_unconfigured": 0}
    tg_row = next(r for r in await _outbox(ctx) if r.channel == "telegram")
    assert tg_row.status == "failed" and tg_row.attempts == 1 and tg_row.last_error


async def test_flush_unconfigured_channel_skips(ctx) -> None:
    await _seed_recipients(ctx)
    await _open_wo(ctx)
    # 不注入 sender + env 無 smtp / telegram → 兩通道皆未配置 → 整列略過(不改狀態、不燒 attempts)
    svc = NotificationService(ctx)
    result = await svc.flush_outbox(actor=Actor.scheduler())
    assert result == {"sent": 0, "failed": 0, "skipped_unconfigured": 3}
    rows = await _outbox(ctx)
    assert all(r.status == "pending" and r.attempts == 0 for r in rows)


# ---- CRUD admin-only + 驗證 ----

async def test_create_recipient_engineer_rejected(ctx) -> None:
    with pytest.raises(AuthorizationError):
        await NotificationService(ctx).create_recipient(
            name="x", email="x@x.com", actor=Actor.human("eng"),
        )


async def test_create_recipient_requires_a_channel(ctx) -> None:
    with pytest.raises(NotifyError):
        await NotificationService(ctx).create_recipient(name="x", actor=ADMIN)


async def test_create_recipient_bad_email(ctx) -> None:
    with pytest.raises(NotifyError):
        await NotificationService(ctx).create_recipient(
            name="x", email="not-an-email", actor=ADMIN
        )


async def test_update_and_toggle_roundtrip(ctx) -> None:
    ids = await _seed_recipients(ctx)
    svc = NotificationService(ctx)
    await svc.update_recipient(
        ids["owner"], name="owner2", email="owner2@x.com",
        assignee_name="Ben Yeh", notify_on_close=True, actor=ADMIN,
    )
    row = await ctx.get(NotifyRecipient, ids["owner"])
    assert row.name == "owner2" and row.assignee_name == "Ben Yeh" and row.notify_on_close
    await svc.set_recipient_active(ids["owner"], False, actor=ADMIN)
    assert (await ctx.get(NotifyRecipient, ids["owner"])).is_active is False


# ---- Slice D:關注名單(notify_watch;0032)----

async def _multi_owner_wo(s, assignees):
    return await WorkOrderService(s).open_work_order(
        asset_id="EID-002", work_type="REACTIVE", actor=ADMIN,
        assignees=assignees, at=OPENED_AT,
    )


async def test_watch_enqueues_on_open_and_close(ctx) -> None:
    """關注者(無廣播旗標、無自身 assignee_name)於開單 AND 結案皆收到通知。"""
    svc = NotificationService(ctx)
    watcher = await svc.create_recipient(
        name="max", email="max@x.com", watch_assignees=["Alice Fang"], actor=ADMIN,
    )
    wo = await _open_wo(ctx)  # assigned Alice Fang
    opened = {(r.recipient_id, r.channel, r.event) for r in await _outbox(ctx)}
    assert (watcher.id, "email", "opened") in opened
    await WorkOrderService(ctx).finish_work_order(wo.work_order_no, ADMIN, at=OPENED_AT)
    closed = {(r.recipient_id, r.channel, r.event) for r in await _outbox(ctx)}
    assert (watcher.id, "email", "closed") in closed


async def test_self_target_plus_watch_dedup_one_row(ctx) -> None:
    """同一收件人本人定向(assignee_name)+ 關注(watch)同時命中同一多負責人工單 → 每通道僅一列。"""
    svc = NotificationService(ctx)
    # assignee_name=Alice Fang(本人定向命中)+ 關注 Ben Yeh(關注臂命中),兩負責人同在工單。
    r = await svc.create_recipient(
        name="both", email="both@x.com", telegram_chat_id="-100777",
        assignee_name="Alice Fang", watch_assignees=["Ben Yeh"], actor=ADMIN,
    )
    await _multi_owner_wo(ctx, ["Alice Fang", "Ben Yeh"])
    rows = [ob for ob in await _outbox(ctx) if ob.recipient_id == r.id]
    # 兩臂命中但唯一鍵去重:email + telegram 各恰一列(共 2),非 4。
    assert sorted(ob.channel for ob in rows) == ["email", "telegram"]


async def test_two_watchers_overlap_each_once(ctx) -> None:
    """多負責人工單 + 兩位關注者(其一關注兩名皆在工單)→ 每位關注者各恰一列。"""
    svc = NotificationService(ctx)
    a = await svc.create_recipient(
        name="wa", email="wa@x.com", watch_assignees=["Alice Fang"], actor=ADMIN,
    )
    b = await svc.create_recipient(
        name="wb", email="wb@x.com",
        watch_assignees=["Ben Yeh", "Alice Fang"], actor=ADMIN,
    )
    await _multi_owner_wo(ctx, ["Alice Fang", "Ben Yeh"])
    rows = await _outbox(ctx)
    a_rows = [r for r in rows if r.recipient_id == a.id]
    b_rows = [r for r in rows if r.recipient_id == b.id]
    assert len(a_rows) == 1 and a_rows[0].channel == "email"
    # b 關注兩名皆在工單 → EXISTS 去重,仍僅一列。
    assert len(b_rows) == 1 and b_rows[0].channel == "email"


async def test_create_with_watches_and_list_carries_them(ctx) -> None:
    svc = NotificationService(ctx)
    r = await svc.create_recipient(
        name="sup", email="sup@x.com",
        watch_assignees=["Sam Wu", "Ben Yeh", "Sam Wu", " "], actor=ADMIN,
    )
    # 去重 + 去空(順序保留),存兩列。
    stored = sorted(
        w.assignee_name
        for w in (await ctx.scalars(
            select(NotifyWatch).where(NotifyWatch.recipient_id == r.id)
        )).all()
    )
    assert stored == ["Ben Yeh", "Sam Wu"]
    # list_recipients 透明附掛 .watches
    listed = {x.id: x for x in await svc.list_recipients()}
    assert sorted(listed[r.id].watches) == ["Ben Yeh", "Sam Wu"]


async def test_update_replaces_watches(ctx) -> None:
    svc = NotificationService(ctx)
    r = await svc.create_recipient(
        name="sup", email="sup@x.com", watch_assignees=["Sam Wu"], actor=ADMIN,
    )
    await svc.update_recipient(
        r.id, name="sup", email="sup@x.com",
        watch_assignees=["Cara Lo", "Ben Yeh"], actor=ADMIN,
    )
    stored = sorted(
        w.assignee_name
        for w in (await ctx.scalars(
            select(NotifyWatch).where(NotifyWatch.recipient_id == r.id)
        )).all()
    )
    assert stored == ["Ben Yeh", "Cara Lo"]  # 舊 Sam Wu 被整組取代
    # 空清單 = 清空
    await svc.update_recipient(
        r.id, name="sup", email="sup@x.com", watch_assignees=[], actor=ADMIN
    )
    assert (await ctx.scalars(
        select(NotifyWatch).where(NotifyWatch.recipient_id == r.id)
    )).all() == []


async def test_self_watch_silently_dropped(ctx) -> None:
    """關注自身 assignee_name = 本人定向已涵蓋,靜默丟棄(冗餘無害)。"""
    svc = NotificationService(ctx)
    r = await svc.create_recipient(
        name="owner", email="owner@x.com", assignee_name="Alice Fang",
        watch_assignees=["Alice Fang", "Ben Yeh"], actor=ADMIN,
    )
    stored = sorted(
        w.assignee_name
        for w in (await ctx.scalars(
            select(NotifyWatch).where(NotifyWatch.recipient_id == r.id)
        )).all()
    )
    assert stored == ["Ben Yeh"]  # 自身 Alice Fang 被丟棄
