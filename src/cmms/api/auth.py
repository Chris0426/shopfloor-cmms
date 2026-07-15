"""讀取類 JSON API 的 static bearer token 保護(峰會裁決 消費端需求 / architect review Critical #1)。

服務間讀取(Analytics 等下游 consumer)= HTTP JSON + static bearer token
(`CMMS_READ_API_TOKEN`,Fly secret)。此 middleware 攔所有**非豁免**路徑;缺 / 錯 token → 401。
公網零驗證的 `/work-orders`、`/assets`、`/inventory-items`、`/organizations`、`/persons`
(單筆 PII 可列舉)、`/attachments`(presigned R2 url)等一律關進 token 後面。

## 豁免路徑(不要求 token)
- `/health` —— Fly 存活探針。
- `/`(精確)—— 裸網址轉址 /app。
- `/app`、`/app/*` —— web UI 有自己的 server-side session 登入(ADR-022)。
- `/admin`、`/admin/*` —— 同上,RBAC gated。
- `/static`、`/static/*` —— 前端靜態資源。
- `/work-orders/on-box/*` —— ADR-017 Profile B,自帶 Ed25519 JWS 驗證,不疊 bearer。
- `/mcp`、`/mcp/*` —— MCP 有自己的 **per-user scoped token 閘門**(本檔
  `mcp_scoped_token_middleware`,查 DB、可即時撤),比 static token 更強;不疊 static bearer。
- `/telegram/webhook` —— Telegram 助理 webhook(續-15),有自己的 secret header 閘門
  (`X-Telegram-Bot-Api-Secret-Token` 常數時間比較 + fail-closed 503),不疊 static bearer。

## 其餘一律要求 `Authorization: Bearer <token>`
- token 以 `secrets.compare_digest` 常數時間比較(避 timing oracle)。
- 失敗 → 401 `{"detail": "unauthorized"}` + `WWW-Authenticate: Bearer`。
- **含 `/openapi.json` `/docs` `/redoc`**:schema 揭露 PII 端點形狀,下游對版日用 token 拉。

## fail-closed(失敗模式 FP-3)
token 未設 **且** `app_env == "production"` → 受保護端點一律 **503**
`{"detail": "read API token not configured"}`;絕不因忘設 secret 而靜默重新裸奔。
token 未設且非 production(local / CI)→ 放行(既有測試 / 本機開發不受影響)。

## 註冊順序
`register_read_api_auth(app)` 在 app.py 於 `htmx_login_redirect` **之後**註冊 →
Starlette「後註冊先執行」→ 本 auth check 為最外層,先於一切其他處理擋下未授權請求。
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from cmms.config import get_settings

# 精確匹配即豁免的路徑(/mcp 有自己的 per-user token 閘門;/telegram/webhook 有 secret header 閘門)
_EXEMPT_EXACT = frozenset(
    {"/health", "/", "/app", "/admin", "/static", "/mcp", "/telegram/webhook"}
)
# 前綴匹配即豁免的路徑(注意帶尾斜線,避免 /apple 誤命中 /app)
_EXEMPT_PREFIXES = ("/app/", "/admin/", "/static/", "/work-orders/on-box/", "/mcp/")


def _is_exempt(path: str) -> bool:
    if path in _EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _EXEMPT_PREFIXES)


def _extract_bearer(auth_header: str) -> str | None:
    """從 `Authorization` 標頭取 bearer token;非 Bearer scheme → None。"""
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


async def read_api_bearer_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """對非豁免路徑強制 static bearer token(見模組 docstring)。"""
    if _is_exempt(request.url.path):
        return await call_next(request)

    settings = get_settings()
    token = settings.read_api_token
    if not token:
        # fail-closed:正式環境缺 token → 503(絕不裸奔);本機 / CI → 放行
        if settings.app_env == "production":
            return JSONResponse(
                {"detail": "read API token not configured"}, status_code=503
            )
        return await call_next(request)

    provided = _extract_bearer(request.headers.get("authorization", ""))
    if provided is None or not secrets.compare_digest(provided, token):
        return JSONResponse(
            {"detail": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


def register_read_api_auth(app: FastAPI) -> None:
    """把 bearer auth 掛成 http middleware。**在 htmx middleware 之後呼叫**(見模組 docstring)。"""
    app.middleware("http")(read_api_bearer_middleware)


# ---- /mcp per-user MCP scoped token 閘門(agent 試點;ADR-020 決策 5)----
#
# transport 層驗證:`Authorization: Bearer <mcp_scoped_token>` → IdentityService
# .resolve_scoped_token(查 DB:存在 / 未過期 / 未撤 / 帳號 active)。無效 → 401 +
# `WWW-Authenticate: Bearer`。驗過的 (user_id, scope) 存 per-request contextvar
# (`identity.service.mcp_transport_identity`),委派寫入工具在 `scoped_token` 參數缺省時
# fallback 至此。**無 fail-open 路徑**:任何環境(含 local/CI)都要求有效 token。
# 路徑覆蓋 `/mcp` 精確 + `/mcp/*` 前綴、全部 method(GET/POST/DELETE 與 307 目標),無繞縫。

_MCP_EXACT = "/mcp"
_MCP_PREFIX = "/mcp/"


def _is_mcp_path(path: str) -> bool:
    return path == _MCP_EXACT or path.startswith(_MCP_PREFIX)


def _mcp_401() -> JSONResponse:
    return JSONResponse(
        {"detail": "invalid or missing MCP token"},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def mcp_scoped_token_middleware(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """對 /mcp 路徑強制 per-user MCP scoped token(見上方註解)。"""
    if not _is_mcp_path(request.url.path):
        return await call_next(request)

    provided = _extract_bearer(request.headers.get("authorization", ""))
    if provided is None:
        return _mcp_401()

    # 惰性 import:避免 auth 模組 import 期就拉 DB / domain(測試 monkeypatch 友善)
    import cmms.db as _db
    from cmms.domain.identity.service import IdentityService, mcp_transport_identity

    async with _db.get_sessionmaker()() as session:
        resolved = await IdentityService(session).resolve_scoped_token(provided)
    if resolved is None:
        return _mcp_401()

    ctx = mcp_transport_identity.set(resolved)  # (user_id, scope) → 工具層 fallback
    try:
        return await call_next(request)
    finally:
        mcp_transport_identity.reset(ctx)


def register_mcp_auth(app: FastAPI) -> None:
    """把 /mcp per-user token 閘門掛成 http middleware(在 register_read_api_auth 之後呼叫,
    使其成為最外層之一;與 static bearer 互斥覆蓋:static 豁免 /mcp、本閘門只管 /mcp)。"""
    app.middleware("http")(mcp_scoped_token_middleware)
