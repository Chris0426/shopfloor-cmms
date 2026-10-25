"""一鍵轉發工單→Jira MRQ 的 web 層測試(ADR-020 決策 1 修訂;純編碼路徑)。

不需 DB:以 dependency override(get_current_user / get_session)+ monkeypatch domain service
(WorkOrderService / AssetService / JiraSyncService)避開真 DB,只驗路由 / 模板 / readiness /
提交行為。domain 轉發邏輯本身由 tests/test_jira_sync_db.py 涵蓋。
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.domain.jira_sync.service import (
    ForwardResult,
    ForwardWoSummary,
    JiraSyncError,
)
from cmms.web import routes as web_routes

client = TestClient(app)


def _hermes_settings() -> SimpleNamespace:
    """Hermes gateway 已配置的 settings stub(其餘 forward readiness 欄位一併帶齊)。"""
    return SimpleNamespace(
        hermes_configured=True,
        hermes_gateway_url="https://hermes.internal",
        hermes_gateway_secret="s3cr3t",
        jira_forwarder_configured=True,
        demo_mode=False,
        demo_reset_minutes=30,
        ui_hide_jira=False,
    )


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


class _FakeClient:
    """monkeypatch web_routes.httpx.AsyncClient:攔截 gateway 呼叫,回可控 payload。"""

    captured: dict = {}
    resp: _FakeResp | None = None
    raise_exc: Exception | None = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        _FakeClient.captured = {"url": url, "headers": headers, "json": json}
        if _FakeClient.raise_exc is not None:
            raise _FakeClient.raise_exc
        return _FakeClient.resp


def _fake_user(*, locale: str = "en", role: str = "engineer") -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="陳工",
        role=role, ui_locale=locale, jira_output_locale="en", is_active=True,
        emaint_assignee="Alice Fang",
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
def _wo_reads(monkeypatch):
    """兩張工單(同/異設備)+ 資產名 + 無既有連結。"""
    wos = {
        90018: SimpleNamespace(
            work_order_no=90018, asset_id="EID-70021", status="IN_PROGRESS",
            brief_description="吸嘴堵塞、取料失敗連續報警",
            opened_at=datetime(2026, 7, 1, 9, 0),
        ),
        90020: SimpleNamespace(
            work_order_no=90020, asset_id="EID-70005", status="ON_HOLD",
            brief_description="供料卡料", opened_at=datetime(2026, 7, 2, 10, 0),
        ),
    }
    assets = {
        "EID-70021": SimpleNamespace(asset_id="EID-70021", description="Aligner46 打線機"),
        "EID-70005": SimpleNamespace(asset_id="EID-70005", description="Curer9 供料模組"),
    }

    async def _get_wo(self, no):
        return wos.get(no)

    async def _get_asset(self, aid):
        return assets.get(aid)

    async def _links(self, no):
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get_wo)
    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get_asset)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_external_links", _links)
    return {"wos": wos, "assets": assets}


def _dry(nos, summary, description, *, pat=True, config=True, warnings=None):
    return ForwardResult(
        dry_run=True, external_key=None,
        work_orders=[ForwardWoSummary(n, note_count=3, photo_count=1) for n in nos],
        total_comments=3 * len(nos), total_photos=len(nos),
        summary=summary, description=description,
        pat_ready=pat, config_ready=config, warnings=warnings or [],
    )


# ---- GET 表單頁 ----

def test_forward_form_renders_prefill_and_preview(as_user, _wo_reads, monkeypatch):
    """GET 200:確定性預填含 EID + 設備名;dry-run 預覽數字正確。"""
    async def _fwd(self, *, work_order_nos, summary, description, acting_user, actor,
                   dry_run=True, idempotency_key=None):
        assert dry_run is True
        return _dry(work_order_nos, summary, description)

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    r = client.get("/app/work-orders/90018/forward")
    assert r.status_code == 200
    # 預填 summary/description(受眾導向:EID + 設備真名;不得含工單號)
    assert "EID-70021" in r.text
    assert "Aligner46 打線機" in r.text
    assert "吸嘴堵塞、取料失敗連續報警" in r.text   # summary 基底含 brief
    # 反饋 1/2:description 預填留空、不帶 opened/status 模板(改由 AI 總結生成)
    assert "opened 2026-07-01" not in r.text
    after = r.text.split('id="fwd-desc"', 1)[1]
    desc_body = after.split(">", 1)[1].split("</textarea>", 1)[0]
    assert desc_body.strip() == ""                # description textarea 內容為空
    assert "Press" in r.text or "AI-summarize" in r.text  # placeholder / 按鈕文字(en)
    # summary 硬規則:不得含工單號(受眾=別單位/老闆;追溯已在 comment 標頭)
    summary_area = r.text.split("<textarea")[1].split("</textarea>")[0]
    assert "90018" not in summary_area
    # dry-run 預覽:note/photo 數 + 總數
    assert "WO-90018" in r.text                    # 預覽列(連結)
    assert ">3<" in r.text and ">1<" in r.text     # note_count 3 / photo_count 1


def test_forward_form_batch_merges_wos(as_user, _wo_reads, monkeypatch):
    """?wos= 合併同批其他工單號;兩張都進 dry-run + 預覽。"""
    captured: dict = {}

    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        captured["nos"] = list(work_order_nos)
        return _dry(work_order_nos, summary, description)

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    r = client.get("/app/work-orders/90018/forward?wos=90020")
    assert r.status_code == 200
    assert captured["nos"] == [90018, 90020]       # primary 第一 + 去重保序
    assert "Curer9 供料模組" in r.text              # 第二張的設備名也入預填


def test_forward_form_pat_missing_disables_submit(as_user, _wo_reads, monkeypatch):
    """PAT 未備 → 警語 + settings#pat 連結 + 送出禁用。"""
    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        return _dry(work_order_nos, summary, description, pat=False,
                    warnings=["no active jira PAT"])

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    r = client.get("/app/work-orders/90018/forward")
    assert r.status_code == 200
    assert "/app/settings#pat" in r.text
    assert "disabled" in r.text                    # 送出鈕禁用


