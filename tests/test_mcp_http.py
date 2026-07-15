"""MCP Streamable HTTP 掛載(/mcp)+ per-user scoped token 閘門 + mint CLI 測試。

覆蓋三層:
1. **auth 閘門(免 DB,永遠跑)**:無 token → 401(/mcp、/mcp/、GET);與 static read
   bearer 的豁免互動(production fail-closed 不波及 /mcp)。
2. **端到端(testcontainers;無 Docker 自動 skip)**:有效 token → MCP initialize 握手
   (stateless streamable http)+ tools/call ping + **transport 身分 fallback 委派寫入**
   (contextvar 穿越 middleware → session manager task group → 工具)。
3. **domain / CLI**:mint_scoped_token_for_user(TTL / inactive 拒發)、
   revoke_scoped_tokens_for_user、`cmms mcp-token` / `mcp-token-revoke`。

★ DB fixture 用 NullPool:TestClient(portal thread 專屬 loop)與 asyncio.run(setup /
assert 的短命 loop)交錯使用同一 engine,pool 化連線會綁死舊 loop。
★ lifespan 由 module-scoped TestClient context 進入一次;api/app.py 的
_McpTransportProxy 讓 lifespan 可重入(SDK session manager run() once-per-instance)。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.config import get_settings

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    },
}
_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def _call(tool: str, arguments: dict, req_id: int = 2) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }


# ---- 1. auth 閘門(免 DB)----

_anon_client = TestClient(app, raise_server_exceptions=False)  # 不進 lifespan(401 在閘門擋下)


@pytest.fixture(autouse=True)
def _isolate_settings():
    """比照 test_read_api_auth:settings 是 lru_cache,前後清快取避免外溢。"""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_mcp_no_token_401():
    r = _anon_client.post("/mcp", json=_INIT, headers=_HEADERS)
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid or missing MCP token"}
    assert r.headers.get("www-authenticate") == "Bearer"


def test_mcp_trailing_slash_and_get_also_gated():
    # /mcp/(Mount 307 目標)與 GET(SSE listen)都在閘門後,無繞縫
    r = _anon_client.post("/mcp/", json=_INIT, headers=_HEADERS, follow_redirects=False)
    assert r.status_code == 401
    r2 = _anon_client.get("/mcp", headers=_HEADERS)
    assert r2.status_code == 401


def test_mcp_non_bearer_scheme_401():
    r = _anon_client.post(
        "/mcp", json=_INIT, headers={**_HEADERS, "Authorization": "Basic abc"}
    )
    assert r.status_code == 401


def test_mcp_exempt_from_static_read_bearer(monkeypatch):
    """/mcp 豁免 static read bearer(有自己的 per-user 閘門):
    production + 未設 read token → /mcp 不 503(fail-closed 不波及),回 MCP 閘門的 401。
    (「read token ≠ MCP token」的互通性測試需 DB,見 test_mcp_static_read_token_rejected。)"""
    monkeypatch.delenv("CMMS_READ_API_TOKEN", raising=False)
    monkeypatch.setenv("CMMS_APP_ENV", "production")
    get_settings.cache_clear()
    r = _anon_client.post("/mcp", json=_INIT, headers=_HEADERS)
    assert r.status_code == 401  # 非 503:走的是 MCP 閘門
    assert r.json() == {"detail": "invalid or missing MCP token"}


# ---- list_help_docs(唯讀 SOP 目錄,無 DB / 無 session,直呼工具函式)----


def test_list_help_docs_shape():
    """內部規格:回每份 SOP {slug,title,summary,url};url 走 public_base_url + /app/help/<slug>。"""
    from cmms.mcp.server import list_help_docs

    docs = list_help_docs()
    assert len(docs) == 2
    for d in docs:
        assert set(d.keys()) == {"slug", "title", "summary", "url"}
        assert f"/app/help/{d['slug']}" in d["url"]
        assert d["url"].endswith(f"/app/help/{d['slug']}")


# ---- 2 + 3. 端到端 / domain / CLI(testcontainers;無 Docker 自動 skip)----

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.attachment import models as _att_models  # noqa: E402, F401
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.service import (  # noqa: E402
    IdentityError,
    IdentityService,
)
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.models import WorkOrderExternalLink  # noqa: E402

_ASSET_ROWS = [
    {"compid": "EID-002", "comp_desc": "Rig", "assettype": "Production", "department": "EQ",
     "line_no": "10K", "available": "Yes"},
]
_WO_ROWS = [
    {
        "wo": "30167", "compid": "EID-002", "comp_desc": "x", "assetsubtp": "",
        "brief_desc": "fix", "diag": "", "comments": "",
        "date_wo": "05/21/26", "sch_date": "", "wo_type": "REACTIVE", "workstatus": "H",
        "miscreated": "F", "assignto": "CMA (Tester)", "edittime": "15:00:00",
        "editdate": "05/21/26", "edituser": "T", "time": "10:00:00", "time_cmpl": "15:00:00",
    },
]


@pytest.fixture(scope="module")
def db_ctx():
    """module-scoped:PG 容器 + NullPool engine + 種子(user bob + 12h pilot token +
    asset/WO)+ monkeypatch 全域 sessionmaker + 進 lifespan 的 TestClient。"""
    mp = pytest.MonkeyPatch()
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        async def _setup() -> str:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sm() as s:
                await load_assets(_ASSET_ROWS, s)
                await load_wo(_WO_ROWS, s)
                svc = IdentityService(s)
                await svc.create_user(
                    user_id="bob", username="bob", display_name="Bob",
                    password="password8", org="plant", actor=Actor.human("cli"),
                )
                token, _ = await svc.mint_scoped_token_for_user(
                    username="bob", agent="codex", scope="pilot"
                )
                return token

        token = asyncio.run(_setup())
        mp.setattr("cmms.db.get_sessionmaker", lambda: sm)
        mp.setattr("cmms.mcp.server.get_sessionmaker", lambda: sm)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                yield client, token, sm
        finally:
            mp.undo()
            asyncio.run(engine.dispose())


def _auth(token: str) -> dict[str, str]:
    return {**_HEADERS, "Authorization": f"Bearer {token}"}


def test_mcp_bogus_token_401(db_ctx):
    client, _token, _sm = db_ctx
    r = client.post("/mcp", json=_INIT, headers=_auth("bogus-token"))
    assert r.status_code == 401
    assert r.json() == {"detail": "invalid or missing MCP token"}


def test_mcp_static_read_token_rejected(db_ctx, monkeypatch):
    """兩把鑰匙不互通:CMMS_READ_API_TOKEN(服務間讀取)打 /mcp 一樣 401。"""
    client, _token, _sm = db_ctx
    monkeypatch.setenv("CMMS_READ_API_TOKEN", "static-read-token")
    get_settings.cache_clear()
    r = client.post("/mcp", json=_INIT, headers=_auth("static-read-token"))
    assert r.status_code == 401


def test_mcp_initialize_handshake(db_ctx):
    client, token, _sm = db_ctx
    r = client.post("/mcp", json=_INIT, headers=_auth(token))
    assert r.status_code == 200
    assert "protocolVersion" in r.text  # SSE event: message → initialize result
    assert '"cmms"' in r.text  # serverInfo.name


def test_mcp_tools_call_ping(db_ctx):
    """stateless:每請求獨立,不需先 initialize 同一 session 即可 tools/call。"""
    client, token, _sm = db_ctx
    r = client.post("/mcp", json=_call("ping", {}), headers=_auth(token))
    assert r.status_code == 200
    assert '"status": "ok"' in r.text.replace('\\"', '"') or "ok" in r.text


def test_mcp_transport_identity_fallback_delegated_write(db_ctx):
    """★ 核心:scoped_token / on_behalf_of 兩參數皆缺 → 委派寫入 fallback 到 transport
    閘門驗出的身分(contextvar 穿越 middleware → SDK task group → 工具)。"""
    client, token, sm = db_ctx
    r = client.post(
        "/mcp",
        json=_call(
            "record_work_order_external_link",
            {"work_order_no": 30167, "external_key": "MRQ-4242"},
        ),
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert '"isError":false' in r.text  # 工具執行成功(SSE 內層 JSON 為 compact 格式)

    async def _check() -> WorkOrderExternalLink:
        async with sm() as s:
            return (
                await s.scalars(
                    select(WorkOrderExternalLink).where(
                        WorkOrderExternalLink.external_key == "MRQ-4242"
                    )
                )
            ).one()

    link = asyncio.run(_check())
    assert link.created_by == "human:bob"  # transport 身分(非參數斷言)
    assert link.source_actor == "agent:hermes"  # dual attribution(工具預設 agent 名)


def test_mcp_transport_identity_beats_on_behalf_of_assertion(db_ctx):
    """★ F-1b 反冒名:transport 閘門驗過的身分**勝過** on_behalf_of 裸斷言 —— 持自己 token(bob)
    者不得宣稱代表他人(alice)。cmms 驗過的身分優先於裸斷言。"""
    client, token, sm = db_ctx
    r = client.post(
        "/mcp",
        json=_call(
            "record_work_order_external_link",
            {"work_order_no": 30167, "external_key": "MRQ-4243", "on_behalf_of": "alice"},
        ),
        headers=_auth(token),
    )
    assert r.status_code == 200

    async def _check() -> WorkOrderExternalLink:
        async with sm() as s:
            return (
                await s.scalars(
                    select(WorkOrderExternalLink).where(
                        WorkOrderExternalLink.external_key == "MRQ-4243"
                    )
                )
            ).one()

    assert asyncio.run(_check()).created_by == "human:bob"  # transport 勝過 on_behalf_of 斷言


# ---- 3. domain / CLI(mint→resolve 往返、TTL、revoke)----
# 用獨立使用者 eve,避免 revoke 波及上面 HTTP 測試共用的 bob token。


def test_mint_for_user_ttl_and_resolve(db_ctx):
    _client, _token, sm = db_ctx

    async def _run() -> None:
        async with sm() as s:
            svc = IdentityService(s)
            await svc.create_user(
                user_id="eve", username="eve", display_name="Eve",
                password="password8", org="plant", actor=Actor.human("cli"),
            )
            # 預設 TTL = config.mcp_pilot_token_ttl_seconds(12h),非 gateway 短票 300s
            tok, exp = await svc.mint_scoped_token_for_user(
                username="eve", agent="codex", scope="pilot"
            )
            ttl = (exp - datetime.now(UTC)).total_seconds()
            assert 43200 - 120 < ttl <= 43200 + 120
            assert await svc.resolve_scoped_token(tok) == ("eve", "pilot")
            # 明確 TTL 覆蓋
            _tok2, exp2 = await svc.mint_scoped_token_for_user(
                username="eve", agent="codex", scope="pilot", ttl_seconds=60
            )
            assert (exp2 - datetime.now(UTC)).total_seconds() <= 61
            # 過期即失效
            tok3, _ = await svc.mint_scoped_token_for_user(
                username="eve", agent="codex", scope="pilot", ttl_seconds=1,
                at=datetime.now(UTC).replace(year=2000),
            )
            assert await svc.resolve_scoped_token(tok3) is None

    asyncio.run(_run())


def test_mint_unknown_user_rejected(db_ctx):
    _client, _token, sm = db_ctx

    async def _run() -> None:
        async with sm() as s:
            with pytest.raises(IdentityError):
                await IdentityService(s).mint_scoped_token_for_user(
                    username="nobody", agent="codex", scope="pilot"
                )

    asyncio.run(_run())


def test_revoke_all_active_tokens(db_ctx):
    _client, _token, sm = db_ctx

    async def _run() -> None:
        async with sm() as s:
            svc = IdentityService(s)
            await svc.create_user(
                user_id="rev", username="rev", display_name="Rev",
                password="password8", org="plant", actor=Actor.human("cli"),
            )
            t1, _ = await svc.mint_scoped_token_for_user(
                username="rev", agent="codex", scope="pilot"
            )
            t2, _ = await svc.mint_scoped_token_for_user(
                username="rev", agent="codex", scope="pilot"
            )
            assert await svc.revoke_scoped_tokens_for_user(username="rev") == 2
            assert await svc.resolve_scoped_token(t1) is None
            assert await svc.resolve_scoped_token(t2) is None
            assert await svc.revoke_scoped_tokens_for_user(username="rev") == 0  # 冪等
            with pytest.raises(IdentityError):  # 拼錯帳號 → raise,不誤報「已撤」
                await svc.revoke_scoped_tokens_for_user(username="nobody")

    asyncio.run(_run())


def test_cli_mint_and_revoke(db_ctx):
    """CLI 面:`cmms mcp-token` 印 token 明文一次 + 過期時間;`mcp-token-revoke` 回筆數。"""
    from typer.testing import CliRunner

    from cmms.cli.main import app as cli_app

    _client, _token, _sm = db_ctx
    runner = CliRunner()
    r = runner.invoke(
        cli_app, ["mcp-token", "--username", "bob", "--ttl-hours", "1", "--agent", "codex"]
    )
    assert r.exit_code == 0, r.output
    assert "token: " in r.output and "expires_at: " in r.output
    minted = r.output.split("token: ", 1)[1].splitlines()[0].strip()

    async def _resolve() -> tuple[str, str] | None:
        async with _sm() as s:
            return await IdentityService(s).resolve_scoped_token(minted)

    assert asyncio.run(_resolve()) == ("bob", "pilot")

    r2 = runner.invoke(cli_app, ["mcp-token-revoke", "--username", "bob"])
    assert r2.exit_code == 0, r2.output
    assert "revoked" in r2.output
    assert asyncio.run(_resolve()) is None


def test_mcp_propose_update_item(db_ctx):
    """propose_update_item:agent 提案改備品欄位 → 建 update_item pending_proposal;
    proposer = transport 閘門驗過的 human:bob(非自報);dry-run diff 只列真的改動。"""
    client, _token, sm = db_ctx  # 不用共用 token(可能已被 revoke 測試撤銷)→ 本測試自鑄新票
    from cmms.domain.inventory.loader import load as load_inv
    from cmms.domain.work_order.models import PendingProposal

    inv_row = {
        "item": "MCPITEM1", "asset_sub": "", "sf_desc": "Pump", "vpartno": "",
        "descrip": "vacuum pump", "location": "A1", "orderpt": "", "onhand": "",
        "cost": "", "lead_time": "", "obsol": "F", "stock": "T", "supplier": "",
        "weblink": "", "photo": "", "parnt_item": "", "child_item": "", "alt_item": "",
        "comment": "",
    }

    async def _seed() -> str:
        async with sm() as s:
            await load_inv([inv_row], s)
            tok, _ = await IdentityService(s).mint_scoped_token_for_user(
                username="bob", agent="codex", scope="pilot"
            )
            return tok

    token = asyncio.run(_seed())

    r = client.post(
        "/mcp",
        json=_call("propose_update_item", {"item_code": "MCPITEM1", "bin_location": "B2"}),
        headers=_auth(token),
    )
    assert r.status_code == 200
    assert '"isError":false' in r.text
    assert "pending_token" in r.text

    async def _check() -> PendingProposal:
        async with sm() as s:
            return (
                await s.scalars(
                    select(PendingProposal).where(PendingProposal.operation == "update_item")
                )
            ).one()

    p = asyncio.run(_check())
    assert p.proposed_by == "human:bob"                 # transport 身分,非自報
    assert p.status == "PENDING"
    # dry-run diff 只列真正的改動(未指定欄位沿用現值,不誤報)
    assert set(p.dry_run_diff["changes"]) == {"bin_location"}
    assert p.dry_run_diff["changes"]["bin_location"] == {"from": "A1", "to": "B2"}


def test_mcp_forward_work_orders_dry_run(db_ctx, monkeypatch):
    """forward_work_orders_to_mrq:dry_run 預設 true → 零寫入預覽;acting_user = transport 身分。"""
    from cryptography.fernet import Fernet

    from cmms.domain.identity.vault import CredentialVault

    client, _token, sm = db_ctx
    monkeypatch.setenv("CMMS_CREDENTIAL_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CMMS_JIRA_BASE_URL", "https://jira.example")
    monkeypatch.setenv("CMMS_JIRA_MRQ_PROJECT_KEY", "MRQ")
    get_settings.cache_clear()

    async def _seed() -> str:
        async with sm() as s:
            await CredentialVault(s).store_credential(
                user_id="bob", system="jira", secret="PAT", actor=Actor.human("bob")
            )
            tok, _ = await IdentityService(s).mint_scoped_token_for_user(
                username="bob", agent="codex", scope="pilot"
            )
            return tok

    token = asyncio.run(_seed())
    r = client.post(
        "/mcp",
        json=_call(
            "forward_work_orders_to_mrq",
            {"work_order_nos": [30167], "summary": "S", "description": "D"},
        ),
        headers=_auth(token),
    )
    assert r.status_code == 200
    body = r.text.replace('\\"', '"')
    assert '"isError":false' in body
    assert '"dry_run":true' in body  # 預設 dry_run;零寫入預覽
    assert '"pat_ready":true' in body
