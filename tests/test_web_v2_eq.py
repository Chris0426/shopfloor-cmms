"""新版介面 v2:設備 / 供應商 / 匯出 / 設定 / 說明 / 提案 / 查無頁的渲染測試(不需 DB)。

沿用 `test_web_smoke.py` 的假使用者 / 假 session 手法:dependency override + monkeypatch service。
預設不帶 `cmms_ui` cookie ⇒ v2;同一請求以 `cmms_ui=v1` 渲染 v1 做表單目標比對(parity)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.web import routes as web_routes
from cmms.web.help_docs import HELP_DOCS

client = TestClient(app)              # 無 cookie ⇒ v2(預設)
client_v1 = TestClient(app)
client_v1.cookies.set("cmms_ui", "v1")

_HX = {"HX-Request": "true"}
_TARGET_RE = re.compile(r'\b(action|hx-post|hx-get)="([^"]*)"')
_V2_CSS = Path(web_routes.__file__).parent / "static" / "v2.css"



def _targets(html: str) -> set[tuple[str, str]]:
    return set(_TARGET_RE.findall(html))


def _assert_v2(html: str) -> None:
    assert "/static/v2.css" in html
    assert "/static/app.css" not in html



def _fake_user(role: str = "engineer", locale: str = "en") -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="陳工",
        role=role, ui_locale=locale, jira_output_locale="en", is_active=True,
        emaint_assignee="Alice Fang",
    )


@pytest.fixture
def env(monkeypatch):
    holder = {"user": _fake_user()}

    async def _session():
        yield None

    async def _empty_list(self, _id):
        return []

    async def _no_link(self, user_id):
        return None

    async def _no_creds(self, uid):
        return []

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: holder["user"]
    monkeypatch.setattr(web_routes.AssetService, "get_owners", _empty_list)
    monkeypatch.setattr(web_routes.TelegramBridgeService, "get_link", _no_link)
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)
    try:
        yield holder
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


def _both(url: str, headers: dict | None = None) -> tuple[str, str]:
    r2 = client.get(url, headers=headers)
    r1 = client_v1.get(url, headers=headers)
    assert r2.status_code == 200 and r1.status_code == 200
    return r2.text, r1.text


def _page(url: str) -> tuple[str, str]:
    v2, v1 = _both(url)
    _assert_v2(v2)
    assert "/static/app.css" in v1
    return v2, v1


# ---- 設備 ----

def _asset(**kw) -> SimpleNamespace:
    base = dict(
        asset_id="EID-70021", description="Aligner46 打線機", asset_type="Production",
        asset_subtype="WIREBOND", department="EQ", line="Zone B", model_no="BX-820",
        serial_no="SN-1", manufacturer="Kestrel", available_for_service=True, is_active=True,
        site="SITE-X", host_name="h1", asset_ref="AR-9", product=None, weblink=None,
        comments=None, process_segment_class=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def eq(monkeypatch):
    data = {
        "main": _asset(),
        "module": _asset(asset_id="EID-70005", description="Curer9 供料模組",
                         available_for_service=False),
        "usage": [
            SimpleNamespace(item_code="ES001", qty_delta=-2.0, work_order_no=None,
                            occurred_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC), txn_id=2,
                            kind="ISSUE", source_actor="human:jlee"),
            SimpleNamespace(item_code="ES002", qty_delta=-1.0, work_order_no=90018,
                            occurred_at=datetime(2026, 6, 30, 9, 0, tzinfo=UTC), txn_id=1,
                            kind="ISSUE", source_actor="human:jlee"),
            SimpleNamespace(item_code="ES003", qty_delta=3.0, work_order_no=None,
                            occurred_at=datetime(2026, 6, 29, 9, 0, tzinfo=UTC), txn_id=3,
                            kind="RETURN", source_actor="human:jlee"),
        ],
    }
    wo = SimpleNamespace(work_order_no=90018, asset_id="EID-70005", work_type="REACTIVE",
                         status="IN_PROGRESS", brief_description="供料卡料")

    async def _get(self, aid):
        return {"EID-70021": data["main"], "EID-70005": data["module"]}.get(aid)

    async def _list(self, **kw):
        return [data["main"], data["module"]]

    async def _ext(self, aid):
        return [SimpleNamespace(namespace="mes_equipment", external_id=aid)]

    async def _desc(self, aid):
        return ["EID-70005"] if aid == "EID-70021" else []

    async def _rollup(self, aid, **kw):
        return [wo]

    async def _usage(self, ids, **kw):
        return list(data["usage"])

    async def _cancelled(self, ids):
        return set()

    async def _first(self, owner_type, owner_ids):
        return {}

    async def _atts(self, owner_type, owner_id, **kw):
        return []

    async def _types(self):
        return [SimpleNamespace(code="Production", label="Production")]

    async def _depts(self):
        return [SimpleNamespace(code="EQ", label="Equipment Eng.")]

    async def _lines(self):
        return [SimpleNamespace(code="Zone B", label="Zone B")]

    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get)
    monkeypatch.setattr(web_routes.AssetService, "list_assets", _list)
    monkeypatch.setattr(web_routes.AssetService, "list_external_ids", _ext)
    monkeypatch.setattr(web_routes.AssetService, "get_contained_descendants", _desc)
    monkeypatch.setattr(web_routes.AssetService, "rollup_work_orders", _rollup)
    monkeypatch.setattr(web_routes.AssetService, "list_asset_types", _types)
    monkeypatch.setattr(web_routes.AssetService, "list_departments", _depts)
    monkeypatch.setattr(web_routes.AssetService, "list_lines", _lines)
    monkeypatch.setattr(web_routes.InventoryService, "list_asset_part_usage", _usage)
    monkeypatch.setattr(web_routes.InventoryService, "cancelled_asset_issue_ids", _cancelled)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    monkeypatch.setattr(web_routes.AttachmentService, "list_attachments", _atts)
    return data


@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_equipment_list_v2(env, eq, role):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/equipment?q=eid")
    for hook in ('action="/app/equipment"', 'hx-get="/app/equipment"', 'hx-target="#eq-results"',
                 'hx-push-url="true"', 'name="q"', 'name="atype"', 'id="eq-results"',
                 'href="/app/equipment/EID-70021"', "href=\"/app/equipment?atype=all&q=eid\""):
        assert hook in v2, hook
    assert 'class="inv-row eq-row is-flat"' in v2           # available_for_service=False → 壓平
    assert "Out of service" in v2                           # eq.unavailable(en)
    assert ('href="/app/equipment/new"' in v2) is (role == "admin")
    assert _targets(v2) == _targets(v1)


def test_equipment_list_htmx_partial_v2(env, eq):
    v2, _ = _both("/app/equipment?q=eid", headers=_HX)
    assert "<html" not in v2 and "ledger--eq" in v2         # v2 partial,不帶殼
    assert "EID-70021" in v2


def test_equipment_list_adminonly_banner_v2(env, eq):
    v2, _ = _page("/app/equipment?msg=adminonly")
    assert 'class="banner banner--info"' in v2 and 'data-msg="adminonly"' in v2


@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_equipment_detail_v2(env, eq, role):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/equipment/EID-70021")
    for hook in ("Aligner46 打線機", "WIREBOND", 'href="/app/equipment/EID-70005"', "WO-90018",
                 'href="/app/report?eid=EID-70021"', "mes_equipment:EID-70021",
                 'href="/app/inventory/ES001"', "2026-07-01",
                 'href="/app/work-orders?scope=all&tab=all&q=EID-70021"'):
        assert hook in v2, hook
    issue_hooks = ('action="/app/equipment/EID-70021/issue"', 'name="item_code"',
                   'data-suggest="part"', "data-suggest-strict", 'name="nonce"',
                   'action="/app/equipment/EID-70021/issue/2/update"',
                   'action="/app/equipment/EID-70021/issue/2/cancel"')
    for hook in issue_hooks:
        assert (hook in v2) is (role != "operator"), hook
    assert "/issue/1/update" not in v2                      # 工單領料列不可在此改
    admin_hooks = ('action="/app/equipment/EID-70021/flags"', 'name="flag" value="available"',
                   'name="flag" value="active"', 'action="/app/equipment/EID-70021/edit"',
                   'name="asset_ref" value="AR-9"', 'name="site" value="SITE-X"',
                   "data-multi")
    for hook in admin_hooks:
        assert (hook in v2) is (role == "admin"), hook
    assert _targets(v2) == _targets(v1)


def test_equipment_detail_retired_v2(env, eq):
    eq["main"] = _asset(is_active=False, available_for_service=False)
    v2, _ = _page("/app/equipment/EID-70021")
    assert 'href="/app/report?eid=EID-70021"' not in v2
    assert 'disabled aria-disabled="true"' in v2
    assert "slip card-slip is-flat" in v2
    assert "Retired" in v2                                  # eq.inactive(en)


@pytest.mark.parametrize("param,code,cls", [
    ("issue", "ok", "ok"), ("issue", "dup", "info"), ("issue", "upd", "ok"),
    ("issue", "cancelled", "info"), ("issue", "err", "err"),
    ("msg", "saved", "ok"), ("msg", "adminonly", "info"), ("msg", "err", "err"),
    ("msg", "bogus", "warn"),
])
def test_equipment_detail_banners_v2(env, eq, param, code, cls):
    v2, _ = _page(f"/app/equipment/EID-70021?{param}={code}")
    assert f'class="banner banner--{cls}" role="status" data-msg="{code}"' in v2


def test_equipment_detail_err_opens_edit_v2(env, eq):
    env["user"] = _fake_user("admin")
    v2, _ = _page("/app/equipment/EID-70021?msg=err")
    assert '<details class="fold fold--panel" open>' in v2


def test_equipment_not_found_placeholder_v2(env, eq):
    v2, _ = _page("/app/equipment/EID-NOPE")
    assert "EID-NOPE" in v2 and "Not found" in v2           # ui.notfound(en)
    assert "coming soon" not in v2


@pytest.mark.parametrize("msg", [None, "err"])
def test_equipment_new_v2(env, eq, msg):
    env["user"] = _fake_user("admin")
    v2, v1 = _page("/app/equipment/new" + (f"?msg={msg}" if msg else ""))
    for hook in ('action="/app/equipment/new"', 'name="asset_id"', 'pattern="EID-\\d{5}"',
                 'name="site"', 'name="asset_type"', 'name="owners"', 'name="asset_ref"',
                 'name="comments"', "data-multi"):
        assert hook in v2, hook
    assert ('class="banner banner--err"' in v2) is (msg == "err")
    assert _targets(v2) == _targets(v1)


def test_equipment_new_engineer_redirects(env, eq):
    r = client.get("/app/equipment/new", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/equipment?msg=adminonly"


# ---- 供應商 ----

@pytest.fixture
def sup(monkeypatch):
    org = SimpleNamespace(
        org_id="NORDIC", name="NORDIC GmbH", org_type="Supplier", is_active=True,
        website="https://nordic.example.com", address="Munich, DE", phone="+49-89-1",
    )
    gone = SimpleNamespace(
        org_id="OLDCO", name="Old Co", org_type=None, is_active=False,
        website=None, address=None, phone=None,
    )
    person = SimpleNamespace(
        person_id="HANS01", org_id="NORDIC", category="Sales", full_name="Hans Muller",
        first_name="Hans", last_name="Muller", email="hans@nordic.example.com",
        work_phone="+49-89-2", extension="12", mobile="+49-170-9", work_address="Munich",
        is_main=True,
    )

    async def _list_orgs(self, **kw):
        return [org, gone]

    async def _get_org(self, oid):
        return {"NORDIC": org, "OLDCO": gone}.get(oid)

    async def _persons(self, oid):
        return [person]

    async def _org_types(self):
        return [SimpleNamespace(code="Supplier", label="Supplier")]

    async def _cats(self):
        return [SimpleNamespace(code="Sales", label="Sales")]

    monkeypatch.setattr(web_routes.ContactsService, "list_organizations", _list_orgs)
    monkeypatch.setattr(web_routes.ContactsService, "get_organization", _get_org)
    monkeypatch.setattr(web_routes.ContactsService, "list_org_persons", _persons)
    monkeypatch.setattr(web_routes.ContactsService, "list_org_types", _org_types)
    monkeypatch.setattr(web_routes.ContactsService, "list_contact_categories", _cats)


@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_suppliers_list_v2(env, sup, role):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/suppliers")
    for hook in ('action="/app/suppliers"', 'hx-get="/app/suppliers"', 'hx-target="#sup-results"',
                 'id="sup-results"', 'href="/app/suppliers/NORDIC"', "Munich, DE"):
        assert hook in v2, hook
    assert 'class="inv-row sup-row is-flat"' in v2          # 停用機構 → 壓平
    assert ('href="/app/suppliers/new"' in v2) is (role == "admin")
    assert _targets(v2) == _targets(v1)


def test_suppliers_list_htmx_and_banner_v2(env, sup):
    part, _ = _both("/app/suppliers?q=nor", headers=_HX)
    assert "<html" not in part and "ledger--sup" in part
    v2, _ = _page("/app/suppliers?msg=adminonly")
    assert 'class="banner banner--info"' in v2


@pytest.mark.parametrize("role", ["admin", "engineer"])
def test_supplier_detail_v2(env, sup, role):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/suppliers/NORDIC")
    assert "NORDIC GmbH" in v2 and "Hans Muller" in v2
    assert 'href="https://nordic.example.com"' in v2
    admin = role == "admin"
    # PII 只給 admin;engineer 留「僅管理員可見」說明
    assert ("hans@nordic.example.com" in v2) is admin
    assert ("+49-170-9" in v2) is admin
    for hook in ('action="/app/suppliers/NORDIC/active"', 'name="active" value="0"',
                 'action="/app/suppliers/NORDIC/update"', 'action="/app/suppliers/NORDIC/persons"',
                 'action="/app/suppliers/NORDIC/persons/HANS01/update"', 'name="full_name"',
                 'name="is_main" value="true"', 'name="work_address"', 'name="category"'):
        assert (hook in v2) is admin, hook
    assert _targets(v2) == _targets(v1)


@pytest.mark.parametrize("code,cls", [("saved", "ok"), ("adminonly", "info"), ("err", "err")])
def test_supplier_detail_banners_v2(env, sup, code, cls):
    env["user"] = _fake_user("admin")
    v2, _ = _page(f"/app/suppliers/NORDIC?msg={code}")
    assert f'class="banner banner--{cls}" role="status" data-msg="{code}"' in v2
    if code == "err":                                       # 編輯 + 新增兩個 fold 都展開
        assert v2.count('<details class="fold fold--panel" open>') == 2


def test_supplier_detail_inactive_v2(env, sup):
    v2, _ = _page("/app/suppliers/OLDCO")
    assert "slip card-slip is-flat" in v2 and "tag--obsolete" in v2


@pytest.mark.parametrize("msg", [None, "err"])
def test_supplier_new_v2(env, sup, msg):
    env["user"] = _fake_user("admin")
    v2, v1 = _page("/app/suppliers/new" + (f"?msg={msg}" if msg else ""))
    for hook in ('action="/app/suppliers/new"', 'name="name"', 'name="org_type"',
                 'value="Supplier" selected', 'name="website"', 'name="phone"', 'name="address"'):
        assert hook in v2, hook
    assert ('class="banner banner--err"' in v2) is (msg == "err")
    assert _targets(v2) == _targets(v1)


# ---- 匯出中心 ----

@pytest.fixture
def exp(monkeypatch):
    rows = [{"work_order_no": n, "asset_id": "EID-001", "status": "CLOSED"} for n in range(5)]

    async def _opts(self, source):
        return [("Production", "Production"), ("REACTIVE", "Reactive")]

    async def _count(self, **f):
        return 7

    async def _rows(self, *, limit=None, **f):
        return rows[:limit] if limit else rows

    monkeypatch.setattr(web_routes.ExportService, "lookup_options", _opts)
    monkeypatch.setattr(web_routes.ExportService, "count_work_orders", _count)
    monkeypatch.setattr(web_routes.ExportService, "rows_work_orders", _rows)


@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
@pytest.mark.parametrize("ds", ["", "assets"])
def test_export_home_v2(env, exp, role, ds):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/export" + (f"?ds={ds}" if ds else ""))
    slug = ds or "work_orders"
    for hook in ('id="export-panel"', "data-export-form", f'hx-get="/app/export/{slug}/preview"',
                 'hx-target="#export-preview"', 'id="export-preview"',
                 f'href="/app/export?ds={slug}"'):
        assert hook in v2, hook
    assert 'class="ds-card is-active" aria-current="true"' in v2
    assert _targets(v2) == _targets(v1)


def test_export_preview_v2(env, exp):
    v2, _ = _both("/app/export/work_orders/preview?status=CLOSED", headers=_HX)
    assert "<html" not in v2
    assert 'class="table-scroll export-table-wrap"' in v2
    assert 'href="/app/export/work_orders/download?status=CLOSED"' in v2 and " download>" in v2
    bad, _ = _both("/app/export/work_orders/preview?opened_from=2026-13-99", headers=_HX)
    assert 'class="banner banner--err banner--inpage"' in bad and 'data-msg="baddate"' in bad


# ---- 設定 ----

@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_settings_v2(env, role):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/settings")
    for hook in ('action="/app/settings/password"', 'name="current_password"',
                 'name="new_password"', 'action="/app/settings/assignee"', 'value="Alice Fang"',
                 'data-suggest="person"',
                 'action="/app/settings/pat"', 'name="pat_secret"', 'id="pat"',
                 'action="/app/settings/telegram/code"', 'href="/app/proposals"',
                 'href="/app/export"', 'href="/app/suppliers"', 'href="/app/help"',
                 "/app/set-locale?locale=zh-TW&next=/app/settings"):
        assert hook in v2, hook
    assert _targets(v2) == _targets(v1)


def test_settings_changed_banner_v2(env):
    v2, _ = _page("/app/settings?changed=1")
    assert 'class="banner banner--ok" role="status" data-msg="1"' in v2


@pytest.mark.parametrize("code,cls", [
    ("saved", "ok"), ("revoked", "info"), ("keyunset", "err"), ("keyinvalid", "err"),
    ("empty", "err"), ("error", "err"), ("bogus", "warn"),
])
def test_settings_pat_banner_v2(env, code, cls):
    v2, _ = _page(f"/app/settings?pat={code}")
    assert f'class="banner banner--{cls} banner--inpage" role="status" data-msg="{code}"' in v2


def test_settings_pat_and_telegram_bound_v2(env, monkeypatch):
    async def _one(self, uid):
        return [SimpleNamespace(system="jira", label=None,
                                created_at=datetime(2026, 7, 1, 4, 0, tzinfo=UTC),
                                last_used_at=None)]

    async def _link(self, user_id):
        return SimpleNamespace(user_id=user_id, chat_id="123456789")

    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _one)
    monkeypatch.setattr(web_routes.TelegramBridgeService, "get_link", _link)
    v2, v1 = _page("/app/settings")
    for hook in ('action="/app/settings/pat/revoke"', "2026-07-01", "Replace token",
                 'action="/app/settings/telegram/unlink"', "····6789"):
        assert hook in v2, hook
    assert v2.count("return confirm(") == 2
    assert _targets(v2) == _targets(v1)


def test_settings_telegram_code_v2(env, monkeypatch):
    async def _mk(self, user_id, actor):
        return "CODE-XYZ-123"

    monkeypatch.setattr(web_routes.TelegramBridgeService, "create_link_code", _mk)
    r = client.post("/app/settings/telegram/code")
    assert r.status_code == 200
    _assert_v2(r.text)
    assert 'class="tg-code">CODE-XYZ-123' in r.text and "?start=CODE-XYZ-123" in r.text


def test_settings_password_error_v2(env, monkeypatch):
    from cmms.domain.identity.service import AuthenticationError

    async def _cp(self, uid, old, new, *, actor):
        raise AuthenticationError("nope")

    monkeypatch.setattr(web_routes.IdentityService, "change_password", _cp)
    r = client.post("/app/settings/password", data={"current_password": "x", "new_password": "y"})
    assert r.status_code == 200
    _assert_v2(r.text)
    assert 'class="form-hint form-hint--err" role="alert"' in r.text
    assert 'aria-invalid="true"' in r.text


# ---- 我的提案 ----

@pytest.fixture
def props(monkeypatch):
    holder = {"rows": [
        SimpleNamespace(
            pending_token="t1", operation="update_item", params={"item_code": "ES900001"},
            dry_run_diff={"changes": {"bin_location": {"from": "A1", "to": "B2"}}},
            status="PENDING", confirmed_by=None,
            created_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
            expires_at=datetime(2030, 1, 1, tzinfo=UTC), resolved_at=None,
        ),
        SimpleNamespace(
            pending_token="t2", operation="void_work_order",
            params={"work_order_no": 90018, "reason": "duplicate report"},
            dry_run_diff=None, status="CONFIRMED", confirmed_by="human:admin",
            created_at=datetime(2026, 7, 4, 9, 0, tzinfo=UTC),
            expires_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
            resolved_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
        ),
        SimpleNamespace(
            pending_token="t4", operation="update_item", params={"item_code": "ES900002"},
            dry_run_diff=None, status="PENDING", confirmed_by=None,
            created_at=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
            expires_at=datetime(2026, 7, 2, 10, 0, tzinfo=UTC), resolved_at=None,
        ),
    ]}

    async def _list(self, *, proposed_by, limit, offset):
        return list(holder["rows"])

    monkeypatch.setattr(web_routes.WorkOrderService, "list_proposals_by_proposer", _list)
    return holder


def test_proposals_v2(env, props):
    v2, v1 = _page("/app/proposals")
    for hook in ("Pending review", "Approved", "Expired", 'href="/app/inventory/ES900001"',
                 'href="/app/work-orders/90018"', "bin_location", "duplicate report",
                 "tag--prop-prog", "tag--prop-done", "tag--prop-muted", "tag--prop-red"):
        assert hook in v2, hook
    assert _targets(v2) == _targets(v1)


def test_proposals_empty_v2(env, props):
    props["rows"] = []
    v2, _ = _page("/app/proposals")
    assert "submitted any proposals." in v2


# ---- 說明中心 ----

@pytest.mark.parametrize("role", ["admin", "engineer", "operator"])
def test_help_index_v2(env, role):
    env["user"] = _fake_user(role)
    v2, v1 = _page("/app/help")
    for doc in HELP_DOCS:
        assert f'href="/app/help/{doc.slug}"' in v2 and doc.title in v2
    assert 'action="/app/help/feedback"' in v2 and 'name="message"' in v2
    assert _targets(v2) == _targets(v1)


@pytest.mark.parametrize("fb,cls", [("ok", "ok"), ("empty", "err"), ("bogus", "warn")])
def test_help_feedback_banner_v2(env, fb, cls):
    v2, _ = _page(f"/app/help?fb={fb}")
    assert f'class="banner banner--{cls}" role="status" data-msg="{fb}"' in v2


def _css_classes() -> set[str]:
    css = re.sub(r"/\*.*?\*/", "", _V2_CSS.read_text(encoding="utf-8"), flags=re.S)
    return set(re.findall(r"\.(-?[_a-zA-Z][\w-]*)", css))


@pytest.mark.parametrize("slug", [d.slug for d in HELP_DOCS])
@pytest.mark.parametrize("locale", ["zh-TW", "en"])
def test_help_doc_v2_fragment_classes(env, slug, locale):
    """v2 殼 include 原 v1 片段;片段用到的每個 class 都在 v2.css 有規則(SOP 片段層)。"""
    env["user"] = _fake_user(locale=locale)
    v2, v1 = _page(f"/app/help/{slug}")
    assert 'class="sheet sheet--card sheet--doc"' in v2 and '<div class="sop">' in v2
    assert ("sop-zh-only" in v2) is (locale != "zh-TW")
    start = v2.index('<div class="sop">')
    frag = v2[start:v2.index("</main>")]
    used = {c for m in re.findall(r'class="([^"]*)"', frag) for c in m.split()}
    assert used - _css_classes() == set()
    assert _targets(v2) == _targets(v1)
