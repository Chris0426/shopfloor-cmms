"""JiraForwarder fake 契約測試 + HttpJiraForwarder live 實作測試。

- fake:ADR-020 決策 7 note↔comment 1:1 冪等(純函式,無 DB)。
- HttpJiraForwarder:httpx MockTransport 驗 auth header / payload 形狀 / 非 2xx 拋錯不洩 PAT。
"""

from __future__ import annotations

import json

import httpx
import pytest

from cmms.config import get_settings
from cmms.jira_forwarder import (
    HttpJiraForwarder,
    InMemoryJiraForwarder,
    JiraForwardError,
    NullJiraForwarder,
    build_jira_forwarder,
    get_jira_forwarder,
)


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _patch_client(monkeypatch, handler):
    """把 cmms.jira_forwarder 用到的 httpx.AsyncClient 換成掛 MockTransport 的 client。"""
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def _factory(*_a, **kwargs):
        kwargs.pop("timeout", None)
        return real(transport=transport)

    monkeypatch.setattr("cmms.jira_forwarder.httpx.AsyncClient", _factory)


async def test_inmemory_comment_idempotent() -> None:
    f = InMemoryJiraForwarder()
    key = await f.create_mrq(summary="s", body="b")
    assert key == "MRQ-1"
    c1 = await f.append_mrq_comment(external_key=key, body="note1", idempotency_key="n1")
    c2 = await f.append_mrq_comment(external_key=key, body="note1", idempotency_key="n1")  # 冪等
    assert c1 == c2 and len(f.comments[key]) == 1  # 同 note → 1 則 comment(1:1,不重貼)
    c3 = await f.append_mrq_comment(external_key=key, body="note2", idempotency_key="n2")
    assert c3 != c1 and len(f.comments[key]) == 2


async def test_inmemory_create_idempotent() -> None:
    f = InMemoryJiraForwarder()
    k1 = await f.create_mrq(summary="s", body="b", idempotency_key="wo-1")
    k2 = await f.create_mrq(summary="s", body="b", idempotency_key="wo-1")
    assert k1 == k2 and len(f.issues) == 1


async def test_null_forwarder_no_effect() -> None:
    f = NullJiraForwarder()
    assert await f.create_mrq(summary="s", body="b") == "MRQ-0"
    assert await f.append_mrq_comment(external_key="MRQ-0", body="x") == "null-comment"
    assert await f.upload_attachment(
        external_key="MRQ-0", filename="p.jpg", data=b"x", content_type="image/jpeg"
    ) == "null-attachment"


def test_get_forwarder_default_null() -> None:
    assert isinstance(get_jira_forwarder(), NullJiraForwarder)


# ---- HttpJiraForwarder(live REST;httpx MockTransport)----


async def test_http_create_mrq_auth_and_payload(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"key": "MRQ-1234"})

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(
        base_url="https://jira.example", pat="SECRET-PAT", project_key="MRQ", issue_type="Task"
    )
    key = await f.create_mrq(summary="pump fault", body="details")
    assert key == "MRQ-1234"
    assert captured["auth"] == "Bearer SECRET-PAT"  # Data Center Bearer PAT
    assert captured["path"] == "/rest/api/2/issue"
    fields = captured["body"]["fields"]
    assert fields["project"]["key"] == "MRQ"
    assert fields["issuetype"]["name"] == "Task"
    assert fields["summary"] == "pump fault"
    assert fields["description"] == "details"


