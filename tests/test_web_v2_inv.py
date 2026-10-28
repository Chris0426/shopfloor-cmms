"""新版介面 v2:報修 / 備品 / 助理 dock partial 的渲染測試(不需 DB)。

沿用 `test_web_smoke.py` 的假使用者 / 假 session 手法:dependency override + monkeypatch service。
預設不帶 `cmms_ui` cookie ⇒ v2;同一請求以 `cmms_ui=v1` 渲染 v1 做表單目標比對(parity)。
"""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.domain.work_order.service import WorkOrderError
from cmms.web import routes as web_routes

client = TestClient(app)              # 無 cookie ⇒ v2(預設)
client_v1 = TestClient(app)
client_v1.cookies.set("cmms_ui", "v1")

_TARGET_RE = re.compile(r'\b(action|hx-post|hx-get)="([^"]*)"')


def _targets(html: str) -> set[tuple[str, str]]:
    return set(_TARGET_RE.findall(html))


def _assert_v2(html: str) -> None:
    assert "/static/v2.css" in html
    assert "/static/app.css" not in html


def _fake_user(role: str = "engineer", locale: str = "en") -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="陳工",
        role=role, ui_locale=locale, jira_output_locale="en", is_active=True,
        emaint_assignee=None,
    )


class _FS:
    """假 session:只給報修 route 的 `session.get(Asset, eid)` 用。"""

    def __init__(self, holder):
        self.holder = holder

    async def get(self, model, key):
        return SimpleNamespace() if self.holder["asset_exists"] else None


@pytest.fixture
def env(monkeypatch):
    holder = {"user": _fake_user(), "asset_exists": True, "owners": ["Owner Bob"]}

    async def _session():
        yield _FS(holder)

    async def _owners(self, eid):
        return list(holder["owners"])

    async def _bins(self, include_inactive=False):
        return [SimpleNamespace(code="A-12")]

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: holder["user"]
    monkeypatch.setattr(web_routes.AssetService, "get_owners", _owners)
    monkeypatch.setattr(web_routes.InventoryService, "list_storage_bins", _bins)
    try:
        yield holder
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


def _both(url: str) -> tuple[str, str]:
    r2 = client.get(url)
    r1 = client_v1.get(url)
    assert r2.status_code == 200 and r1.status_code == 200
    _assert_v2(r2.text)
    assert "/static/app.css" in r1.text
    return r2.text, r1.text


# ---- 報修 ----

@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_report_form_v2(env, role):
    env["user"] = _fake_user(role)
    v2, v1 = _both("/app/report?eid=EID-70021")
    for hook in ('action="/app/report"', 'enctype="multipart/form-data"', 'name="asset_id"',
                 'id="asset_id"', 'data-suggest="asset"', "data-suggest-strict",
                 'data-owner-autofill="report-owners"', 'id="report-owners"', 'name="assignees"',
                 'name="brief_description"', 'name="photos"', 'id="scan-btn"', 'id="scan-video"',
                 'value="EID-70021"'):
        assert hook in v2, hook
    assert 'role="alert"' not in v2                    # 無錯誤 ⇒ 不佔位
    assert _targets(v2) == _targets(v1)


def _stub_open(monkeypatch, *, fail: bool = False):
    async def _open(self, **kw):
        if fail:
            raise WorkOrderError("retired")
        return SimpleNamespace(work_order_no=999)

    monkeypatch.setattr(web_routes.WorkOrderService, "open_work_order", _open)


@pytest.mark.parametrize("case,text", [
    ("notfound", "not found"),
    ("owner_missing", "no owner set"),
    ("retired", None),
])
def test_report_post_errors_v2(env, monkeypatch, case, text):
    from cmms.web.i18n import translate

    if case == "notfound":
        env["asset_exists"] = False
    elif case == "owner_missing":
        env["owners"] = []
    _stub_open(monkeypatch, fail=(case == "retired"))
    data = {"asset_id": "EID-70021", "brief_description": "leak"}
    r = client.post("/app/report", data=data)
    assert r.status_code == 200
    _assert_v2(r.text)
    assert "banner--err" in r.text and 'role="alert"' in r.text
    key = {"notfound": "report.error", "owner_missing": "report.owner_missing",
           "retired": "report.retired"}[case]
    assert translate(key, "en") in r.text.replace("&#39;", "'")
    if text:
        assert text in r.text.lower()
    assert "leak" in r.text                             # 已填值回填
    assert "is-err" in r.text
    r1 = client_v1.post("/app/report", data=data)
    assert _targets(r.text) == _targets(r1.text)


# ---- 備品 ----

