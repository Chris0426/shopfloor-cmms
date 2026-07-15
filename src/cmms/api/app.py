"""FastAPI 應用入口。

各切片以 router 掛入(`/assets`、`/work-orders`…),router 內只呼叫 domain service,
不直接碰 DB(ADR-001)。另掛 `/mcp` = cmms MCP server 的 Streamable HTTP transport
(agent 試點;per-user scoped token 閘門見 api/auth.py)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.fastmcp.server import StreamableHTTPASGIApp
from starlette.routing import Route

from cmms import __version__
from cmms.api.auth import register_mcp_auth, register_read_api_auth
from cmms.api.routes import (
    assets,
    attachments,
    contacts,
    inventory,
    pm_schedule,
    tasks,
    vocab,
    work_orders,
)
from cmms.mcp.server import mcp as mcp_server
from cmms.web import admin_routes, telegram_webhook
from cmms.web import routes as web_routes


class _McpTransportProxy:
    """`/mcp` 的 ASGI 轉接層:lifespan 每次啟動換上**新的** streamable-http session manager。

    SDK(mcp 1.28)的 `StreamableHTTPSessionManager` 一個 instance 只能 `run()` 一次;
    若把 manager 直接綁死在 route 上,第二次 lifespan(如測試多次進出 TestClient)必炸。
    proxy + per-lifespan 重建解耦。lifespan 未運行時(不該發生於 uvicorn 正常啟動)回 503。
    """

    def __init__(self) -> None:
        self.app: StreamableHTTPASGIApp | None = None

    async def __call__(self, scope, receive, send) -> None:  # ASGI 介面
        if self.app is None:
            await JSONResponse(
                {"detail": "mcp transport not running"}, status_code=503
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


_mcp_transport = _McpTransportProxy()


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """streamable-http session manager 必須在 lifespan 的 task group 內運行(SDK 要求;
    stateless 模式亦然)。每次啟動重建 manager(見 _McpTransportProxy docstring)。"""
    mcp_server._session_manager = None  # 丟棄上輪 manager(run() once-per-instance)
    mcp_server.streamable_http_app()  # 官方曝露的惰性初始化路徑(帶 FastMCP settings)
    _mcp_transport.app = StreamableHTTPASGIApp(mcp_server.session_manager)
    try:
        async with mcp_server.session_manager.run():
            yield
    finally:
        _mcp_transport.app = None


app = FastAPI(title="cmms", version=__version__, lifespan=_lifespan)


@app.middleware("http")
async def htmx_login_redirect(request, call_next):
    """HTMX 局部請求撞上「轉登入頁」時,改回 HX-Redirect 標頭做整頁跳轉。

    否則 htmx 會跟隨 307 把**整個登入頁**(含側欄/dock)swap 進結果 fragment,
    產生巢狀破版(review f14cf8d)。一般導航不受影響。
    """
    response = await call_next(request)
    if (
        request.headers.get("hx-request") == "true"
        and response.status_code in (303, 307)
        and response.headers.get("location", "").startswith("/app/login")
    ):
        response.status_code = 204  # htmx 不 swap、只跳轉
        response.headers["HX-Redirect"] = "/app/login"
        del response.headers["location"]
    return response


# 讀取類 JSON API 的 static bearer token 保護(峰會裁決 消費端需求)。★ 在 htmx middleware **之後**
# 註冊 → Starlette「後註冊先執行」→ auth check 為最外層,先擋未授權請求(見 api/auth.py)。
register_read_api_auth(app)
# /mcp 的 per-user MCP scoped token 閘門(agent 試點;static bearer 豁免 /mcp、此閘門只管 /mcp)
register_mcp_auth(app)


@app.get("/health")
async def health() -> dict[str, str]:
    """存活探針(供 Fly health check)。"""
    return {"status": "ok", "version": __version__}


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    """裸網址 → 工程師操作台(/app 再依登入態轉 login / 佇列)。避免根路徑回 404。"""
    return RedirectResponse(url="/app", status_code=307)


app.include_router(assets.router)
app.include_router(tasks.router)
app.include_router(pm_schedule.router)
app.include_router(work_orders.router)
app.include_router(inventory.router)
app.include_router(contacts.router)
app.include_router(attachments.router)
app.include_router(vocab.router)  # C2 失效詞彙唯讀 lookup(分析平台消費 contract_failure_vocab.v1)

# 工程師操作台 web UI(ADR-019):靜態資源 + /app 路由(伺服器渲染 Jinja2 + HTMX)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "web" / "static")),
    name="static",
)
app.include_router(web_routes.router)
app.include_router(admin_routes.router)  # /admin 管理台(ADR-019 雙表面 + ADR-022 RBAC)
# Telegram 助理 webhook(續-15;/telegram/webhook 有自己的 secret header 閘門,見 api/auth.py 豁免)
app.include_router(telegram_webhook.router)

# cmms MCP server 掛 /mcp(Streamable HTTP,agent 試點)。用 Starlette `Route` **精確路徑**
# 掛 ASGI handler(SDK 官方 pattern)而非 `Mount` —— Mount 會對 /mcp 發 307 → /mcp/,
# 留 redirect 縫;Route 精確服務 /mcp。閘門(register_mcp_auth)覆蓋 /mcp + /mcp/* 全 method。
app.router.routes.append(
    Route("/mcp", endpoint=_mcp_transport, methods=["GET", "POST", "DELETE"])
)
