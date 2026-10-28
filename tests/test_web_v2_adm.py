"""新版介面 v2:保養(PM)頁 + admin 後台的渲染測試(不需 DB)。

沿用 `test_web_smoke.py` 的假使用者 / 假 session 手法:dependency override + monkeypatch service。
預設不帶 `cmms_ui` cookie ⇒ v2;同一請求以 `cmms_ui=v1` 渲染 v1 做表單目標比對(parity)。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.web import admin_routes as _adm
from cmms.web import i18n
from cmms.web import routes as web_routes

client = TestClient(app)              # 無 cookie ⇒ v2(預設)
client_v1 = TestClient(app)
client_v1.cookies.set("cmms_ui", "v1")

_TARGET_RE = re.compile(r'\b(action|hx-post|hx-get)="([^"]*)"')
_ADM_NAV = [
    "/admin/accounts", "/admin/pm", "/admin/proposals", "/admin/audit", "/admin/attachments",
    "/admin/credentials", "/admin/vocab", "/admin/notify", "/admin/relationships",
    "/admin/owners",
]


def _targets(html: str) -> set[tuple[str, str]]:
    return set(_TARGET_RE.findall(html))


def _assert_v2(html: str) -> None:
    assert "/static/v2.css" in html
    assert "/static/app.css" not in html


def _fake_user(role: str = "engineer", assignee: str | None = "Alice Fang") -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="Jordan Lee",
        role=role, ui_locale="en", jira_output_locale="en", is_active=True,
        emaint_assignee=assignee,
    )


@pytest.fixture
def env(monkeypatch):
    holder = {"user": _fake_user()}

    async def _session():
        yield None

    async def _empty(self, *a, **kw):
        return []

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: holder["user"]
    # /admin/proposals 同頁的使用者回饋區預設空
    monkeypatch.setattr(web_routes.FeedbackService, "list_open", _empty)
    monkeypatch.setattr(web_routes.FeedbackService, "list_recent_resolved", _empty)
    try:
        yield holder
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


def _as(env, role: str, **kw) -> None:
    env["user"] = _fake_user(role, **kw)


def _both(url: str, headers: dict | None = None) -> tuple[str, str]:
    r2 = client.get(url, headers=headers)
    r1 = client_v1.get(url, headers=headers)
    assert r2.status_code == 200, url
    assert r1.status_code == 200, url
    return r2.text, r1.text


def _parity(url: str, headers: dict | None = None) -> str:
    v2, v1 = _both(url, headers)
    _assert_v2(v2)
    assert "/static/app.css" in v1
    assert _targets(v2) == _targets(v1), url
    return v2


# ======================== 保養(PM):到期清單 / 行事曆 / 補開確認 ========================

def _due_pm(pm_id: str = "PM-DUE", **kw) -> SimpleNamespace:
    base = dict(
        pm_id=pm_id, asset_id="EID-1", task_id="T-1",
        next_due_date=date(2020, 1, 1), frequency_interval=30, frequency_unit="Days",
        assigned_person=None, effective_assignee="Alice Fang", last_work_order_no=None,
        is_suppressed=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def pm_data(monkeypatch):
    pm = _due_pm()
    wo = SimpleNamespace(
        work_order_no=6001, asset_id="EID-1", work_type="PM", status="OPEN",
        brief_description="Quarterly", opened_date=date(2020, 1, 1),
    )

    async def _list(self, **kw):
        return [pm]

    async def _get(self, pm_id):
        return pm if pm_id == "PM-DUE" else None

    async def _task(self, tid):
        return SimpleNamespace(description="Quarterly service")

    async def _wos(self, **kw):
        return [wo]

    async def _steps(self, tn):
        return [SimpleNamespace(id=1, proc_seq=10, task_desc="Clean the belt")]

    async def _parts(self, ids):
        return {1: [SimpleNamespace(item_code="EC900007", replace_qty=Decimal("2"))]}

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _list)
    monkeypatch.setattr(web_routes.PmScheduleService, "get_pm_schedule", _get)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)
    monkeypatch.setattr(web_routes.TaskService, "get_task_steps", _steps)
    monkeypatch.setattr(web_routes.TaskService, "get_parts_for_steps", _parts)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _wos)
    return pm


def test_pm_due_list_engineer(env, pm_data):
    html = _parity("/app/pm?scope=all")
    assert "ledger ledger--list ledger--pm" in html
    assert 'href="/app/pm/PM-DUE/backfill"' in html          # 已到期且本期未生成 → 補開鈕
    assert "btn btn--secondary pm-exec" in html               # 清單模式 = 描邊
    assert i18n.translate("pm.overdue", "en") in html         # 逾期 = armed 標
    assert 'class="armed pm-due__over"' in html
    assert i18n.translate("pm.backfill.hint", "en") in html
    assert 'href="/app/pm?focus=PM-DUE&scope=all"' in html
    assert 'href="/admin/pm"' not in html                     # engineer 不見後台入口
    assert 'href="/app/pm/browse"' not in html                # 公開版無「全部排程」頁


def test_pm_due_list_admin_entry_and_operator_hides_backfill(env, pm_data):
    _as(env, "admin")
    html = _parity("/app/pm?scope=all")
    assert 'href="/admin/pm"' in html
    _as(env, "operator")
    html = _parity("/app/pm?scope=all")
    assert "/app/pm/PM-DUE/backfill" not in html              # operator 無補開權


def test_pm_focus_detail(env, pm_data):
    html = _parity("/app/pm?focus=PM-DUE")
    assert "is-focus" in html
    assert "btn btn--primary pm-exec" in html                 # 單項詳情 = 實填
    assert i18n.translate("pm.focus.showall", "en") in html
    assert "Clean the belt" in html
    assert 'href="/app/inventory/EC900007"' in html
    assert i18n.translate("pm.parts_summary", "en") in html


def test_pm_mine_empty_without_assignee(env, pm_data):
    _as(env, "engineer", assignee=None)
    html = _parity("/app/pm")
    assert i18n.translate("scope.empty", "en") in html
    assert 'href="/app/pm?scope=all"' in html


@pytest.mark.parametrize("view", ["month", "week", "day"])
def test_pm_calendar_views(env, pm_data, view):
    html = _parity(f"/app/pm/calendar?view={view}&d=2020-01-01&scope=all")
    assert 'class="cal-nav"' in html
    assert "/app/work-orders/6001" in html
    if view == "month":
        assert 'class="cal-grid"' in html
        assert "focus=PM-DUE" in html
    else:
        assert 'class="cal-rows"' in html
        assert "Quarterly service" in html
    assert ("/app/pm/PM-DUE/backfill" in html) == (view == "day")


def test_pm_backfill_confirm(env, pm_data):
    html = _parity("/app/pm/PM-DUE/backfill")
    assert 'action="/app/pm/PM-DUE/generate"' in html
    assert 'id="assigned_person" name="assigned_person"' in html
    assert 'data-suggest="person"' in html
    assert 'href="/app/pm"' in html                           # 取消
    assert "Alice Fang" in html                               # 有效負責人預填說明
    assert "pm.caution" not in html


# ======================== admin 後台 ========================

def _assert_admin_shell(html: str, active: str) -> None:
    _assert_v2(html)
    assert '<body class="admin">' in html
    nav = html[html.index('class="adm-nav"'):html.index("</nav>", html.index('class="adm-nav"'))]
    assert re.findall(r'href="([^"]+)"', nav) == _ADM_NAV     # 連結 / 順序同 v1
    assert "/admin/costs" not in html and "/admin/consumables" not in html
    assert f'href="{active}" class="is-active" aria-current="page"' in html
    assert 'href="/app"' in html and 'href="/app/logout"' in html
    assert 'id="assistant-dock"' not in html                  # admin 殼無 dock


@pytest.fixture
def adm_data(monkeypatch):
    """所有 admin 頁需要的 service 讀取(SimpleNamespace 假資料)。"""
    async def _users(self, **kw):
        return [
            SimpleNamespace(user_id="ben.yeh", username="ben.yeh", display_name="Ben Yeh",
                            org="contractor", role="engineer", is_active=True,
                            emaint_assignee="Ben Yeh"),
            SimpleNamespace(user_id="old", username="old", display_name="Old User",
                            org="plant", role="operator", is_active=False, emaint_assignee=None),
        ]

    async def _tasks(self, **kw):
        return [SimpleNamespace(task_no="TSK0007", description="Sorter PM", is_active=True)]

    async def _task(self, tn):
        return SimpleNamespace(task_no=tn, description="Sorter PM", is_active=True) \
            if tn == "TSK0007" else None

    async def _steps(self, tn):
        return [SimpleNamespace(id=1, proc_seq=10, task_desc="Clean the head"),
                SimpleNamespace(id=2, proc_seq=20, task_desc="Replace the blade")]

    async def _parts(self, ids):
        return {2: [SimpleNamespace(item_code="EC900007", replace_qty=Decimal("2"))]}

    async def _scheds(self, **kw):
        return [
            SimpleNamespace(pm_id="P1", asset_id="EID-001", task_id="TSK0007",
                            frequency_interval=3, frequency_unit="Months",
                            next_due_date=date(2026, 8, 1), assigned_person="Alice Fang",
                            is_suppressed=False),
            SimpleNamespace(pm_id="P2", asset_id="EID-002", task_id="TSK0007",
                            frequency_interval=1, frequency_unit="Weeks",
                            next_due_date=None, assigned_person=None, is_suppressed=True),
        ]

    async def _props(self, **kw):
        return [SimpleNamespace(
            pending_token="tok1", operation="void_work_order",
            params={"work_order_no": 90018, "reason": "duplicate"},
            dry_run_diff={"changes": {"status": {"from": "OPEN", "to": "VOID"}}},
            proposed_by="agent:analytics",
            expires_at=datetime(2026, 7, 3, 12, 0), created_at=datetime(2026, 7, 2, 12, 0),
        )]

    async def _zero(self, **kw):
        return 0

    async def _fb_open(self, *a, **kw):
        return [SimpleNamespace(id=7, user_id="operator.a", message="Need a scan SOP",
                                created_at=datetime(2026, 7, 13, 9, 30))]

    async def _fb_done(self, *a, **kw):
        return [SimpleNamespace(id=6, user_id="operator.a", message="Old note",
                                resolved_by="human:jlee",
                                resolved_at=datetime(2026, 7, 12, 9, 30))]

    async def _status(self, **kw):
        return [SimpleNamespace(work_order_no=90018, from_status="IN_PROGRESS",
                                to_status="ON_HOLD", hold_reason="WAITING_PARTS",
                                changed_at=datetime(2026, 7, 3, 15, 0),
                                source_actor="human:jlee")]

    async def _notes(self, **kw):
        return [SimpleNamespace(work_order_no=90018, entry_type="progress",
                                updated_at=datetime(2026, 7, 3, 14, 0), updated_by="human:ben.yeh")]

    async def _txns(self, **kw):
        return [SimpleNamespace(item_code="ES900004", kind="ISSUE", qty_delta=Decimal("-2"),
                                work_order_no=90018, charge_target_asset_id="EID-9",
                                occurred_at=datetime(2026, 7, 3, 16, 0),
                                source_actor="agent:hermes")]

    async def _rprops(self, **kw):
        return [SimpleNamespace(status="CONFIRMED", operation="void_work_order",
                                params={"work_order_no": 90018},
                                resolved_at=datetime(2026, 7, 3, 13, 0),
                                confirmed_by="human:jlee", proposed_by="human:ben.yeh")]

    async def _dels(self, **kw):
        return [SimpleNamespace(kind="step", task_no="TSK0007", detail="Clean the head",
                                deleted_at=datetime(2026, 7, 3, 12, 0), deleted_by="human:jlee")]

    async def _att_counts(self):
        return {"work_order_note": 12}

    async def _att_recent(self, **kw):
        return [SimpleNamespace(id=7, owner_type="work_order_note", owner_id="7",
                                original_filename="fault.jpg", r2_key="k/7.jpg",
                                created_by="human:jlee")]

    async def _att_deleted(self, **kw):
        return [SimpleNamespace(id=9, owner_type="inventory_item", owner_id="ES900004",
                                original_filename="old.jpg", r2_key="k/9.jpg",
                                created_by="human:ben.yeh", updated_by="human:jlee")]

    async def _creds(self):
        return [SimpleNamespace(id=3, user_id="ben.yeh", system="jira", label="prod",
                                secret_ciphertext="SHOULD-NEVER-RENDER",
                                created_at=datetime(2026, 7, 1, 9, 0), last_used_at=None)]

    async def _holds(self):
        return [SimpleNamespace(code="WAITING_PARTS", label="Waiting parts", is_downtime=True)]

    async def _statuses(self):
        return [SimpleNamespace(code="ON_HOLD", label="On Hold", rank=3,
                                is_terminal=False, is_downtime=True)]

    async def _codes(self):
        return [SimpleNamespace(code="progress", label="Progress")]

    async def _bins(self, include_inactive=False):
        return [SimpleNamespace(code="02A", is_active=True),
                SimpleNamespace(code="OLD1", is_active=False)]

    async def _mfc(self, station=None):
        return [SimpleNamespace(station="sta1", label="CycleStop", signal_id=None,
                                entry_kind="fail_flag", semantic_zh=None)]

    async def _efc(self, station_hint=None):
        return [SimpleNamespace(code="demo_air_pressure", descr="Low Air Pressure",
                                station_hint="sta7")]

    async def _rel_all(self, **kw):
        return [SimpleNamespace(relationship_type="contains_module", id=1),
                SimpleNamespace(relationship_type="shared_dependency", id=3)]

    async def _asset(self, aid):
        return SimpleNamespace(asset_id=aid, description="Curer 9") if aid != "EID-404" else None

    async def _rels(self, asset_id, *, direction="both", **kw):
        if direction == "from":
            return [SimpleNamespace(id=10, relationship_type="contains_module",
                                    from_asset_id=asset_id, to_asset_id="EID-70006"),
                    SimpleNamespace(id=11, relationship_type="shared_dependency",
                                    from_asset_id=asset_id, to_asset_id="EID-70014")]
        return [SimpleNamespace(id=12, relationship_type="contains_module",
                                from_asset_id="EID-70001", to_asset_id=asset_id)]

    async def _recips(self):
        return [
            SimpleNamespace(id=1, name="Owner A", email="a@example.com", telegram_chat_id="-100",
                            assignee_name="Alice Fang", notify_on_open=False,
                            notify_on_close=True, is_active=True, watches=["Ben Yeh"]),
            SimpleNamespace(id=2, name="team", email="team@example.com", telegram_chat_id=None,
                            assignee_name=None, notify_on_open=True, notify_on_close=False,
                            is_active=False, watches=[]),
        ]

    async def _outbox(self, limit=20):
        return [SimpleNamespace(work_order_no=90016, event="opened", channel="email",
                                recipient_id=1, status="failed", attempts=3,
                                last_error="x" * 80)]

    async def _owners_list(self, *, search=None, only_missing=True, limit=300):
        return [SimpleNamespace(asset_id="EID-70003", description="Air Gun", line="Zone A",
                                owners=[])]

    async def _pm_counts(self, ids):
        return {"EID-70003": 4}

    async def _empty(self, *a, **kw):
        return []

    S = web_routes
    for cls, name, fn in [
        (S.IdentityService, "list_users", _users),
        (S.TaskService, "list_tasks", _tasks),
        (S.TaskService, "get_task", _task),
        (S.TaskService, "get_task_steps", _steps),
        (S.TaskService, "get_parts_for_steps", _parts),
        (S.PmScheduleService, "list_pm_schedules", _scheds),
        (S.WorkOrderService, "list_proposals", _props),
        (S.WorkOrderService, "expire_stale_proposals", _zero),
        (S.FeedbackService, "list_open", _fb_open),
        (S.FeedbackService, "list_recent_resolved", _fb_done),
        (S.WorkOrderService, "list_recent_status_changes", _status),
        (S.WorkOrderService, "list_recent_note_edits", _notes),
        (S.InventoryService, "list_recent_transactions", _txns),
        (S.WorkOrderService, "list_resolved_proposals", _rprops),
        (S.TaskService, "list_recent_deletions", _dels),
        (S.AttachmentService, "counts_by_owner_type", _att_counts),
        (S.AttachmentService, "list_recent_uploads", _att_recent),
        (S.AttachmentService, "list_soft_deleted", _att_deleted),
        (S.CredentialVault, "list_all_credentials", _creds),
        (_adm.WorkOrderService, "list_hold_reasons", _holds),
        (_adm.WorkOrderService, "list_statuses", _statuses),
        (_adm.WorkOrderService, "list_note_types", _codes),
        (_adm.PmScheduleService, "list_freq_units", _codes),
        (_adm.ContactsService, "list_org_types", _codes),
        (_adm.InventoryService, "list_stock_txn_kinds", _codes),
        (_adm.InventoryService, "list_storage_bins", _bins),
        (_adm.FailureVocabService, "list_mes_failmodes", _mfc),
        (_adm.FailureVocabService, "list_equipment_failure_codes", _efc),
        (_adm.AssetService, "list_relationships_all", _rel_all),
        (_adm.AssetService, "get_asset", _asset),
        (_adm.AssetService, "list_relationships", _rels),
        (_adm.NotificationService, "list_recipients", _recips),
        (_adm.NotificationService, "list_recent_outbox", _outbox),
        (_adm.NotificationService, "count_failed", _zero),
        (_adm.AssetService, "list_for_owner_admin", _owners_list),
        (_adm.AssetService, "pm_counts", _pm_counts),
    ]:
        monkeypatch.setattr(cls, name, fn)


_ADMIN_PAGES = [
    ("/admin/accounts", "/admin/accounts"),
    ("/admin/accounts/new", "/admin/accounts"),
    ("/admin/pm", "/admin/pm"),
    ("/admin/pm/TSK0007", "/admin/pm"),
    ("/admin/proposals", "/admin/proposals"),
    ("/admin/audit", "/admin/audit"),
    ("/admin/attachments", "/admin/attachments"),
    ("/admin/credentials", "/admin/credentials"),
    ("/admin/vocab", "/admin/vocab"),
    ("/admin/notify", "/admin/notify"),
    ("/admin/relationships", "/admin/relationships"),
    ("/admin/relationships?eid=EID-70005", "/admin/relationships"),
    ("/admin/owners", "/admin/owners"),
]


@pytest.mark.parametrize(("url", "active"), _ADMIN_PAGES)
def test_admin_pages_render_v2_with_parity(env, adm_data, url, active):
    _as(env, "admin")
    html = _parity(url)
    _assert_admin_shell(html, active)
    assert "banner--err" not in html


@pytest.mark.parametrize("url", [u for u, _ in _ADMIN_PAGES])
def test_admin_pages_forbid_engineer(env, adm_data, url):
    assert client.get(url).status_code == 403


def test_admin_accounts_content(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/accounts").text
    assert 'href="/admin/accounts/new"' in html
    assert 'onchange="this.form.submit()"' in html
    assert 'value="viewer"' not in html and 'value="storekeeper"' not in html
    assert 'name="assignee" value="Ben Yeh"' in html and 'data-suggest="person"' in html
    assert 'name="new_password"' in html
    assert "adm-btn adm-btn--danger" in html                  # 停用 = 危險描邊
    assert 'class="is-flat"' in html                          # 停用帳號列壓平
    assert "/admin/accounts/old/activate" in html


def test_admin_account_new_form_and_error(env, adm_data, monkeypatch):
    from cmms.domain.identity.service import IdentityError

    _as(env, "admin")
    html = client.get("/admin/accounts/new").text
    for field in ("username", "display_name", "org", "role", "password", "emaint_assignee"):
        assert f'name="{field}"' in html
    assert '<option value="plant">plant</option>' in html
    assert '<option value="contractor">contractor</option>' in html
    assert i18n.translate("admin.notify.assignee_ph", "en") in html

    async def _create(self, **kw):
        raise IdentityError("username already exists")

    monkeypatch.setattr(web_routes.IdentityService, "create_user", _create)
    r = client.post("/admin/accounts", data={
        "username": "x", "display_name": "X", "org": "plant", "role": "engineer",
        "password": "secret12",
    })
    assert r.status_code == 200
    _assert_v2(r.text)
    assert '<div class="banner banner--err" role="alert">username already exists</div>' in r.text


@pytest.mark.parametrize("url", [
    "/admin/accounts?err=boom", "/admin/proposals?err=boom", "/admin/vocab?err=boom",
    "/admin/notify?err=boom", "/admin/pm/TSK0007?err=boom", "/admin/relationships?err=boom",
    "/admin/owners?err=boom",
])
def test_admin_err_banner(env, adm_data, url):
    _as(env, "admin")
    html = client.get(url).text
    assert '<div class="banner banner--err" role="alert">boom</div>' in html


def test_admin_owners_banners_and_hooks(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/owners?err=admin.owners.err.noselect").text
    assert i18n.translate("admin.owners.err.noselect", "en") in html
    assert "admin.owners.err.noselect" not in html.replace('value="', "")
    html = client.get("/admin/owners?ok=2").text
    assert "banner banner--ok" in html
    assert 'id="owner-all"' in html and 'class="check owner-row"' in html
    assert 'name="asset_ids" value="EID-70003"' in html
    assert 'name="owners"' in html and "data-multi" in html
    assert "Zone A" in html


def test_admin_pm_list_and_htmx_partial(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/pm").text
    assert 'id="task-results"' in html
    assert 'hx-target="#task-results"' in html and 'hx-push-url="true"' in html
    assert 'name="subtype"' not in html                       # 公開版無子類型過濾
    assert 'action="/admin/pm"' in html and 'name="task_no"' in html
    part = client.get("/admin/pm?q=Sorter", headers={"HX-Request": "true"})
    assert part.status_code == 200
    assert "<html" not in part.text
    assert 'class="chip chip--id mono" href="/admin/pm/TSK0007"' in part.text   # v2 partial
    v1 = client_v1.get("/admin/pm?q=Sorter", headers={"HX-Request": "true"}).text
    assert 'class="chip chip--id' not in v1


def test_admin_pm_task_hooks(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/pm/TSK0007").text
    assert "<ol" in html
    assert 'formaction="/admin/pm/TSK0007/steps/1/delete"' in html and "formnovalidate" in html
    assert 'data-suggest="part"' in html and "data-suggest-strict" in html
    assert 'data-suggest="asset"' in html
    assert "/admin/pm/TSK0007/steps/2/parts/EC900007/delete" in html
    assert "/admin/pm/TSK0007/schedules/P2/suppress" in html
    assert 'class="is-flat"' in html                          # 暫停中的排程列壓平


def test_admin_proposals_content(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/proposals").text
    assert "tag tag--prop-red" in html
    assert 'href="/app/work-orders/90018"' in html
    assert "VOID" in html and "duplicate" in html
    assert "adm-btn adm-btn--primary" in html                 # 核准 = 實填
    assert 'name="reason"' not in html                        # 公開版駁回無理由欄
    assert 'name="disposition"' not in html                   # 公開版回饋單鍵結案
    assert "/admin/feedback/7/resolve" in html
    assert "Need a scan SOP" in html and "Old note" in html


def test_admin_audit_content(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/audit").text
    assert 'action="/admin/audit"' in html and 'name="limit"' in html
    assert "2026-07-04 00:00" in html                         # 台北時間(16:00Z → 次日 00:00)
    assert 'href="/app/equipment/EID-9"' in html
    assert 'href="/admin/pm/TSK0007"' in html
    assert "author--agent" in html                            # agent 身分
    assert "-2" in html


def test_admin_credentials_no_secret(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/credentials").text
    assert "SHOULD-NEVER-RENDER" not in html
    assert "/admin/credentials/3/revoke" in html and "return confirm(" in html
    assert "2026-07-01" in html


def test_admin_notify_hooks(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/notify").text
    assert 'id="rcp1"' in html and 'action="/admin/notify/1/update"' in html
    assert 'form="rcp1" name="name"' in html
    assert 'form="rcp1" type="checkbox" name="notify_on_close"' in html
    assert 'button type="submit" form="rcp1"' in html
    assert "/admin/notify/2/toggle" in html
    assert html.count('action="/admin/notify"') == 2          # 新增通訊錄 + 新增廣播
    assert html.count('name="watch_assignees"') >= 4
    assert i18n.translate("admin.notify.section.contacts", "en") in html
    assert i18n.translate("admin.notify.section.broadcast", "en") in html
    assert "banner banner--warn" in html                      # 通道未設定 → 琥珀
    assert 'title="' + "x" * 80 + '"' in html                 # 截斷錯誤附全文 title
    assert "/admin/notify/preview" not in html                # 公開版無試算面板


def test_admin_relationships_lookup_and_notfound(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/relationships?eid=EID-70005").text
    assert "/admin/relationships/10/unlink" in html and "/admin/relationships/12/unlink" in html
    assert html.count("data-suggest-strict") == 4
    html = client.get("/admin/relationships?eid=EID-404").text
    assert "banner banner--err" in html
    assert i18n.translate("admin.rel.notfound", "en") in html


def test_admin_vocab_content(env, adm_data):
    _as(env, "admin")
    html = client.get("/admin/vocab").text
    for target in ("/admin/vocab/hold-reasons", "/admin/vocab/hold-reasons/WAITING_PARTS/update",
                   "/admin/vocab/storage-bins", "/admin/vocab/storage-bins/OLD1/toggle"):
        assert f'action="{target}"' in html
    assert "demo_air_pressure" in html and "CycleStop" in html


def test_admin_shell_demo_banner(env, adm_data, monkeypatch):
    from cmms.config import Settings

    monkeypatch.setattr(web_routes, "get_settings",
                        lambda: Settings(demo_mode=True, demo_reset_minutes=30))
    _as(env, "admin")
    html = client.get("/admin/accounts").text
    assert "demo-banner" in html
    assert html.index("demo-banner") < html.index('class="adm-top"')