@pytest.fixture
def inv(env, monkeypatch):
    item = SimpleNamespace(
        item_code="ES900004", name="Demo valve", description="desc",
        vendor_part_no="VP-1", quantity_on_hand=2, reorder_point=5,
        bin_location="A-12", supplier="CMB", supplier_org_id="CMB", reorder_quantity=12.0,
        unit_cost=None, lead_time_weeks=None, weblink=None, comment=None,
        is_stocked=True, is_obsolete=False,
    )
    state = {"pending": False}

    async def _list(self, **kw):
        return [item] if kw.get("search") or kw.get("below_reorder") else []

    async def _get(self, code):
        return item if code == "ES900004" else None

    async def _alts(self, code):
        return ["ES900005"]

    async def _kit(self, code):
        return ["ES900009"]

    async def _parents(self, code):
        return ["ES000999"]

    async def _subs(self, code):
        return ["DISPENSER"]

    async def _all_subs(self):
        return [SimpleNamespace(code="DISPENSER"), SimpleNamespace(code="VALVE")]

    async def _list_atts(self, owner_type, owner_id, **kw):
        return [SimpleNamespace(id=1, r2_bucket="b", r2_key="inventory/ES900004/x.jpg")]

    def _presign(self, att, **kw):
        return (f"memory://{att.r2_key}", 900)

    async def _first(self, owner_type, owner_ids):
        return {}

    async def _org(self, oid):
        return SimpleNamespace(org_id=oid, name="CMB Corp")

    async def _pending(self, **kw):
        return object() if state["pending"] else None

    monkeypatch.setattr(web_routes.InventoryService, "list_items", _list)
    monkeypatch.setattr(web_routes.InventoryService, "get_item", _get)
    monkeypatch.setattr(web_routes.InventoryService, "get_alternatives", _alts)
    monkeypatch.setattr(web_routes.InventoryService, "get_kit_children", _kit)
    monkeypatch.setattr(web_routes.InventoryService, "get_applicable_subtypes", _subs)
    monkeypatch.setattr(web_routes.InventoryService, "get_parent_kits", _parents)
    monkeypatch.setattr(web_routes.InventoryService, "list_all_asset_subtypes", _all_subs)
    monkeypatch.setattr(web_routes.ContactsService, "get_organization", _org)
    monkeypatch.setattr(web_routes.AttachmentService, "list_attachments", _list_atts)
    monkeypatch.setattr(web_routes.AttachmentService, "presigned_url", _presign)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    monkeypatch.setattr(web_routes.WorkOrderService, "find_pending_proposal", _pending)
    return state


@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
@pytest.mark.parametrize("query", ["?q=VALVE", "?low=1", ""])
def test_inventory_list_v2(env, inv, role, query):
    env["user"] = _fake_user(role)
    v2, v1 = _both("/app/inventory" + query)
    assert 'id="inv-results"' in v2
    assert 'hx-target="#inv-results"' in v2 and 'hx-push-url="true"' in v2
    assert 'href="/app/suppliers"' in v2
    assert ("/app/inventory/new" in v2) == (role == "admin")
    assert ('href="/app/inventory-rfq"' in v2) == (role == "admin" and query == "?low=1")
    if query:
        assert "ES900004" in v2 and "tag--restock" in v2          # 2 < 5 ⇒ 紅籤
    else:
        assert "ES900004" not in v2
    if query == "?low=1":
        assert 'name="low" value="1"' in v2
    assert _targets(v2) == _targets(v1)


