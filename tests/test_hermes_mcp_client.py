"""`open_mcp_tools` 真連線測試:本機起一個最小 MCP streamable HTTP server,真的握手一次。

背景:單元測試以 `tools_opener` seam 取代 `open_mcp_tools`,沒抓到 mcp 1.x / 2.x 的
`streamable_http_client` yield 個數差異(線上 502)。這裡不 mock transport,實際走
httpx → MCP initialize → list_tools → `async_mcp_tool` 轉換。

- server:`FastMCP` 一個 `ping` 工具,uvicorn 在背景 thread 跑在 127.0.0.1 的隨機空埠
  (預先 bind 的 socket,免搶埠),外包一層純 ASGI middleware 記下收到的 header。
- 不打 Anthropic API、不依賴外部網路、不需要 Docker。
"""

from __future__ import annotations

import contextlib
import importlib.util
import pathlib
import socket
import sys
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP

_APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "infra" / "hermes" / "app"


def _load(name: str, key: str):
    """以檔案路徑載入 gateway 同目錄模組;已載入(同鍵)就重用,與其他 hermes 測試共用。"""
    module = sys.modules.get(key)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(key, _APP_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


llm = _load("llm_anthropic", "hermes_llm_anthropic")

TOKEN = "tok-123"


class _HeaderRecorder:
    """純 ASGI middleware:記下每個 HTTP 請求的 Authorization / X-Board-Turn(不改請求)。"""

    def __init__(self, app):
        self.app = app
        self.seen: list[dict[str, str | None]] = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                       for k, v in scope["headers"]}
            self.seen.append({
                "authorization": headers.get("authorization"),
                "x-board-turn": headers.get("x-board-turn"),
            })
        await self.app(scope, receive, send)


def _build_app() -> _HeaderRecorder:
    mcp = FastMCP("hermes-mcp-client-test")

    @mcp.tool()
    def ping(x: int) -> int:
        """回傳 x + 1。"""
        return x + 1

    # streamable_http_app 的 lifespan 會跑 session_manager.run();uvicorn lifespan=on 觸發它。
    return _HeaderRecorder(mcp.streamable_http_app())


@pytest.fixture(scope="module")
def mcp_server():
    """背景 thread 起 uvicorn;回 (port, recorder)。測試結束 should_exit 收掉。"""
    recorder = _build_app()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(recorder, log_level="warning", lifespan="on", ws="none")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            pytest.fail("MCP test server did not start")
        time.sleep(0.02)
    try:
        yield port, recorder
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


async def test_open_mcp_tools_real_handshake_with_extra_headers(mcp_server):
    port, recorder = mcp_server
    recorder.seen.clear()
    async with llm.open_mcp_tools(
        f"http://127.0.0.1:{port}/mcp", TOKEN, extra_headers={"X-Board-Turn": "t1"}
    ) as tools:
        assert len(tools) == 1
        assert tools[0].name == "ping"
    assert recorder.seen, "server saw no requests"
    # 每個請求(initialize / list_tools / 關 session)都帶同一組 header
    assert all(h["authorization"] == f"Bearer {TOKEN}" for h in recorder.seen)
    assert all(h["x-board-turn"] == "t1" for h in recorder.seen)


async def test_open_mcp_tools_real_handshake_without_extra_headers(mcp_server):
    port, recorder = mcp_server
    recorder.seen.clear()
    async with llm.open_mcp_tools(f"http://127.0.0.1:{port}/mcp", TOKEN) as tools:
        assert [t.name for t in tools] == ["ping"]
    assert recorder.seen
    assert all(h["authorization"] == f"Bearer {TOKEN}" for h in recorder.seen)
    assert all(h["x-board-turn"] is None for h in recorder.seen)


async def test_open_mcp_tools_accepts_two_value_transport(mcp_server, monkeypatch):
    """模擬 mcp 2.x:transport 只 yield (read, write);仍走真連線(線上 502 的回歸防線)。"""
    from mcp.client import streamable_http

    real = streamable_http.streamable_http_client

    @contextlib.asynccontextmanager
    async def two_value_client(*args, **kwargs):
        async with real(*args, **kwargs) as transport:
            yield transport[0], transport[1]

    monkeypatch.setattr(streamable_http, "streamable_http_client", two_value_client)
    port, _ = mcp_server
    async with llm.open_mcp_tools(f"http://127.0.0.1:{port}/mcp", TOKEN) as tools:
        assert [t.name for t in tools] == ["ping"]
