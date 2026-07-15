"""IdentityService DB 整合測試(ADR-022;testcontainers-postgres)。本機無 Docker 自動 skip。

驗:建帳號 + argon2 認證 + session 解析 + 撤銷(登出)+ 停用擋登入 + per-user locale 寫回。
identity 無跨切片 FK(user_session→user_account only)→ create_all 只建這兩表、自足。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.models import UserAccount  # noqa: E402
from cmms.domain.identity.service import (  # noqa: E402
    AuthenticationError,
    IdentityError,
    IdentityService,
)

ADMIN = Actor.human("admin1")


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s
        await engine.dispose()


async def _mk(svc: IdentityService, **over: str) -> str:
    kw = {
        "user_id": "jlee",
        "username": "jlee",
        "display_name": "陳工",
        "password": "correct horse",
        "org": "plant",
        "role": "engineer",
        "actor": ADMIN,
    }
    kw.update(over)
    return await svc.create_user(**kw)


async def test_create_authenticate_resolve(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, role="admin")

    # 正確帳密 → 建 session
    user_id, token = await svc.authenticate("jlee", "correct horse")
    assert user_id == "jlee"
    assert token

    # token → 使用者(未過期 / 未撤)
    user = await svc.resolve_user(token)
    assert user is not None
    assert user.user_id == "jlee"
    assert user.role == "admin"
    assert user.ui_locale == "en"          # 預設 en(ADR-023)

    # 密碼永不明文落庫
    assert user.password_hash != "correct horse"
    assert user.password_hash.startswith("$argon2")

    # 錯密碼 / 不存在帳號 → 同一種錯誤(不區分,避免枚舉)
    with pytest.raises(AuthenticationError):
        await svc.authenticate("jlee", "wrong")
    with pytest.raises(AuthenticationError):
        await svc.authenticate("nobody", "whatever")

    # 無效 token → None
    assert await svc.resolve_user("bogus-token") is None
    assert await svc.resolve_user(None) is None


async def test_logout_revokes_and_set_locale(session) -> None:
    svc = IdentityService(session)
    await _mk(svc)

    _uid, token = await svc.authenticate("jlee", "correct horse")
    assert await svc.resolve_user(token) is not None

    # 登出 = 即時撤銷 → resolve 回 None
    await svc.logout(token)
    assert await svc.resolve_user(token) is None
    await svc.logout(token)  # 冪等:重複登出不炸

    # per-user locale 寫回 user_account(ADR-023)
    await svc.set_locale("jlee", actor=ADMIN, ui_locale="zh-TW", jira_output_locale="vi")
    user = await session.get(UserAccount, "jlee")
    await session.refresh(user)
    assert user.ui_locale == "zh-TW"
    assert user.jira_output_locale == "vi"


async def test_set_emaint_assignee(session) -> None:
    svc = IdentityService(session)
    await _mk(svc)  # 建立時無 emaint_assignee
    user = await session.get(UserAccount, "jlee")
    assert user.emaint_assignee is None

    # 設定(前後空白 strip)
    await svc.set_emaint_assignee("jlee", assignee="  Jordan Lee  ", actor=ADMIN)
    await session.refresh(user)
    assert user.emaint_assignee == "Jordan Lee"

    # 空字串 → 清除(回監督者語意)
    await svc.set_emaint_assignee("jlee", assignee="", actor=ADMIN)
    await session.refresh(user)
    assert user.emaint_assignee is None

    # 不存在的 user → raise
    with pytest.raises(IdentityError):
        await svc.set_emaint_assignee("nobody", assignee="X", actor=ADMIN)


async def test_inactive_user_cannot_authenticate(session) -> None:
    svc = IdentityService(session)
    await _mk(svc)
    user = await session.get(UserAccount, "jlee")
    user.is_active = False
    await session.commit()

    with pytest.raises(AuthenticationError):
        await svc.authenticate("jlee", "correct horse")