def test_inventory_list_htmx_partial_v2(env, inv):
    r = client.get("/app/inventory?q=VALVE", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text                                   # 只回結果區
    assert "inv-row" in r.text and "ES900004" in r.text


def test_inventory_list_adminonly_banner_v2(env, inv):
    r = client.get("/app/inventory?msg=adminonly")
    _assert_v2(r.text)
    assert 'class="banner banner--info"' in r.text and 'data-msg="adminonly"' in r.text


@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_inventory_detail_v2(env, inv, role):
    env["user"] = _fake_user(role)
    v2, v1 = _both("/app/inventory/es900004")
    assert "ES900004" in v2 and "ES900005" in v2 and "ES900009" in v2 and "ES000999" in v2
    assert "inventory/ES900004/x.jpg" in v2
    assert 'href="/app/suppliers/CMB"' in v2
    if role == "admin":
        for a in ("/rfq", "/update", "/subtypes", "/adjust", "/photos", "/photos/1/delete"):
            assert f'action="/app/inventory/ES900004{a}"' in v2, a
        assert 'name="nonce"' in v2 and 'list="adjust-reasons"' in v2
        assert 'name="subtypes" multiple' in v2
        assert 'data-suggest="supplier"' in v2 and 'data-fill="edit-org-id"' in v2
        assert 'id="edit-org-id"' in v2 and 'data-add-url="/app/inventory/bins"' in v2
        assert 'name="is_stocked"' in v2 and 'name="is_obsolete"' in v2
        assert "propose-update" not in v2
    else:
        assert 'action="/app/inventory/ES900004/propose-update"' in v2
        assert "/app/inventory/ES900004/update" not in v2
        assert "data-add-url" not in v2
    assert _targets(v2) == _targets(v1)


def test_inventory_detail_pending_hides_proposal_v2(env, inv):
    inv["pending"] = True
    v2, v1 = _both("/app/inventory/ES900004")
    assert "propose-update" not in v2
    assert "banner--info banner--inpage" in v2
    assert _targets(v2) == _targets(v1)


@pytest.mark.parametrize("code,cls", [
    ("sent", "ok"), ("drafted", "warn"), ("failed", "err"), ("nosupplier", "info"),
    ("saved", "ok"), ("editerr", "err"), ("nochange", "info"), ("proposed", "ok"),
    ("photo_ok", "ok"), ("photo_err", "err"),
])
def test_inventory_detail_banners_v2(env, inv, code, cls):
    from cmms.web.i18n import translate

    env["user"] = _fake_user("admin")
    r = client.get(f"/app/inventory/ES900004?rfq={code}")
    assert r.status_code == 200
    assert f'class="banner banner--{cls}"' in r.text and f'data-msg="{code}"' in r.text
    assert translate(f"inv.rfq.{code}", "en").replace("'", "&#39;") in r.text


@pytest.mark.parametrize("msg", [None, "err"])
def test_inventory_new_v2(env, msg):
    env["user"] = _fake_user("admin")
    v2, v1 = _both("/app/inventory/new" + (f"?msg={msg}" if msg else ""))
    for hook in ('action="/app/inventory/new"', 'enctype="multipart/form-data"',
                 'name="item_code"', 'name="initial_quantity"', 'name="photos"', 'name="nonce"',
                 'name="name"', 'name="bin_location"', 'name="is_stocked" value="true" checked'):
        assert hook in v2, hook
    assert ('class="banner banner--err"' in v2) == (msg == "err")
    assert _targets(v2) == _targets(v1)


def test_inventory_new_engineer_redirects(env):
    r = client.get("/app/inventory/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/inventory?msg=adminonly"


@pytest.fixture
def drafts(monkeypatch):
    from cmms.domain.procurement.service import RfqDraft

    async def _drafts(self, **kw):
        return [RfqDraft(supplier_org_id="CMB", supplier_name="CMB Corp",
                         recipient_email=None,
                         lines=[("ES001", Decimal("12")), ("ES002", Decimal("3"))])]

    monkeypatch.setattr(web_routes.ProcurementService, "draft_below_safety_stock", _drafts)


@pytest.mark.parametrize("sent,cls", [(None, None), ("sent", "ok"), ("drafted", "warn"),
                                      ("failed", "err")])
def test_inventory_rfq_batch_v2(env, drafts, sent, cls):
    env["user"] = _fake_user("admin")
    v2, v1 = _both("/app/inventory-rfq" + (f"?sent={sent}" if sent else ""))
    assert "CMB Corp" in v2 and 'value="ES001,ES002"' in v2
    assert 'action="/app/inventory-rfq"' in v2 and 'name="supplier_org_id" value="CMB"' in v2
    assert 'href="/app/inventory/ES001"' in v2
    if cls:
        assert f'class="banner banner--{cls}"' in v2 and f'data-msg="{sent}"' in v2
    else:
        assert "data-msg=" not in v2
    assert _targets(v2) == _targets(v1)


# ---- 助理 dock partial ----

def _hermes(configured: bool = True):
    return SimpleNamespace(
        hermes_gateway_url="http://gw" if configured else None,
        hermes_gateway_secret="S" if configured else None,
        hermes_configured=configured,
        demo_mode=False, demo_reset_minutes=30, ui_hide_jira=False, ui_hide_assistant=False,
    )


def _patch_panel(monkeypatch, convs, msgs):
    async def _list_open(self, user_id):
        return convs

    async def _get_conv(self, user_id, cid):
        return convs[0] if convs else None

    async def _msgs(self, user_id, cid):
        return msgs

    monkeypatch.setattr(web_routes.AssistantService, "list_open_conversations", _list_open)
    monkeypatch.setattr(web_routes.AssistantService, "get_conversation", _get_conv)
    monkeypatch.setattr(web_routes.AssistantService, "get_messages", _msgs)


def test_assistant_panel_v2(env, monkeypatch):
    conv = SimpleNamespace(id=5, title="pump leak", closed_at=None)
    _patch_panel(monkeypatch, [conv], [
        SimpleNamespace(id=1, role="user", content="<b>hi</b>"),
        SimpleNamespace(id=2, role="assistant",
                        content="see [EID-70021](/app/equipment/EID-70021)"),
        SimpleNamespace(id=3, role="user", content="still leaking"),
    ])
    r = client.get("/app/assistant/panel")
    r1 = client_v1.get("/app/assistant/panel")
    assert r.status_code == 200
    for hook in ('id="assistant-sessions"', 'id="assistant-conv"', 'id="assistant-form"',
                 'id="assistant-conv-id"', 'hx-post="/app/assistant"',
                 'hx-get="/app/assistant/5"', 'hx-post="/app/assistant/5/close"',
                 'hx-post="/app/assistant/5/reply"', '{"message_id": 3}',
                 "data-assistant-pending", 'class="dock__field"', 'value="5"'):
        assert hook in r.text, hook
    assert "&lt;b&gt;hi&lt;/b&gt;" in r.text
    assert '<a href="/app/equipment/EID-70021">EID-70021</a>' in r.text
    assert " hidden" not in r.text.split('id="assistant-conv"')[0]  # 有會話 ⇒ 切換列可見
    assert _targets(r.text) == _targets(r1.text)


def test_assistant_panel_empty_v2(env, monkeypatch):
    _patch_panel(monkeypatch, [], [])
    r = client.get("/app/assistant/panel?new=1")
    assert r.status_code == 200
    assert 'id="assistant-sessions"' in r.text and "hidden" in r.text
    assert 'style=' not in r.text
    assert 'name="conversation_id" value=""' in r.text and "dock__empty" in r.text
    assert "/close" not in r.text


def test_assistant_turn_v2(env, monkeypatch):
    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes())

    async def _count(self, user_id):
        return 0

    async def _add_user(self, *, user_id, conversation_id, content, actor):
        return SimpleNamespace(id=77, title=content), SimpleNamespace(id=101)

    _patch_panel(monkeypatch, [SimpleNamespace(id=77, title="hello there", closed_at=None)], [])
    monkeypatch.setattr(web_routes.AssistantService, "count_open_conversations", _count)
    monkeypatch.setattr(web_routes.AssistantService, "add_user_message", _add_user)
    r = client.post("/app/assistant", data={"message": "hello there", "conversation_id": ""})
    assert r.status_code == 200
    assert 'class="chat-msg chat-msg--user">hello there</div>' in r.text
    assert 'hx-post="/app/assistant/77/reply"' in r.text and '{"message_id": 101}' in r.text
    assert 'id="assistant-sessions"' in r.text and 'hx-swap-oob="true"' in r.text
    assert 'id="assistant-conv-id"' in r.text and 'value="77"' in r.text
    assert "新版介面 v2" not in r.text                     # Jinja 註解不外洩


@pytest.mark.parametrize("configured,expect", [(False, "enabled yet")])
def test_assistant_reply_disabled_v2(env, monkeypatch, configured, expect):
    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes(configured))
    r = client.post("/app/assistant", data={"message": "hi", "conversation_id": ""})
    assert r.status_code == 200 and expect in r.text and "chat-msg--sys" in r.text


