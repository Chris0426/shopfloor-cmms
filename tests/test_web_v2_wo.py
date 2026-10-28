"""新版介面 v2 · 工單群組(佇列 / 詳情 / 轉發 MRQ)渲染測試。

不需 DB:沿用 test_web_smoke 的 fake user + session=None + monkeypatch service 手法。
預設不帶 `cmms_ui` cookie ⇒ 走 v2;parity 斷言以 `cmms_ui=v1` 渲染同一請求,比對
`action=` / `hx-post=` / `hx-get=` 目標集合必須相等(版面可換,接線不得變)。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.domain.jira_sync.service import ForwardResult, ForwardWoSummary
from cmms.web import routes as web_routes

client = TestClient(app)  # 不設 cookie ⇒ 預設 v2
client_v1 = TestClient(app)
client_v1.cookies.set("cmms_ui", "v1")

_TARGET_RE = re.compile(r'\b(?:action|hx-post|hx-get)="([^"]*)"')


def _targets(html: str) -> set[str]:
    return set(_TARGET_RE.findall(html))


def _get(url: str, *, ui: str | None = None, **kw):
    return (client_v1 if ui == "v1" else client).get(url, **kw)


def _assert_v2(html: str) -> None:
    assert "/static/v2.css" in html
    assert "/static/app.css" not in html


def _fake_user(*, role: str = "engineer", emaint_assignee: str | None = "Alice Fang"):
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="陳工",
        role=role, ui_locale="en", jira_output_locale="en", is_active=True,
        emaint_assignee=emaint_assignee,
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


@pytest.fixture(autouse=True)
def _stub_batch_reads(monkeypatch):
    async def _empty_map(self, ids):
        return {}

    async def _empty_list(self, _id):
        return []

    monkeypatch.setattr(web_routes.AssetService, "descriptions_map", _empty_map)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_assignees", _empty_list)
    monkeypatch.setattr(web_routes.WorkOrderService, "assignees_map", _empty_map)


def _settings(**over):
    from cmms.config import Settings

    return Settings(**over)


# ---- 佇列 ----

@pytest.fixture
def _queue(monkeypatch):
    async def _list(self, **kwargs):
        return [
            SimpleNamespace(
                work_order_no=90018, asset_id="EID-70021", work_type="REACTIVE",
                status="IN_PROGRESS", brief_description="Belt slipping, pickup failing",
                downtime_minutes=220, opened_date=date(2026, 7, 1),
                assigned_person="Alice Fang", assigned_vendor=None, hold_reason=None,
            ),
            SimpleNamespace(
                work_order_no=90019, asset_id="EID-70005", work_type="PM",
                status="CLOSED", brief_description=None, downtime_minutes=None,
                opened_date=date(2026, 6, 30), assigned_person=None,
                assigned_vendor=None, hold_reason=None,
            ),
        ]

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)


def test_queue_renders_v2(as_user, _queue):
    r = _get("/app/work-orders")
    assert r.status_code == 200
    _assert_v2(r.text)
    assert 'id="wo-results"' in r.text
    assert 'hx-target="#wo-results"' in r.text
    assert "WO-90018" in r.text and "Belt slipping, pickup failing" in r.text
    assert "ledger__row--prog is-live" in r.text      # 處理中浮起
    assert "ledger__row--done is-flat" in r.text      # 結案壓平
    assert "stamp" in r.text
    assert "3h40m" in r.text                            # 停機時數(同 v1)
    assert "/app/work-orders?scope=all&tab=active" in r.text
    assert 'style="' not in r.text.split('<main class="page">', 1)[1].split("</main>", 1)[0]


def test_queue_parity_with_v1(as_user, _queue):
    for url in ("/app/work-orders", "/app/work-orders?scope=all&tab=all&q=pump"):
        v2 = _get(url).text
        v1 = _get(url, ui="v1").text
        assert _targets(v2) == _targets(v1), url


def test_queue_htmx_partial_is_v2(as_user, _queue):
    r = _get("/app/work-orders?q=no", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "ledger ledger--list" in r.text
    assert "&q=no" in r.text                            # chips 帶著 q


def test_queue_mine_empty_hint(as_user, _queue):
    as_user["user"] = _fake_user(emaint_assignee=None)
    r = _get("/app/work-orders")
    assert r.status_code == 200
    assert "switch to All" in r.text
    assert "/app/work-orders?scope=all&tab=active" in r.text


# ---- 詳情 ----

def _wo(**over):
    base = dict(
        work_order_no=90018, asset_id="EID-70021", work_type="REACTIVE",
        status="IN_PROGRESS", brief_description="Belt slipping, pickup failing",
        downtime_minutes=220, hold_reason=None, action_taken=None, labor_hours=None,
        confirmed_reason_code=None, assigned_vendor=None, created_by="human:jlee",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def detail(monkeypatch):
    state = {"wo": _wo(), "pending": None, "links": [
        SimpleNamespace(id=1, external_key="MRQ-4220", link_type="referenced", title=None),
    ], "parts": [
        SimpleNamespace(id=11, item_code="ES001", quantity=2, source_actor="human:jlee",
                        created_at=datetime(2026, 7, 1)),
    ]}
    notes = [
        SimpleNamespace(id=1, entry_type="report", body="Line stopped.",
                        author="human:jlee", occurred_at=datetime(2026, 7, 1, 9, 12),
                        updated_at=None),
        SimpleNamespace(id=2, entry_type="diagnosis", body="Vacuum pump worn.",
                        author="agent:hermes", occurred_at=datetime(2026, 7, 1, 14, 30),
                        updated_at=None),
        SimpleNamespace(id=3, entry_type="hold", body="Waiting for pump.",
                        author="human:jlee", occurred_at=datetime(2026, 7, 2, 8, 20),
                        updated_at=datetime(2026, 7, 2, 9, 0)),
    ]

    async def _get_wo(self, no):
        return state["wo"] if no == 90018 else None

    async def _notes(self, no):
        return notes

    async def _atts(self, owner_type, owner_ids):
        if "2" in owner_ids:
            return {"2": [SimpleNamespace(id=7, r2_bucket="b", r2_key="work_order_note/2/a.jpg")]}
        return {}

    def _presign(self, att, **kw):
        return (f"memory://{att.r2_key}", 900)

    async def _parts(self, no):
        return state["parts"]

    async def _issue_times(self, no):
        return {}

    async def _links(self, no):
        return state["links"]

    async def _pending(self, **kw):
        return state["pending"]

    async def _holds(self):
        return [SimpleNamespace(code=c, is_downtime=d) for c, d in (
            ("WAITING_PARTS", True), ("TEST_RUN", False), ("OTHER", True),
        )]

    async def _efc(self, asset_id):
        return [SimpleNamespace(code="demo_pickup_fault", descr="Pickup Vacuum Fault",
                                used=True)]

    async def _asset(self, asset_id):
        return SimpleNamespace(asset_id=asset_id, description="Cap Applicator #3")

    svc = web_routes.WorkOrderService
    monkeypatch.setattr(svc, "get_work_order", _get_wo)
    monkeypatch.setattr(svc, "list_notes", _notes)
    monkeypatch.setattr(svc, "get_parts", _parts)
    monkeypatch.setattr(svc, "get_part_issue_times", _issue_times)
    monkeypatch.setattr(svc, "list_external_links", _links)
    monkeypatch.setattr(svc, "find_pending_proposal", _pending)
    monkeypatch.setattr(svc, "list_hold_reasons", _holds)
    monkeypatch.setattr(svc, "list_confirmed_reason_options", _efc)
    monkeypatch.setattr(web_routes.AssetService, "get_asset", _asset)
    monkeypatch.setattr(web_routes.AttachmentService, "attachments_map", _atts)
    monkeypatch.setattr(web_routes.AttachmentService, "presigned_url", _presign)
    return state


_URL = "/app/work-orders/90018"


def test_detail_engineer_v2(as_user, detail):
    r = _get(_URL)
    assert r.status_code == 200
    _assert_v2(r.text)
    t = r.text
    assert 'class="slip slip--prog' in t and "Cap Applicator #3" in t
    for target in ("/notes", "/assign", "/brief", "/transition", "/parts", "/parts/11/update",
                   "/parts/11/cancel", "/links", "/links/1/remove", "/forward",
                   "/notes/1/edit", "/notes/1/delete", "/void"):
        if target == "/void":
            assert f'action="{_URL}/void"' not in t     # 作廢限 admin
            continue
        assert f"{_URL}{target}" in t, target
    assert f"{_URL}/notes/2/edit" not in t               # 別人(agent)的日誌不可改
    assert f"{_URL}/notes/2/photos/7/delete" not in t
    assert "memory://work_order_note/2/a.jpg" in t
    assert 'name="action" value="progress"' in t and 'name="action" value="finish"' in t
    assert 'name="hold_reason" value="WAITING_PARTS"' in t
    assert 'data-suggest="part"' in t and 'data-suggest="person"' in t
    assert 'pattern="[Mm][Rr][Qq]-\\d+"' in t and "MRQ-4220" in t
    assert "3h40m" in t
    assert "status →" in t                               # hold 型日誌的狀態變化標記
    assert f"{_URL}/request-void" not in t               # 同 v1:工程師面無請求作廢鈕
    assert 'style="' not in t.split('<main class="page">', 1)[1].split("</main>", 1)[0]


@pytest.mark.parametrize("role", ["engineer", "admin", "operator"])
@pytest.mark.parametrize("status", ["OPEN", "IN_PROGRESS", "COMPLETED", "CLOSED"])
def test_detail_parity_with_v1(as_user, detail, role, status):
    as_user["user"] = _fake_user(role=role)
    detail["wo"] = _wo(status=status)
    v2 = _get(_URL)
    v1 = _get(_URL, ui="v1")
    assert v2.status_code == v1.status_code == 200
    _assert_v2(v2.text)
    assert _targets(v2.text) == _targets(v1.text)


def test_detail_operator_hides_writes(as_user, detail):
    as_user["user"] = _fake_user(role="operator")
    detail["wo"] = _wo(status="OPEN", created_by="human:jlee")
    t = _get(_URL).text
    for target in ("/notes", "/assign", "/transition", "/parts", "/links", "/forward"):
        assert f'"{_URL}{target}"' not in t, target
    assert f'action="{_URL}/cancel"' in t                # operator 可取消自己開的 OPEN 單


def test_detail_admin_void_and_terminal_reason_edit(as_user, detail):
    as_user["user"] = _fake_user(role="admin")
    t = _get(_URL).text
    assert f'action="{_URL}/void"' in t
    detail["wo"] = _wo(status="CLOSED", confirmed_reason_code="demo_pickup_fault")
    t2 = _get(_URL).text
    assert f'action="{_URL}/confirmed-reason"' in t2
    assert "stamp--round" in t2                          # 結案蓋章
    assert f'action="{_URL}/void"' not in t2


def test_detail_hide_jira(as_user, detail, monkeypatch):
    monkeypatch.setattr(web_routes, "get_settings", lambda: _settings(ui_hide_jira=True))
    t = _get(_URL).text
    assert f"{_URL}/forward" not in t
    assert 'name="external_key"' not in t
    assert f"{_URL}/parts" in t                          # 領料區不受影響
    v1 = _get(_URL, ui="v1").text
    assert _targets(t) == _targets(v1)


@pytest.mark.parametrize(("code", "cls", "text"), [
    ("assign_ok", "ok", None),
    ("brief_err", "err", None),
    ("voidreq_ok", "ok", None),
    ("voidreq_err", "err", None),
    ("void_err", "err", None),
    ("note_deleted", "info", None),
    ("photo_err", "warn", None),
    ("link_needpat", "warn", None),
    ("forward_ok", "ok", "MRQ-9 · "),
])
def test_detail_top_banner(as_user, detail, code, cls, text):
    url = f"{_URL}?msg={code}" + ("&mrq=MRQ-9" if code == "forward_ok" else "")
    t = _get(url).text
    assert f'class="banner banner--{cls}" role="status" data-msg="{code}"' in t
    if text:
        assert text in t


def test_detail_part_banner_is_inline(as_user, detail):
    t = _get(f"{_URL}?msg=part_ok").text
    assert 'class="banner banner--ok banner--inpage" role="status" data-msg="part_ok"' in t
    assert 'class="banner banner--ok" role="status"' not in t   # 不進頁頂
    assert t.index('id="parts"') < t.index('data-msg="part_ok"')


def test_detail_pending_void_banner(as_user, detail):
    detail["pending"] = SimpleNamespace(id=1)
    t = _get(_URL).text
    assert "A void request for this work order is awaiting admin review." in t


# ---- 轉發 MRQ ----

def _dry(nos, summary, description, *, pat=True):
    return ForwardResult(
        dry_run=True, external_key=None,
        work_orders=[ForwardWoSummary(n, note_count=3, photo_count=1) for n in nos],
        total_comments=3 * len(nos), total_photos=len(nos),
        summary=summary, description=description,
        pat_ready=pat, config_ready=True, warnings=[],
    )


@pytest.fixture
def forward(monkeypatch):
    wo = SimpleNamespace(work_order_no=90018, asset_id="EID-70021", status="IN_PROGRESS",
                         brief_description="Belt slipping", opened_at=datetime(2026, 7, 1, 9))
    state = {"pat": True}

    async def _get_wo(self, no):
        return wo if no == 90018 else None

    async def _asset(self, aid):
        return SimpleNamespace(asset_id=aid, description="Cap Applicator #3")

    async def _links(self, no):
        return []

    async def _fwd(self, *, work_order_nos, summary, description, **kw):
        return _dry(work_order_nos, summary, description, pat=state["pat"])

    async def _ready(self, user_id):
        return state["pat"]

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get_wo)
    monkeypatch.setattr(web_routes.AssetService, "get_asset", _asset)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_external_links", _links)
    monkeypatch.setattr(web_routes.JiraSyncService, "forward_work_orders_to_mrq", _fwd)
    monkeypatch.setattr(web_routes.JiraSyncService, "pat_ready", _ready)
    return state


def test_forward_form_v2(as_user, forward):
    r = _get(f"{_URL}/forward?wos=99999")
    assert r.status_code == 200
    _assert_v2(r.text)
    t = r.text
    assert f'action="{_URL}/forward"' in t
    assert 'id="fwd-wos"' in t and 'name="idem_key"' in t
    assert 'id="fwd-desc-wrap"' in t and 'id="fwd-desc"' in t and 'id="fwd-summary"' in t
    assert "WO-99999" in t                               # 查無工單 → 頁內 banner
    assert "WO-90018" in t and ">3<" in t
    assert _targets(t) == _targets(_get(f"{_URL}/forward?wos=99999", ui="v1").text)


def test_forward_pat_missing_disables_submit(as_user, forward):
    forward["pat"] = False
    t = _get(f"{_URL}/forward").text
    assert "/app/settings#pat" in t and "disabled" in t


def test_forward_error_banner(as_user, forward):
    r = client.post(f"{_URL}/forward", data={"summary": " ", "description": "d", "idem_key": "k"},
                    follow_redirects=False)
    assert r.status_code == 200
    _assert_v2(r.text)
    assert 'class="banner banner--err" role="status" data-msg="empty"' in r.text
    assert "both required" in r.text


def _ns_settings(*, hermes: bool) -> SimpleNamespace:
    return SimpleNamespace(
        hermes_configured=hermes, hermes_gateway_url="https://gw.example",
        hermes_gateway_secret="s", jira_forwarder_configured=True, demo_mode=False,
        demo_reset_minutes=30, ui_hide_jira=False, ui_hide_assistant=False,
    )


def test_forward_ai_button_and_partial(as_user, forward, monkeypatch):
    monkeypatch.setattr(web_routes, "get_settings", lambda: _ns_settings(hermes=True))
    t = _get(f"{_URL}/forward").text
    assert f'hx-post="{_URL}/forward/ai-description"' in t
    assert 'hx-target="#fwd-desc-wrap"' in t and 'hx-include="#fwd-wos, #fwd-desc"' in t
    assert _targets(t) == _targets(_get(f"{_URL}/forward", ui="v1").text)

    # gateway 未配置 ⇒ route 直接回 partial(ai_error=disabled),不打外部
    monkeypatch.setattr(web_routes, "get_settings", lambda: _ns_settings(hermes=False))
    p = client.post(f"{_URL}/forward/ai-description", data={"wos": "", "description": "keep"},
                    headers={"HX-Request": "true"})
    assert p.status_code == 200
    assert "<html" not in p.text
    assert 'id="fwd-desc-wrap" class="form-row"' in p.text   # v2 partial
    assert "keep" in p.text
    assert "hx-post" not in p.text                            # 未配置 ⇒ 不渲染 AI 鈕