async def test_http_append_comment(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/2/issue/MRQ-9/comment"
        assert json.loads(request.content) == {"body": "note text"}
        return httpx.Response(201, json={"id": "10001"})

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(base_url="https://jira.example/", pat="p", project_key="MRQ")
    cid = await f.append_mrq_comment(external_key="MRQ-9", body="note text")
    assert cid == "10001"


async def test_http_non_2xx_raises_without_pat_leak(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden for token")

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(base_url="https://jira.example", pat="SECRET-PAT", project_key="MRQ")
    with pytest.raises(JiraForwardError) as ei:
        await f.create_mrq(summary="s", body="d")
    assert ei.value.status == 403
    assert "SECRET-PAT" not in str(ei.value)  # PAT 絕不入例外


async def test_http_connection_error_raises(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(base_url="https://jira.example", pat="p", project_key="MRQ")
    with pytest.raises(JiraForwardError):
        await f.append_mrq_comment(external_key="MRQ-1", body="x")


async def test_inmemory_upload_records_and_returns_filename() -> None:
    f = InMemoryJiraForwarder()
    key = await f.create_mrq(summary="s", body="b")
    ref = await f.upload_attachment(
        external_key=key, filename="wo30167-note5-p.jpg", data=b"bytes", content_type="image/jpeg"
    )
    assert ref == "wo30167-note5-p.jpg"  # 回送出的檔名(= 內嵌引用鍵)
    assert f.attachments[key] == [("wo30167-note5-p.jpg", b"bytes", "image/jpeg")]


async def test_http_upload_attachment_multipart_headers(monkeypatch) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["xsrf"] = request.headers.get("x-atlassian-token")
        captured["content_type"] = request.headers.get("content-type")
        captured["raw"] = request.content
        return httpx.Response(200, json=[{"filename": "wo1-note2-p.jpg", "id": "att-1"}])

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(base_url="https://jira.example", pat="SECRET-PAT", project_key="MRQ")
    ref = await f.upload_attachment(
        external_key="MRQ-7", filename="wo1-note2-p.jpg", data=b"JPEGDATA",
        content_type="image/jpeg",
    )
    assert ref == "wo1-note2-p.jpg"  # 取回應 list 第一個 filename
    assert captured["path"] == "/rest/api/2/issue/MRQ-7/attachments"
    assert captured["auth"] == "Bearer SECRET-PAT"
    assert captured["xsrf"] == "no-check"  # Jira 附件必要,否則 XSRF 403
    # 不手設 Content-Type → httpx 自帶 multipart boundary
    assert captured["content_type"].startswith("multipart/form-data; boundary=")
    assert b"JPEGDATA" in captured["raw"] and b'filename="wo1-note2-p.jpg"' in captured["raw"]


async def test_http_upload_attachment_non_2xx_no_pat_leak(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="XSRF check failed for token")

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(base_url="https://jira.example", pat="SECRET-PAT", project_key="MRQ")
    with pytest.raises(JiraForwardError) as ei:
        await f.upload_attachment(
            external_key="MRQ-1", filename="p.jpg", data=b"x", content_type="image/jpeg"
        )
    assert ei.value.status == 403
    assert "SECRET-PAT" not in str(ei.value)  # PAT 絕不入例外


async def test_http_upload_attachment_empty_list_raises(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])  # 無條目 → 誠實拋錯

    _patch_client(monkeypatch, handler)
    f = HttpJiraForwarder(base_url="https://jira.example", pat="p", project_key="MRQ")
    with pytest.raises(JiraForwardError):
        await f.upload_attachment(
            external_key="MRQ-1", filename="p.jpg", data=b"x", content_type="image/jpeg"
        )


def test_build_forwarder_unconfigured_returns_none(monkeypatch) -> None:
    monkeypatch.delenv("CMMS_JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("CMMS_JIRA_MRQ_PROJECT_KEY", raising=False)
    get_settings.cache_clear()
    assert build_jira_forwarder("pat") is None  # config 缺 → None(誠實)


def test_build_forwarder_configured(monkeypatch) -> None:
    monkeypatch.setenv("CMMS_JIRA_BASE_URL", "https://jira.example")
    monkeypatch.setenv("CMMS_JIRA_MRQ_PROJECT_KEY", "MRQ")
    get_settings.cache_clear()
    assert isinstance(build_jira_forwarder("pat"), HttpJiraForwarder)
    assert build_jira_forwarder("") is None  # 無 PAT → None
