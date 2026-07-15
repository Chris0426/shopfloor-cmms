"""MCP scoped token DB 測試(ADR-020 決策 5;testcontainers)。無 Docker 自動 skip。

驗:從有效 session mint → resolve 得 (user_id, scope);無效 session 不能 mint;bogus/None/
過期/撤銷 → resolve None。identity 無跨切片 FK → create_all 只建 identity。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import update  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.models import McpScopedToken  # noqa: E402
from cmms.domain.identity.service import IdentityError, IdentityService  # noqa: E402


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await IdentityService(s).create_user(
                user_id="bob", username="bob", display_name="Bob", password="password8",
                org="plant", actor=Actor.human("cli"),
            )
            yield s
        await engine.dispose()


async def test_mint_and_resolve(session) -> None:
    svc = IdentityService(session)
    _uid, stok = await svc.authenticate("bob", "password8")
    token = await svc.mint_scoped_token(session_token=stok, agent="agent:hermes", scope="wo:link")
    assert await svc.resolve_scoped_token(token) == ("bob", "wo:link")


async def test_invalid_session_cannot_mint(session) -> None:
    svc = IdentityService(session)
    with pytest.raises(IdentityError):
        await svc.mint_scoped_token(session_token="bogus", agent="agent:hermes", scope="x")


async def test_bogus_expired_revoked_resolve_none(session) -> None:
    svc = IdentityService(session)
    _uid, stok = await svc.authenticate("bob", "password8")
    assert await svc.resolve_scoped_token("nope") is None
    assert await svc.resolve_scoped_token(None) is None

    # 過期:以過去時間 mint → expires_at 已過
    expired = await svc.mint_scoped_token(
        session_token=stok, agent="a", scope="x", at=datetime.now(UTC) - timedelta(hours=1)
    )
    assert await svc.resolve_scoped_token(expired) is None

    # 撤銷:設 revoked_at → 即時失效
    tok = await svc.mint_scoped_token(session_token=stok, agent="a", scope="x")
    await session.execute(
        update(McpScopedToken)
        .where(McpScopedToken.token == tok)
        .values(revoked_at=datetime.now(UTC))
    )
    await session.commit()
    assert await svc.resolve_scoped_token(tok) is None