def test_assistant_reply_error_retry_v2(env, monkeypatch):
    import httpx as _httpx

    class _Err:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise _httpx.ConnectError("boom")

    async def _mint(self, **kw):
        return "TOK"

    async def _get_conv(self, user_id, cid):
        return SimpleNamespace(id=77, closed_at=None)

    async def _get_msg(self, user_id, mid):
        return SimpleNamespace(id=101, conversation_id=77, role="user", content="hi")

    async def _none(self, *a, **kw):
        return None

    async def _hist(self, *a, **kw):
        return []

    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes())
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _Err)
    monkeypatch.setattr(web_routes.IdentityService, "mint_scoped_token", _mint)
    monkeypatch.setattr(web_routes.AssistantService, "get_conversation", _get_conv)
    monkeypatch.setattr(web_routes.AssistantService, "get_message", _get_msg)
    monkeypatch.setattr(web_routes.AssistantService, "next_message_after", _none)
    monkeypatch.setattr(web_routes.AssistantService, "recent_history", _hist)
    r = client.post("/app/assistant/77/reply", data={"message_id": "101"})
    assert r.status_code == 200
    assert "chat-error" in r.text and "dock__retry" in r.text
    assert 'hx-post="/app/assistant/77/reply"' in r.text and '{"message_id": 101}' in r.text
    assert 'hx-target="closest .chat-error"' in r.text
    assert "TOK" not in r.text
