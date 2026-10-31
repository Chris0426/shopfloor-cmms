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