def test_forward_form_unknown_wo_shows_banner_not_500(as_user, _wo_reads, monkeypatch):
    """同批含不存在工單號 → 誠實 banner + 略過,不 500。"""
    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        return _dry(work_order_nos, summary, description)

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    r = client.get("/app/work-orders/90018/forward?wos=99999")
    assert r.status_code == 200
    assert "WO-99999" in r.text                    # 列於「查無、已略過」


# ---- POST 送出 ----

def test_forward_submit_success(as_user, _wo_reads, monkeypatch):
    """POST 成功 → 302 至詳情帶 forward_ok + mrq;forward 以 dry_run=False 呼叫。"""
    captured: dict = {}

    async def _fwd(self, *, work_order_nos, summary, description, acting_user, actor,
                   dry_run=True, idempotency_key=None):
        captured.update(nos=list(work_order_nos), summary=summary, description=description,
                        dry_run=dry_run, idem=idempotency_key, user=acting_user)
        return ForwardResult(
            dry_run=False, external_key="MRQ-4242",
            work_orders=[ForwardWoSummary(n, 3, 1) for n in work_order_nos],
            total_comments=3, total_photos=1, summary=summary, description=description,
            pat_ready=True, config_ready=True, already_forwarded=False,
        )

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    r = client.post(
        "/app/work-orders/90018/forward",
        data={"summary": "EID-70021 Aligner46 — 吸嘴堵塞", "description": "line one",
              "wos": "90020", "idem_key": "abc123"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/90018?msg=forward_ok&mrq=MRQ-4242"
    assert captured["dry_run"] is False
    assert captured["nos"] == [90018, 90020]
    assert captured["idem"] == "abc123"
    assert captured["user"] == "jlee"


def test_forward_submit_already_forwarded(as_user, _wo_reads, monkeypatch):
    """already_forwarded → forward_exists 訊息 + 復用既有 MRQ key。"""
    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        return ForwardResult(
            dry_run=False, external_key="MRQ-1", work_orders=[], total_comments=0,
            total_photos=0, summary=summary, description=description,
            pat_ready=True, config_ready=True, already_forwarded=True,
        )

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    r = client.post(
        "/app/work-orders/90018/forward",
        data={"summary": "s", "description": "d", "idem_key": "k"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/work-orders/90018?msg=forward_exists&mrq=MRQ-1"


def test_forward_submit_empty_summary_reshows_form(as_user, _wo_reads, monkeypatch):
    """空 summary → 回表單帶錯誤,不以 dry_run=False 執行。"""
    calls: list = []

    async def _fwd(self, *, work_order_nos, summary, description, dry_run=True, **kw):
        calls.append(dry_run)
        return _dry(work_order_nos, summary, description)

    async def _ready(self, user_id):
        return True

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    monkeypatch.setattr(web_routes.JiraSyncService, "pat_ready", _ready)
    r = client.post(
        "/app/work-orders/90018/forward",
        data={"summary": "  ", "description": "d", "idem_key": "k"},
        follow_redirects=False,
    )
    assert r.status_code == 200                    # 回表單(非 redirect)
    assert "both required" in r.text               # wo.forward.err.empty(en)
    assert False not in calls                       # 從未以 dry_run=False 執行


def test_forward_submit_jira_error_safe_banner(as_user, _wo_reads, monkeypatch):
    """JiraSyncError → 回表單帶安全錯誤 banner(不 500、不洩漏細節)。"""
    async def _fwd(self, *, work_order_nos, summary, description, dry_run=True, **kw):
        if not dry_run:
            raise JiraSyncError("no active jira PAT for jlee")
        return _dry(work_order_nos, summary, description, pat=False)

    async def _ready(self, user_id):
        return False

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    monkeypatch.setattr(web_routes.JiraSyncService, "pat_ready", _ready)
    r = client.post(
        "/app/work-orders/90018/forward",
        data={"summary": "s", "description": "d", "idem_key": "k"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    # PAT 分類訊息;絕不照抄例外原文
    assert "Settings" in r.text
    assert "no active jira PAT for jlee" not in r.text


# ---- 連結表單同步勾選 ----

def test_add_link_sync_pat_missing_no_link(as_user, monkeypatch):
    """勾選同步但 PAT 未備 → 不落連結 + flash link_needpat。"""
    called = {"n": 0}

    async def _ready(self, user_id):
        return False

    async def _record(self, **kw):
        called["n"] += 1

    monkeypatch.setattr(web_routes.JiraSyncService, "pat_ready", _ready)
    monkeypatch.setattr(web_routes.WorkOrderService, "record_external_link", _record)
    r = client.post(
        "/app/work-orders/90018/links",
        data={"external_key": "MRQ-7", "sync": "1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/work-orders/90018?msg=link_needpat"
    assert called["n"] == 0                          # 未落連結


def test_add_link_sync_ready_records_appended(as_user, monkeypatch):
    """勾選同步 + PAT ready → record_external_link(link_type=appended)。"""
    captured: dict = {}

    async def _ready(self, user_id):
        return True

    async def _record(self, **kw):
        captured.update(kw)
        return SimpleNamespace(id=1)

    monkeypatch.setattr(web_routes.JiraSyncService, "pat_ready", _ready)
    monkeypatch.setattr(web_routes.WorkOrderService, "record_external_link", _record)
    r = client.post(
        "/app/work-orders/90018/links",
        data={"external_key": "mrq-7", "sync": "1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/work-orders/90018?msg=link_ok"
    assert captured["link_type"] == "appended"
    assert captured["external_key"] == "MRQ-7"       # 正規化大寫


# ---- AI 總結工作紀錄(反饋 3;/forward/ai-description)----

@pytest.fixture
def _notes(monkeypatch):
    """兩筆工作日誌 for list_notes(供 AI 總結取樣)。"""
    notes = [
        SimpleNamespace(occurred_at=datetime(2026, 7, 1, 9, 5),
                        author="human:jlee", body="吸嘴堵塞,拆下清潔"),
        SimpleNamespace(occurred_at=datetime(2026, 7, 1, 11, 0),
                        author="human:jlee", body="重新校正取料座,測試通過"),
    ]

    async def _list_notes(self, no):
        return notes

    monkeypatch.setattr(web_routes.WorkOrderService, "list_notes", _list_notes)
    return notes


def test_ai_description_button_renders_when_hermes_configured(
    as_user, _wo_reads, monkeypatch
):
    """Hermes 已配置 → GET 表單渲染「AI 總結工作紀錄」按鈕 + 端點。"""
    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        return _dry(work_order_nos, summary, description)

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    monkeypatch.setattr(web_routes, "get_settings", _hermes_settings)
    r = client.get("/app/work-orders/90018/forward")
    assert r.status_code == 200
    assert "/app/work-orders/90018/forward/ai-description" in r.text
    assert "AI-summarize work log" in r.text          # 按鈕標籤(en)


def test_ai_description_button_hidden_when_hermes_unconfigured(
    as_user, _wo_reads, monkeypatch
):
    """Hermes 未配置 → 按鈕不渲染(誠實不假裝有 agent)。"""
    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        return _dry(work_order_nos, summary, description)

    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    monkeypatch.setattr(
        web_routes, "get_settings",
        lambda: SimpleNamespace(
            hermes_configured=False, jira_forwarder_configured=True,
            demo_mode=False, demo_reset_minutes=30, ui_hide_jira=False,
        ),
    )
    r = client.get("/app/work-orders/90018/forward")
    assert r.status_code == 200
    assert "/forward/ai-description" not in r.text


def test_ai_description_success_fills_textarea(as_user, _wo_reads, _notes, monkeypatch):
    """gateway 成功 → partial textarea 填生成文字;payload 含 notes + jira_locale。"""
    _FakeClient.resp = _FakeResp({"description": "Belt jam cleared; pickup realigned."})
    _FakeClient.raise_exc = None
    monkeypatch.setattr(web_routes, "get_settings", _hermes_settings)
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _FakeClient)

    r = client.post(
        "/app/work-orders/90018/forward/ai-description",
        data={"wos": "90020", "description": "old text"},
    )
    assert r.status_code == 200
    assert "Belt jam cleared; pickup realigned." in r.text
    # gateway 收到正確端點 + secret header + 兩張工單 + jira_locale
    cap = _FakeClient.captured
    assert cap["url"] == "https://hermes.internal/mrq-description"
    assert cap["headers"]["X-Hermes-Secret"] == "s3cr3t"
    assert cap["json"]["jira_locale"] == "English"  # UI locale en → 明確語言名(Jordan 2026-07-07)
    assert len(cap["json"]["work_orders"]) == 2
    assert cap["json"]["work_orders"][0]["eid"] == "EID-70021"
    assert cap["json"]["work_orders"][0]["notes"][0]["body"].startswith("吸嘴堵塞")
    assert "scoped_token" not in cap["json"]          # 此任務不帶身分


def test_ai_description_gateway_failure_keeps_original(
    as_user, _wo_reads, _notes, monkeypatch
):
    """gateway 失敗 → 保留使用者原文 + 誠實錯誤 hint(不假造)。"""
    _FakeClient.resp = None
    _FakeClient.raise_exc = httpx.ConnectError("boom")
    monkeypatch.setattr(web_routes, "get_settings", _hermes_settings)
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _FakeClient)

    r = client.post(
        "/app/work-orders/90018/forward/ai-description",
        data={"wos": "", "description": "my draft text"},
    )
    assert r.status_code == 200
    assert "my draft text" in r.text                  # 原文保留
    assert "Could not generate a summary" in r.text   # 失敗 hint(en)


def test_ai_description_empty_reply_keeps_original(
    as_user, _wo_reads, _notes, monkeypatch
):
    """gateway 回空 description → 當暫時無法產生,保留原文 + hint。"""
    _FakeClient.resp = _FakeResp({"description": "  "})
    _FakeClient.raise_exc = None
    monkeypatch.setattr(web_routes, "get_settings", _hermes_settings)
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _FakeClient)

    r = client.post(
        "/app/work-orders/90018/forward/ai-description",
        data={"wos": "", "description": "keep me"},
    )
    assert r.status_code == 200
    assert "keep me" in r.text
    assert "Could not generate a summary" in r.text


def test_ai_description_unconfigured_returns_disabled_hint(
    as_user, _wo_reads, monkeypatch
):
    """Hermes 未配置時直接呼叫端點 → disabled hint,不打 gateway。"""
    called = {"n": 0}

    class _NoCall(_FakeClient):
        async def post(self, *a, **k):
            called["n"] += 1
            return _FakeResp({"description": "x"})

    monkeypatch.setattr(
        web_routes, "get_settings",
        lambda: SimpleNamespace(
            hermes_configured=False, demo_mode=False, demo_reset_minutes=30,
            ui_hide_jira=False,
        ),
    )
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _NoCall)
    r = client.post(
        "/app/work-orders/90018/forward/ai-description",
        data={"wos": "", "description": "d"},
    )
    assert r.status_code == 200
    assert "not enabled" in r.text
    assert called["n"] == 0


def test_add_link_no_sync_stays_referenced(as_user, monkeypatch):
    """未勾同步 → 現行為(referenced),不查 PAT。"""
    captured: dict = {}
    ready_calls = {"n": 0}

    async def _ready(self, user_id):
        ready_calls["n"] += 1
        return True

    async def _record(self, **kw):
        captured.update(kw)
        return SimpleNamespace(id=1)

    monkeypatch.setattr(web_routes.JiraSyncService, "pat_ready", _ready)
    monkeypatch.setattr(web_routes.WorkOrderService, "record_external_link", _record)
    r = client.post(
        "/app/work-orders/90018/links",
        data={"external_key": "MRQ-7"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/work-orders/90018?msg=link_ok"
    assert captured["link_type"] == "referenced"
    assert ready_calls["n"] == 0                      # 未勾 → 不查 PAT
