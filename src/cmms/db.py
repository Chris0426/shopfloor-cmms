"""資料庫底層:async engine、session factory、ORM Base。

唯一寫入路徑(ADR-001)在 domain service;這裡只提供連線與宣告式 Base,
thin client 不得繞過 domain service 直接用 session 寫入。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from cmms.config import get_settings


class Base(DeclarativeBase):
    """所有 ORM model 的宣告式 Base;Alembic 以 `Base.metadata` 自動產 migration。"""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, echo=settings.db_echo)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def session_scope() -> AsyncIterator[AsyncSession]:
    """提供一個 session(交易由 domain service 控制 commit/rollback)。"""
    async with get_sessionmaker()() as session:
        yield session
