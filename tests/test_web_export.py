"""匯出中心 web 層測試(路由 / 過濾 / 預覽 / CSV / 欄位級 RBAC)。

不需 DB:override get_current_user / get_session + monkeypatch ExportService,只驗路由、
CSV 串流(BOM / 欄頭 / 值 / formula guard)、預覽截斷、壞日期 banner、admin_only 欄位閘。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.web import export as web_export
from cmms.web import routes as web_routes
from cmms.web.export import ColumnSpec, DatasetSpec, visible_columns

client = TestClient(app)


def _fake_user(*, role: str = "engineer", locale: str = "en") -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="Jordan",
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
def anon():
    async def _session():
        yield None

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


@pytest.fixture
def _no_options(monkeypatch):
    """/app/export 首頁會為 chips 過濾器讀 lookup:session=None 下 monkeypatch 成空。"""
    async def _opts(self, source):
        return [("Production", "Production"), ("REACTIVE", "Reactive")]

    monkeypatch.setattr(web_routes.ExportService, "lookup_options", _opts)


# ---- 首頁 + 卡片 ----

def test_export_requires_login(anon):
    r = client.get("/app/export", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


def test_export_home_five_cards(as_user, _no_options):
    r = client.get("/app/export")
    assert r.status_code == 200
    # 五張資料集卡片標題(en)
    for title in ["Work Orders", "Part Usage", "Equipment",
                  "Maintenance Schedules", "Maintenance Steps"]:
        assert title in r.text
    # 預設 active = work_orders(其過濾表單:狀態 chips + 開單日期欄)
    assert 'name="status"' in r.text
    assert 'name="opened_from"' in r.text and 'type="date"' in r.text


def test_export_home_switch_dataset(as_user, _no_options):
    r = client.get("/app/export?ds=assets")
    assert r.status_code == 200
    assert 'href="/app/export?ds=assets"' in r.text
    assert "is-active" in r.text
    # assets 的過濾器:類型 chips + 三態狀態下拉
    assert 'name="asset_type"' in r.text
    assert 'name="is_active"' in r.text and "<select" in r.text


def test_export_home_invalid_ds_defaults(as_user, _no_options):
    r = client.get("/app/export?ds=nope")
    assert r.status_code == 200
    assert 'name="status"' in r.text  # 落回 work_orders 預設


# ---- 預覽(試算)----

@pytest.fixture
def _wo_export(monkeypatch):
    rows = [
        {
            "work_order_no": 100, "asset_id": "EID-001", "asset_description": "Rig One",
            "work_type": "REACTIVE", "status": "CLOSED", "brief_description": "belt snapped",
            "diagnosis": None, "assigned_person": "Alice Fang", "assigned_vendor": "CMA",
            "priority": None, "external_ref": "MRQ-4220", "opened_date": None,
            "scheduled_date": None, "closed_date": None, "opened_at": None, "closed_at": None,
            "downtime_minutes": 120, "downtime_estimated": True, "hold_reason": None,
            "labor_hours": None, "cost": None, "action_taken": None,
        },
    ]

    async def _count(self, **f):
        return 7

    async def _rows(self, *, limit=None, **f):
        return rows[:limit] if limit else rows

    monkeypatch.setattr(web_routes.ExportService, "count_work_orders", _count)
    monkeypatch.setattr(web_routes.ExportService, "rows_work_orders", _rows)
    return rows


def test_export_preview_count_and_download_link(as_user, _wo_export):
    r = client.get("/app/export/work_orders/preview?status=CLOSED&status=OPEN")
    assert r.status_code == 200
    assert "Matches:" in r.text and ">7<" in r.text        # count
    assert "showing first 5" in r.text                      # count > 5
    assert "belt snapped" in r.text                         # 前 5 列
    assert "Rig One" in r.text                              # join 顯示欄
    # 下載連結帶同組 query(過濾器一致;Jinja autoescape 把 & → &amp;,故只比對前綴)
    assert "/app/export/work_orders/download?status=CLOSED" in r.text


def test_export_preview_bad_date_is_banner_not_500(as_user, _wo_export):
    r = client.get("/app/export/work_orders/preview?opened_from=2026-13-99")
    assert r.status_code == 200                             # 不 500
    assert "Invalid date" in r.text


def test_export_preview_unknown_slug_404(as_user):
    r = client.get("/app/export/nope/preview")
    assert r.status_code == 404


# ---- 下載 CSV ----

def test_export_download_csv_bom_header_values(as_user, _wo_export):
    r = client.get("/app/export/work_orders/download?status=CLOSED")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    cd = r.headers["content-disposition"]
    assert "attachment" in cd and "cmms-work_orders-" in cd and cd.endswith('.csv"')
    body = r.content
    assert body.startswith(b"\xef\xbb\xbf")                 # utf-8-sig BOM
    text = body.decode("utf-8-sig")
    lines = text.splitlines()
    assert lines[0].startswith("WO No.,EID,Equipment,")     # 譯過的欄頭
    assert "100,EID-001,Rig One" in lines[1]                # 值列
    assert "MRQ-4220" in lines[1]


def test_export_download_formula_injection_guard(as_user, monkeypatch):
    rows = [{
        "work_order_no": 1, "asset_id": "EID-001", "asset_description": "=cmd()",
        "work_type": "REACTIVE", "status": "OPEN", "brief_description": "+SUM(A1)",
        "diagnosis": "@evil", "assigned_person": "-danger", "assigned_vendor": None,
        "priority": None, "external_ref": None, "opened_date": None, "scheduled_date": None,
        "closed_date": None, "opened_at": None, "closed_at": None, "downtime_minutes": None,
        "downtime_estimated": False, "hold_reason": None, "labor_hours": None, "cost": None,
        "action_taken": None,
    }]

    async def _rows(self, *, limit=None, **f):
        return rows

    monkeypatch.setattr(web_routes.ExportService, "rows_work_orders", _rows)
    r = client.get("/app/export/work_orders/download")
    text = r.content.decode("utf-8-sig")
    # 開頭為 = + - @ 的字串值前綴 '(Excel formula-injection 防護)
    assert "'=cmd()" in text
    assert "'+SUM(A1)" in text
    assert "'@evil" in text
    assert "'-danger" in text


def test_export_download_unknown_slug_404(as_user):
    r = client.get("/app/export/nope/download")
    assert r.status_code == 404


# ---- 欄位級 RBAC(admin_only 機制)----

def test_visible_columns_filters_admin_only():
    spec = DatasetSpec(
        slug="x", title_key="", desc_key="",
        columns=(ColumnSpec("a"), ColumnSpec("secret", admin_only=True)),
        filters=(), parse=lambda qp: {},
    )
    assert [c.key for c in visible_columns(spec, is_admin=False)] == ["a"]
    assert [c.key for c in visible_columns(spec, is_admin=True)] == ["a", "secret"]


def test_current_datasets_have_no_admin_only():
    """本範圍 engineer 全欄可讀(PII 只在 contacts、不在此);機制在,尚無欄標 admin_only。"""
    for spec in web_export.DATASETS.values():
        assert all(not c.admin_only for c in spec.columns)


@pytest.fixture
def _admin_only_ds(monkeypatch):
    """注入一個帶 admin_only 欄的測試資料集,驗端到端 RBAC(preview + download)。"""
    spec = DatasetSpec(
        slug="t_admin", title_key="export.title", desc_key="export.title",
        columns=(ColumnSpec("a"), ColumnSpec("secret", admin_only=True)),
        filters=(), parse=lambda qp: {},
    )
    monkeypatch.setitem(web_export.DATASETS, "t_admin", spec)

    async def _count(self, **f):
        return 1

    async def _rows(self, *, limit=None, **f):
        return [{"a": "shown", "secret": "topsecret"}]

    monkeypatch.setattr(web_routes.ExportService, "count_t_admin", _count, raising=False)
    monkeypatch.setattr(web_routes.ExportService, "rows_t_admin", _rows, raising=False)


def test_export_admin_only_hidden_from_engineer(as_user, _admin_only_ds):
    r = client.get("/app/export/t_admin/preview")
    assert r.status_code == 200
    assert "shown" in r.text
    assert "topsecret" not in r.text                        # engineer 看不到 admin_only 欄
    # CSV 亦不含
    d = client.get("/app/export/t_admin/download")
    assert "topsecret" not in d.content.decode("utf-8-sig")


def test_export_admin_only_visible_to_admin(as_user, _admin_only_ds):
    as_user["user"].role = "admin"
    r = client.get("/app/export/t_admin/preview")
    assert "topsecret" in r.text                            # admin 看得到
    d = client.get("/app/export/t_admin/download")
    assert "topsecret" in d.content.decode("utf-8-sig")
