"""管理台身分操作 DB 整合測試(ADR-022 決策 4;testcontainers-postgres)。本機無 Docker 自動 skip。

驗:list_users / deactivate(即時擋登入)/ set_role(驗證 + 守門)/ reset_password(舊失效新可登
+ 撤 session)/ change_password(驗舊 + 本人)/ _assert_admin(engineer 拒 admin 過)/ 自我 &
末位 admin 守門 / 稽核。identity 無跨切片 FK → create_all 只建 user_account + user_session。
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
    AuthorizationError,
    IdentityError,
    IdentityService,
)


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


async def _mk(svc: IdentityService, uid: str, *, role: str = "engineer", pw: str = "correct horse"):
    return await svc.create_user(
        user_id=uid, username=uid, display_name=uid.title(), password=pw,
        org="plant", role=role, actor=Actor.human("cli"),
    )


async def test_list_users_ordered(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "zoe")
    await _mk(svc, "adam", role="admin")
    users = await svc.list_users()
    assert [u.username for u in users] == ["adam", "zoe"]  # 依 username


async def test_deactivate_blocks_login_immediately(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "adam", role="admin")
    await _mk(svc, "bob")
    admin = Actor.human("adam")
    _uid, token = await svc.authenticate("bob", "correct horse")
    assert await svc.resolve_user(token) is not None

    await svc.deactivate_user("bob", actor=admin)
    assert await svc.resolve_user(token) is None            # is_active=False → 即時擋
    with pytest.raises(AuthenticationError):
        await svc.authenticate("bob", "correct horse")
    bob = await session.get(UserAccount, "bob")
    assert bob.is_active is False and bob.updated_by == "human:adam"  # 稽核


async def test_set_role_validates_and_persists(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "adam", role="admin")
    await _mk(svc, "bob")
    admin = Actor.human("adam")
    await svc.set_role("bob", "admin", actor=admin)
    assert (await session.get(UserAccount, "bob")).role == "admin"
    with pytest.raises(IdentityError):
        await svc.set_role("bob", "superuser", actor=admin)  # 非法角色


async def test_create_and_set_operator_role(session) -> None:
    """operator 角色(iPad 產線共用帳號)= 合法 role:create_user 建帳 + set_role 改派皆通過。"""
    svc = IdentityService(session)
    await _mk(svc, "adam", role="admin")
    # create_user 直接建 operator
    await _mk(svc, "ipad1", role="operator")
    assert (await session.get(UserAccount, "ipad1")).role == "operator"
    # 既有 engineer 帳號可由 admin 改派為 operator(部署後 Jordan 改三個 iPad 帳號用)
    await _mk(svc, "ipad2")  # engineer
    await svc.set_role("ipad2", "operator", actor=Actor.human("adam"))
    assert (await session.get(UserAccount, "ipad2")).role == "operator"


async def test_reset_password_old_fails_new_works_and_revokes(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "adam", role="admin")
    await _mk(svc, "bob")
    admin = Actor.human("adam")
    _uid, token = await svc.authenticate("bob", "correct horse")

    await svc.reset_password("bob", "brand new pw", actor=admin)
    assert await svc.resolve_user(token) is None            # reset 撤該用戶 session
    with pytest.raises(AuthenticationError):
        await svc.authenticate("bob", "correct horse")      # 舊密碼失效
    uid2, _ = await svc.authenticate("bob", "brand new pw")  # 新密碼可登
    assert uid2 == "bob"
    with pytest.raises(IdentityError):
        await svc.reset_password("bob", "short", actor=admin)  # < 8


async def test_change_password_self_service(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "bob")
    bob = Actor.human("bob")
    with pytest.raises(AuthenticationError):
        await svc.change_password("bob", "wrong old", "new longpw", actor=bob)  # 舊錯
    await svc.change_password("bob", "correct horse", "new longpw", actor=bob)
    uid, _ = await svc.authenticate("bob", "new longpw")
    assert uid == "bob"
    # 只能改自己
    with pytest.raises(AuthorizationError):
        await svc.change_password("bob", "new longpw", "another pw", actor=Actor.human("adam"))


async def test_assert_admin_enforced(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "adam", role="admin")
    await _mk(svc, "bob")
    await _mk(svc, "carl")
    # engineer actor 不能做 admin 操作
    with pytest.raises(AuthorizationError):
        await svc.deactivate_user("carl", actor=Actor.human("bob"))
    # admin actor 可以
    await svc.deactivate_user("carl", actor=Actor.human("adam"))
    assert (await session.get(UserAccount, "carl")).is_active is False


async def test_self_and_last_admin_guards(session) -> None:
    svc = IdentityService(session)
    await _mk(svc, "adam", role="admin")
    adam = Actor.human("adam")
    # 停自己
    with pytest.raises(IdentityError):
        await svc.deactivate_user("adam", actor=adam)
    # 降末位 admin(adam 自己)
    with pytest.raises(IdentityError):
        await svc.set_role("adam", "engineer", actor=adam)
    # 加第二個 admin 後,才能降原 admin(非自己)
    await _mk(svc, "eve", role="admin")
    await svc.set_role("adam", "engineer", actor=Actor.human("eve"))
    assert (await session.get(UserAccount, "adam")).role == "engineer"
