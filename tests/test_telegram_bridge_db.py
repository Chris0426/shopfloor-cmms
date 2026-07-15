"""telegram_bridge DB 測試(續-15;testcontainers)。無 Docker 自動 skip。

覆蓋:
- create→redeem 快樂路徑(link 建立、code 標 used)。
- 過期 / 已用 / 亂碼碼皆拒(同一 "invalid or expired code" 訊息)。
- 重新 create 作廢舊碼(舊碼兌換失敗)。
- chat_id 已綁他人 → 拒;同一 user 重綁新 chat_id → REPLACE。
- resolve_user_by_chat(active 回 user、inactive 回 None、未綁 None)。
- unlink 冪等;mark_update_seen 首次 True / 重送 False。
- NotificationService.fill_telegram_chat_id(空欄填 True / 有值不覆蓋 / 查無 / None → False)。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from datetime import UTC, datetime, timedelta  # noqa: E402

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402

# 匯入全部 model 模組,讓 Base.metadata 收錄(FK 目標表齊全才能 create_all)。
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.assistant import models as _assistant_models  # noqa: E402, F401
from cmms.domain.attachment import models as _attachment_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.failure_vocab import models as _failure_vocab_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.models import UserAccount  # noqa: E402
from cmms.domain.identity.service import IdentityService  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.notify import models as _notify_models  # noqa: E402, F401
from cmms.domain.notify.models import NotifyRecipient  # noqa: E402
from cmms.domain.notify.service import NotificationService  # noqa: E402
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.procurement import models as _proc_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.telegram_bridge import models as _tg_models  # noqa: E402, F401
from cmms.domain.telegram_bridge.models import TelegramLink, TelegramLinkCode  # noqa: E402
from cmms.domain.telegram_bridge.service import (  # noqa: E402
    TelegramBridgeError,
    TelegramBridgeService,
    hash_code,
)
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401

ADMIN = Actor.human("admin")
A_ALICE = Actor.human("alice")
A_BOB = Actor.human("bob")


@pytest.fixture
async def ctx():
    """PG 容器 + 種子帳號(admin / alice / bob,皆 engineer|admin、active)。"""
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            ident = IdentityService(s)
            await ident.create_user(
                user_id="admin", username="admin", display_name="Admin",
                password="password8", org="plant", role="admin", actor=Actor.human("cli"),
            )
            await ident.create_user(
                user_id="alice", username="alice", display_name="Alice",
                password="password8", org="plant", role="engineer", actor=Actor.human("cli"),
            )
            await ident.create_user(
                user_id="bob", username="bob", display_name="Bob",
                password="password8", org="plant", role="engineer", actor=Actor.human("cli"),
            )
            yield s
        await engine.dispose()


# ---- create / redeem ----

async def test_create_and_redeem_happy(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    link = await svc.redeem_code(code, "111", A_ALICE)
    assert link.user_id == "alice" and link.chat_id == "111"
    # 碼標記已用
    row = await ctx.get(TelegramLinkCode, hash_code(code))
    assert row.used_at is not None


async def test_create_link_code_rejects_inactive_user(ctx) -> None:
    alice = await ctx.get(UserAccount, "alice")
    alice.is_active = False
    await ctx.commit()
    with pytest.raises(TelegramBridgeError):
        await TelegramBridgeService(ctx).create_link_code("alice", A_ALICE)


async def test_expired_code_rejected(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    row = await ctx.get(TelegramLinkCode, hash_code(code))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await ctx.commit()
    with pytest.raises(TelegramBridgeError, match="invalid or expired code"):
        await svc.redeem_code(code, "111", A_ALICE)


async def test_used_code_rejected(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    await svc.redeem_code(code, "111", A_ALICE)
    with pytest.raises(TelegramBridgeError, match="invalid or expired code"):
        await svc.redeem_code(code, "111", A_ALICE)


async def test_garbage_code_rejected(ctx) -> None:
    with pytest.raises(TelegramBridgeError, match="invalid or expired code"):
        await TelegramBridgeService(ctx).redeem_code("not-a-real-code", "111", A_ALICE)


async def test_recreate_invalidates_old_code(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code1 = await svc.create_link_code("alice", A_ALICE)
    code2 = await svc.create_link_code("alice", A_ALICE)
    # 舊碼已被作廢(刪除)→ 兌換失敗
    with pytest.raises(TelegramBridgeError, match="invalid or expired code"):
        await svc.redeem_code(code1, "111", A_ALICE)
    # 新碼仍可用
    link = await svc.redeem_code(code2, "111", A_ALICE)
    assert link.chat_id == "111"


async def test_chat_linked_to_other_rejected(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code_a = await svc.create_link_code("alice", A_ALICE)
    await svc.redeem_code(code_a, "111", A_ALICE)
    code_b = await svc.create_link_code("bob", A_BOB)
    with pytest.raises(TelegramBridgeError, match="another account"):
        await svc.redeem_code(code_b, "111", A_BOB)


async def test_same_user_rebinds_new_chat(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code1 = await svc.create_link_code("alice", A_ALICE)
    await svc.redeem_code(code1, "111", A_ALICE)
    code2 = await svc.create_link_code("alice", A_ALICE)
    link = await svc.redeem_code(code2, "222", A_ALICE)
    assert link.chat_id == "222"
    # alice 只有一列 link(REPLACE 非新增)
    rows = (
        await ctx.scalars(select(TelegramLink).where(TelegramLink.user_id == "alice"))
    ).all()
    assert len(rows) == 1 and rows[0].chat_id == "222"


async def test_redeem_blank_chat_id_rejected(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    with pytest.raises(TelegramBridgeError, match="chat_id is required"):
        await svc.redeem_code(code, "   ", A_ALICE)


# ---- resolve / unlink ----

async def test_resolve_user_by_chat(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    await svc.redeem_code(code, "111", A_ALICE)
    u = await svc.resolve_user_by_chat("111")
    assert u is not None and u.user_id == "alice"
    # 未綁 chat
    assert await svc.resolve_user_by_chat("999") is None
    # 綁定後帳號停用 → None
    alice = await ctx.get(UserAccount, "alice")
    alice.is_active = False
    await ctx.commit()
    assert await svc.resolve_user_by_chat("111") is None


async def test_unlink_idempotent(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    await svc.redeem_code(code, "111", A_ALICE)
    assert await svc.unlink("alice", A_ALICE) is True
    assert await svc.unlink("alice", A_ALICE) is False


# ---- read helpers(get_link / peek_code_user)----

async def test_get_link(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    assert await svc.get_link("alice") is None            # 未綁 → None
    code = await svc.create_link_code("alice", A_ALICE)
    await svc.redeem_code(code, "111", A_ALICE)
    link = await svc.get_link("alice")
    assert link is not None and link.chat_id == "111"


async def test_peek_code_user(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    assert await svc.peek_code_user(code) == "alice"      # 有效碼 → user_id
    assert await svc.peek_code_user("garbage") is None    # 亂碼 → None
    # peek 不標已用 → 碼仍可兌換
    row = await ctx.get(TelegramLinkCode, hash_code(code))
    assert row.used_at is None
    link = await svc.redeem_code(code, "111", A_ALICE)
    assert link.chat_id == "111"
    # 已用 → peek 回 None
    assert await svc.peek_code_user(code) is None


async def test_peek_code_user_expired(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    code = await svc.create_link_code("alice", A_ALICE)
    row = await ctx.get(TelegramLinkCode, hash_code(code))
    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await ctx.commit()
    assert await svc.peek_code_user(code) is None         # 過期 → None


# ---- webhook 冪等 ----

async def test_mark_update_seen(ctx) -> None:
    svc = TelegramBridgeService(ctx)
    assert await svc.mark_update_seen(42) is True
    assert await svc.mark_update_seen(42) is False  # 重送
    assert await svc.mark_update_seen(43) is True


# ---- NotificationService.fill_telegram_chat_id ----

async def test_fill_telegram_chat_id(ctx) -> None:
    nsvc = NotificationService(ctx)
    rec = await nsvc.create_recipient(
        name="alice-owner", email="a@x.com", assignee_name="Alice Wu", actor=ADMIN,
    )
    # 空欄 → 填入 True
    assert await nsvc.fill_telegram_chat_id(
        assignee_name="Alice Wu", chat_id="111", actor=A_ALICE
    ) is True
    assert (await ctx.get(NotifyRecipient, rec.id)).telegram_chat_id == "111"
    # 已有值 → 不覆蓋 False
    assert await nsvc.fill_telegram_chat_id(
        assignee_name="Alice Wu", chat_id="222", actor=A_ALICE
    ) is False
    assert (await ctx.get(NotifyRecipient, rec.id)).telegram_chat_id == "111"
    # assignee_name 查無 → False
    assert await nsvc.fill_telegram_chat_id(
        assignee_name="Ghost", chat_id="333", actor=A_ALICE
    ) is False
    # None → False(不查)
    assert await nsvc.fill_telegram_chat_id(
        assignee_name=None, chat_id="333", actor=A_ALICE
    ) is False
