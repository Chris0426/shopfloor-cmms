"""MCP `list_work_orders` 工具邊界的 status 正規化(純單元測試,mock service,免 Docker)。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from cmms.mcp import server


@pytest.fixture
def captured(monkeypatch):
    calls: list[dict] = []

    @asynccontextmanager
    async def _fake_session():
        yield object()

    class _FakeService:
        def __init__(self, session):
            pass

        async def list_work_orders(self, **kwargs):
            calls.append(kwargs)
            return []

    monkeypatch.setattr(server, "get_sessionmaker", lambda: _fake_session)
    monkeypatch.setattr(server, "WorkOrderService", _FakeService)
    return calls


@pytest.mark.parametrize(
    ("given", "expected"),
    [("open", "OPEN"), (" in_progress ", "IN_PROGRESS"), ("OPEN", "OPEN"),
     ("  ", None), ("", None), (None, None)],
)
def test_status_normalized(captured, given, expected):
    asyncio.run(server.list_work_orders(status=given))
    assert captured[0]["status"] == expected


def test_active_sends_statuses_group(captured):
    asyncio.run(server.list_work_orders(active=True))
    assert captured[0]["statuses"] == ["OPEN", "IN_PROGRESS", "ON_HOLD"]
    assert captured[0]["status"] is None


def test_status_wins_over_active(captured):
    asyncio.run(server.list_work_orders(active=True, status="completed"))
    assert captured[0]["status"] == "COMPLETED"
    assert captured[0]["statuses"] is None


def test_default_sends_neither(captured):
    asyncio.run(server.list_work_orders())
    assert captured[0]["status"] is None
    assert captured[0]["statuses"] is None
