"""SOP 說明中心(/app/help,內部規格 + 內部規格)web 冒煙 + 註冊表純函式測試。

- 純函式 / GET 頁 / 空留言:不需 DB(dependency override get_current_user + session=None)。
- 回饋 POST 落 DB(內部規格:改 DB 為主、email 盡力):需 Docker,以 testcontainers PG +
  patch get_sessionmaker(比照 test_vocab_route_db)驗落庫 + smtp 未配置仍 ok + email 盡力。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.email import EmailError, InMemoryEmailSender
from cmms.web import help_docs
from cmms.web import routes as web_routes

client = TestClient(app)


def _fake_user(*, locale: str = "en", role: str = "engineer") -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="陳工",
        role=role, ui_locale=locale, jira_output_locale="en", is_active=True,
        emaint_assignee=None,
    )


@pytest.fixture
def as_user():
    holder = {"user": _fake_user()}

    async def _session():
        yield None

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: holder["user"]
    try:
        yield holder
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


@pytest.fixture
def anon():
    app.dependency_overrides[web_routes.get_current_user] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(web_routes.get_current_user, None)


# ---- 註冊表純函式 ----

def test_get_help_doc_hit_and_miss() -> None:
    doc = help_docs.get_help_doc("telegram-assistant")
    assert doc is not None
    assert doc.slug == "telegram-assistant"
    assert help_docs.get_help_doc("jira-pat") is not None
    assert help_docs.get_help_doc("no-such-slug") is None


def test_help_docs_template_files_exist() -> None:
    """每份註冊 SOP 的 include 片段模板檔實際存在(避免 404/500 於內頁)。"""
    templates_dir = Path(help_docs.__file__).parent / "templates"
    assert help_docs.HELP_DOCS  # 非空
    for doc in help_docs.HELP_DOCS:
        assert (templates_dir / doc.template).is_file(), doc.template


# ---- 路由 ----

def test_help_requires_login(anon) -> None:
    r = client.get("/app/help", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


def test_help_index_lists_both_docs(as_user) -> None:
    r = client.get("/app/help")
    assert r.status_code == 200
    assert "Telegram 助理:綁定與使用" in r.text
    assert "Jira 權杖:讓 CMMS 用你的身分開 MRQ" in r.text


def test_help_doc_page_renders_steps(as_user) -> None:
    r = client.get("/app/help/telegram-assistant")
    assert r.status_code == 200
    assert "在 Telegram 找到機器人" in r.text
    assert "產生綁定碼" in r.text


def test_help_doc_unknown_slug_404(as_user) -> None:
    r = client.get("/app/help/does-not-exist")
    assert r.status_code == 404


# ---- 回饋表單 ----

def test_feedback_empty_message(as_user) -> None:
    r = client.post("/app/help/feedback", data={"message": "   "}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/help?fb=empty"


# ---- 回饋 POST 落 DB(內部規格;需 Docker,無則 skip)----

class _FailingSender:
    """寄信一律 EmailError(驗「email 盡力、失敗不影響 ok banner」)。"""

    async def send(self, **_kw) -> None:  # noqa: D401
        raise EmailError("smtp down")


@pytest.fixture
def db_client(monkeypatch):
    """testcontainers PG + 種子帳號 jlee;patch get_sessionmaker 讓真 get_session 生效。

    回 (client, sm):client 已登入 jlee(get_current_user override),sm 供測試查落庫列。
    比照 test_vocab_route_db:NullPool + asyncio.run seed(避免 TestClient loop 綁死連線)。
    """
    pytest.importorskip("testcontainers.postgres")
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from testcontainers.postgres import PostgresContainer

    from cmms.audit import Actor
    from cmms.db import Base

    # create_all 需全 model 註冊(FK 目標齊全)
    from cmms.domain.asset import models as _a  # noqa: F401
    from cmms.domain.assistant import models as _as  # noqa: F401
    from cmms.domain.attachment import models as _at  # noqa: F401
    from cmms.domain.contacts import models as _c  # noqa: F401
    from cmms.domain.failure_vocab import models as _fv  # noqa: F401
    from cmms.domain.feedback import models as _fb  # noqa: F401
    from cmms.domain.feedback.models import HelpFeedback
    from cmms.domain.identity import models as _id  # noqa: F401
    from cmms.domain.identity.service import IdentityService
    from cmms.domain.inventory import models as _inv  # noqa: F401
    from cmms.domain.notify import models as _n  # noqa: F401
    from cmms.domain.pm_schedule import models as _pm  # noqa: F401
    from cmms.domain.procurement import models as _pr  # noqa: F401
    from cmms.domain.task import models as _t  # noqa: F401
    from cmms.domain.telegram_bridge import models as _tg  # noqa: F401
    from cmms.domain.work_order import models as _wo  # noqa: F401

    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url, poolclass=NullPool)
        sm = async_sessionmaker(engine, expire_on_commit=False)

        async def _setup() -> None:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with sm() as s:
                await IdentityService(s).create_user(
                    user_id="jlee", username="jlee", display_name="陳工",
                    password="password8", org="plant", role="engineer",
                    actor=Actor.human("cli"),
                )

        asyncio.run(_setup())
        monkeypatch.setattr("cmms.api.deps.get_sessionmaker", lambda: sm)
        app.dependency_overrides[web_routes.get_current_user] = lambda: _fake_user()

        async def _rows() -> list[HelpFeedback]:
            async with sm() as s:
                return list(
                    (await s.scalars(select(HelpFeedback).order_by(HelpFeedback.id))).all()
                )

        try:
            yield client, _rows
        finally:
            app.dependency_overrides.pop(web_routes.get_current_user, None)
            asyncio.run(engine.dispose())


def test_feedback_persists_and_smtp_unconfigured_ok(db_client, monkeypatch) -> None:
    """SMTP 未配置也落庫 + ok banner(不再回 smtp err)。"""
    cl, rows = db_client
    monkeypatch.setattr(web_routes, "smtp_configured", lambda: False)
    r = cl.post(
        "/app/help/feedback", data={"message": "想要一份掃碼 SOP"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/help?fb=ok"
    persisted = asyncio.run(rows())
    assert len(persisted) == 1
    assert persisted[0].message == "想要一份掃碼 SOP"
    assert persisted[0].user_id == "jlee"
    assert persisted[0].resolved_at is None


def test_feedback_ok_best_effort_email(db_client, monkeypatch) -> None:
    """落庫成功 + email 盡力送出(InMemory sender 收到一封)。"""
    cl, rows = db_client
    sender = InMemoryEmailSender()
    monkeypatch.setattr(web_routes, "smtp_configured", lambda: True)
    monkeypatch.setattr(web_routes, "get_email_sender", lambda: sender)
    r = cl.post(
        "/app/help/feedback", data={"message": "想要一份掃碼 SOP"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/help?fb=ok"
    assert len(asyncio.run(rows())) == 1  # 落庫
    assert len(sender.sent) == 1  # email 盡力
    msg = sender.sent[0]
    assert msg["to"] == "maintenance@example.com"
    assert "想要一份掃碼 SOP" in (msg["body"] or "")
    assert "陳工" in (msg["subject"] or "")


def test_feedback_email_failure_still_ok(db_client, monkeypatch) -> None:
    """email 寄送失敗(EmailError)仍 ok banner + 落庫保留(盡力通知,不 500、不回 err)。"""
    cl, rows = db_client
    monkeypatch.setattr(web_routes, "smtp_configured", lambda: True)
    monkeypatch.setattr(web_routes, "get_email_sender", lambda: _FailingSender())
    r = cl.post(
        "/app/help/feedback", data={"message": "email 掛了但仍要落庫"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/help?fb=ok"
    assert len(asyncio.run(rows())) == 1
