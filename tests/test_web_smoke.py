"""工程師操作台 web scaffold 冒煙測試(ADR-019 + i18n ADR-023 + auth ADR-022)。

不需 DB:i18n 純函式 + 以 dependency override(get_current_user / get_session)+ monkeypatch
service 避開真 DB,只驗路由 / 模板 / 登入流 / 保護 / 時間線渲染。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from cmms.api.app import app
from cmms.api.deps import get_session
from cmms.domain.asset.service import AssetError
from cmms.domain.identity.service import AuthenticationError
from cmms.domain.work_order.service import WorkOrderError
from cmms.web import admin_routes as _adm
from cmms.web import i18n
from cmms.web import routes as web_routes

client = TestClient(app)


def _fake_user(
    *, locale: str = "en", role: str = "engineer", emaint_assignee: str | None = "Alice Fang"
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id="jlee", username="jlee", display_name="陳工",
        role=role, ui_locale=locale, jira_output_locale="en", is_active=True,
        emaint_assignee=emaint_assignee,
    )


@pytest.fixture
def as_user():
    """登入態:override get_current_user + get_session。回可變 holder(可改 user.ui_locale)。"""
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
    """未登入態:get_current_user → None。"""
    async def _session():
        yield None

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


@pytest.fixture(autouse=True)
def _stub_asset_names(monkeypatch):
    """web smoke session=None:工單清單卡的批次資產敘述預設 stub 空(空 map → 卡片輸出不變;
    個別測試可覆蓋以驗機台名渲染)。同時 stub 0031 多負責人讀取(get_owners/owners_map/
    get_assignees/assignees_map)為空預設,避免 session=None 觸真 SQL;個別測試可覆蓋。"""
    async def _empty_map(self, asset_ids):
        return {}

    async def _empty_list(self, _id):
        return []

    async def _empty_amap(self, ids):
        return {}

    monkeypatch.setattr(web_routes.AssetService, "descriptions_map", _empty_map)
    monkeypatch.setattr(web_routes.AssetService, "get_owners", _empty_list)
    monkeypatch.setattr(web_routes.AssetService, "owners_map", _empty_amap)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_assignees", _empty_list)
    monkeypatch.setattr(web_routes.WorkOrderService, "assignees_map", _empty_amap)


@pytest.fixture(autouse=True)
def _stub_storage_bins(monkeypatch):
    """web smoke session=None:儲位受控詞彙(combobox options)預設 stub 空;
    個別測試可覆蓋以驗 quick-add / 選項渲染。"""
    async def _bins(self, include_inactive=False):
        return []

    monkeypatch.setattr(web_routes.InventoryService, "list_storage_bins", _bins)


@pytest.fixture(autouse=True)
def _stub_telegram_link(monkeypatch):
    """web smoke session=None:設定頁的 Telegram 綁定狀態預設 stub 未綁(None);
    個別測試可覆蓋以驗已綁 / 產碼渲染。"""
    async def _no_link(self, user_id):
        return None

    monkeypatch.setattr(web_routes.TelegramBridgeService, "get_link", _no_link)


@pytest.fixture(autouse=True)
def _stub_feedback(monkeypatch):
    """web smoke session=None:/admin/proposals 同頁的使用者回饋區(內部規格)預設 stub 空;
    個別測試可覆蓋以驗開放回饋渲染。"""
    async def _empty(self, *a, **kw):
        return []

    monkeypatch.setattr(web_routes.FeedbackService, "list_open", _empty)
    monkeypatch.setattr(web_routes.FeedbackService, "list_recent_resolved", _empty)


# ---- i18n 純函式 ----

def test_translate_fallback():
    assert i18n.translate("nav.report", "en") == "Report"
    assert i18n.translate("nav.report", "zh-TW") == "報修"
    assert i18n.translate("nav.report", "vi") == "Báo sự cố"
    assert i18n.translate("nav.report", "xx") == "Report"          # 未知 locale → en
    assert i18n.translate("no.such.key", "en") == "no.such.key"    # 未知 key → key 本身


def test_wo_status_key_reactive_open_label():
    """Jordan 2026-07-07:REACTIVE 開單即機台 down → OPEN 顯示「維修中」;PM 的 OPEN 維持
    「待處理」。分流僅在 OPEN + REACTIVE —— 其餘狀態(含任何 IN_PROGRESS)一律走通用鍵,
    故 PM 切到處理中顯示「進行中」、不會變「維修中」(維修中綁工單類型,不綁狀態)。"""
    assert i18n.wo_status_key("OPEN", "REACTIVE") == "status.OPEN.reactive"
    assert i18n.translate("status.OPEN.reactive", "zh-TW") == "維修中"
    assert i18n.wo_status_key("OPEN", "PM") == "status.OPEN"
    assert i18n.translate("status.OPEN", "zh-TW") == "待處理"
    # 任何 IN_PROGRESS(reactive 或 PM)→ 通用「進行中」,非「維修中」
    assert i18n.wo_status_key("IN_PROGRESS", "REACTIVE") == "status.IN_PROGRESS"
    assert i18n.wo_status_key("IN_PROGRESS", "PM") == "status.IN_PROGRESS"
    assert i18n.translate("status.IN_PROGRESS", "zh-TW") == "進行中"


def test_negotiate_locale():
    assert i18n.negotiate_locale("zh-TW", None) == "zh-TW"
    assert i18n.negotiate_locale(None, "vi,en;q=0.8") == "vi"
    assert i18n.negotiate_locale(None, "zh-TW,zh;q=0.9") == "zh-TW"
    assert i18n.negotiate_locale(None, "zh") == "zh-TW"
    assert i18n.negotiate_locale(None, "fr-FR") == "en"
    assert i18n.negotiate_locale("bogus", "fr") == "en"


def test_status_css():
    assert i18n.status_css("IN_PROGRESS") == "prog"
    assert i18n.status_css("H") == "done"
    assert i18n.status_css("O") == "open"
    assert i18n.status_css("???") == "open"
    assert i18n.status_css(None) == "open"


def test_note_type_css():
    assert i18n.note_type_css("diagnosis") == "indigo"
    assert i18n.note_type_css("hold") == "hold"
    assert i18n.note_type_css("report") == "open"
    assert i18n.note_type_css("???") == "muted"


# ---- 認證流(ADR-022)----

def test_health():
    assert client.get("/health").status_code == 200


def test_root_redirects_to_app():
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app"


def test_unauthenticated_redirects_to_login(anon):
    r = client.get("/app/work-orders", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"
    r2 = client.get("/app", follow_redirects=False)
    assert r2.headers["location"] == "/app/login"


def test_login_page_renders(anon):
    r = client.get("/app/login")
    assert r.status_code == 200
    assert 'name="password"' in r.text
    assert "Sign in" in r.text


def test_login_success_sets_cookie(anon, monkeypatch):
    async def _ok(self, username, password):
        return "jlee", "tok-abc123"
    monkeypatch.setattr(web_routes.IdentityService, "authenticate", _ok)
    r = client.post(
        "/app/login", data={"username": "jlee", "password": "pw"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders"
    assert "cmms_session=tok-abc123" in r.headers.get("set-cookie", "")


def test_login_bad_credentials_shows_error(anon, monkeypatch):
    async def _bad(self, username, password):
        raise AuthenticationError("nope")
    monkeypatch.setattr(web_routes.IdentityService, "authenticate", _bad)
    r = client.post("/app/login", data={"username": "x", "password": "y"})
    assert r.status_code == 200
    assert "Incorrect username or password." in r.text


def test_logout_clears_cookie(anon, monkeypatch):
    async def _logout(self, token):
        return None
    monkeypatch.setattr(web_routes.IdentityService, "logout", _logout)
    r = client.get("/app/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/login"
    sc = r.headers.get("set-cookie", "")
    assert "cmms_session=" in sc and "max-age=0" in sc.lower()   # 清除 cookie


# ---- 工單佇列(登入態)----

@pytest.fixture
def _queue_data(monkeypatch):
    async def _fake_list(self, **kwargs):
        return [
            SimpleNamespace(
                work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
                status="IN_PROGRESS", brief_description="吸嘴堵塞、取料失敗連續報警",
                downtime_minutes=220, opened_date=date(2026, 7, 1),
            ),
        ]
    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _fake_list)


def test_work_order_queue_renders(as_user, _queue_data):
    r = client.get("/app/work-orders")
    assert r.status_code == 200
    assert "WO-30318" in r.text
    assert "吸嘴堵塞、取料失敗連續報警" in r.text
    assert "badge--prog" in r.text
    assert "3h40m" in r.text                 # downtime 220 分 → 3h40m


def test_wo_queue_card_shows_machine_name(as_user, _queue_data, monkeypatch):
    """清單卡在 EID 後附機台名(批次 descriptions_map;Jordan 2026-07-07)。"""
    async def _names(self, asset_ids):
        return {"EID-70021": "Aligner46"}

    monkeypatch.setattr(web_routes.AssetService, "descriptions_map", _names)
    r = client.get("/app/work-orders")
    assert r.status_code == 200
    assert "EID-70021" in r.text
    assert "Aligner46" in r.text                # 機台名附在 EID 後


def test_work_order_queue_zh_labels(as_user, _queue_data):
    as_user["user"].ui_locale = "zh-TW"      # 語言取自 user_account(ADR-023)
    r = client.get("/app/work-orders")
    assert "我的工單" in r.text
    assert "進行中" in r.text
    assert "停機" in r.text  # 累計停機時數 chip(非「停機中」—— 結案單也顯示定案值)


# ---- 工單詳情 · 時間線(work_order_note)----

@pytest.fixture
def _detail_data(monkeypatch):
    wo = SimpleNamespace(
        work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
        status="IN_PROGRESS", brief_description="吸嘴堵塞、取料失敗連續報警", downtime_minutes=220,
    )
    notes = [
        SimpleNamespace(id=1, entry_type="report", body="吸嘴堵塞,已停機。",
                        author="human:jlee", occurred_at=datetime(2026, 7, 1, 9, 12),
                        status_history_id=None),
        SimpleNamespace(id=2, entry_type="diagnosis", body="量測真空度不足,判定真空泵老化需更換。",
                        author="agent:hermes", occurred_at=datetime(2026, 7, 1, 14, 30),
                        status_history_id=None),
        SimpleNamespace(id=3, entry_type="hold", body="轉等待:等 ES000804 真空泵。",
                        author="human:jlee", occurred_at=datetime(2026, 7, 2, 8, 20),
                        status_history_id=5),
    ]

    async def _get(self, no):
        return wo if no == 30318 else None

    async def _list(self, no):
        return notes

    # 詳情路由改批次撈照片(attachments_map,免 N+1):note 2 有 1 張,其餘無
    async def _atts_map(self, owner_type, owner_ids):
        if owner_type == "work_order_note" and "2" in owner_ids:
            return {"2": [SimpleNamespace(id=1, r2_bucket="cmms-media",
                                          r2_key="work_order_note/2/ab.jpg")]}
        return {}

    def _presign(self, att, **kwargs):
        return (f"memory://{att.r2_bucket}/{att.r2_key}?ttl=900", 900)

    async def _parts(self, no):
        return []

    async def _links(self, no):
        return []

    async def _find_pending(self, **kw):
        return None

    async def _hold_reasons(self):
        return [SimpleNamespace(code=c, is_downtime=d) for c, d in (
            ("WAITING_MACHINE_TIME", False), ("WAITING_PARTS", True),
            ("WAITING_VENDOR", True), ("OTHER", True), ("TEST_RUN", False),
        )]

    async def _efc_opts(self, asset_id):  # D6:REACTIVE 結單真因下拉候選(efc 軸,code+人話+曾用)
        return [
            SimpleNamespace(code="efcPickupVacuumFault", descr="Pickup Vacuum Fault", used=True),
            SimpleNamespace(code="efcOtherFault", descr="Other Fault", used=False),
        ]

    async def _get_asset(self, asset_id):  # 標頭機台名(工單只帶 EID → 附資產敘述)
        return SimpleNamespace(asset_id=asset_id, description="Cap Applicator #3")

    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get_asset)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_notes", _list)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_parts", _parts)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_external_links", _links)
    monkeypatch.setattr(web_routes.WorkOrderService, "find_pending_proposal", _find_pending)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_hold_reasons", _hold_reasons)
    monkeypatch.setattr(
        web_routes.WorkOrderService, "list_confirmed_reason_options", _efc_opts
    )
    monkeypatch.setattr(web_routes.AttachmentService, "attachments_map", _atts_map)
    monkeypatch.setattr(web_routes.AttachmentService, "presigned_url", _presign)


def test_work_order_detail_timeline(as_user, _detail_data):
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/work-orders/30318")
    assert r.status_code == 200
    assert "工作日誌" in r.text
    assert "量測真空度不足,判定真空泵老化需更換。" in r.text
    assert "診斷" in r.text
    assert "badge--indigo" in r.text
    assert "author-chip--agent" in r.text
    assert "hermes" in r.text
    assert "等料" in r.text
    assert "狀態 →" in r.text
    assert "work_order_note/2/ab.jpg" in r.text      # note 2 的照片內嵌時間線
    assert 'class="tl-photo"' in r.text
    assert "Cap Applicator #3" in r.text             # 標頭附機台敘述(不只 EID)


def test_work_order_detail_not_found(as_user, _detail_data):
    r = client.get("/app/work-orders/99999")
    assert r.status_code == 200
    assert "WO-99999" in r.text


def test_finish_form_shows_efc_combobox_for_reactive(as_user, _detail_data):
    """D6:REACTIVE 工單結單表單顯示「確認故障原因」下拉(datalist + efc 候選)。"""
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/work-orders/30318")
    assert r.status_code == 200
    assert 'name="confirmed_reason_code"' in r.text
    assert "確認故障原因" in r.text
    assert "data-efc-combo" in r.text            # 多關鍵字 combobox 容器
    assert 'id="cb-list-confirmed_reason_code-finish"' in r.text  # 無 JS 退回原生 datalist
    assert "efcPickupVacuumFault" in r.text        # 候選 code(datalist + JSON)
    assert "Pickup Vacuum Fault" in r.text         # 人話描述供搜尋/顯示


def test_finish_form_hides_efc_combobox_for_pm(as_user, _detail_data, monkeypatch):
    """非 REACTIVE(PM)工單結單表單不顯示真因欄。"""
    async def _get_pm(self, no):
        return SimpleNamespace(
            work_order_no=30318, asset_id="EID-70021", work_type="PM",
            status="IN_PROGRESS", brief_description="Annual PM", downtime_minutes=0,
        )

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get_pm)
    r = client.get("/app/work-orders/30318")
    assert r.status_code == 200
    assert 'name="confirmed_reason_code"' not in r.text


def test_finish_post_passes_confirmed_reason(as_user, _detail_data, monkeypatch):
    """結單 POST 帶 confirmed_reason_code → 轉呼 finish_work_order 收到該碼。"""
    captured: dict = {}

    async def _finish(self, no, actor, **kw):
        captured.update(kw)
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "finish_work_order", _finish)
    r = client.post(
        "/app/work-orders/30318/transition",
        data={"action": "finish", "confirmed_reason_code": "efcPickupVacuumFault"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["confirmed_reason_code"] == "efcPickupVacuumFault"


# ---- 語言切換(登入 → 寫回 user_account)----

def test_set_locale_persists_for_user(as_user, monkeypatch):
    calls: list[tuple] = []

    async def _set(self, user_id, *, actor, ui_locale=None, jira_output_locale=None):
        calls.append((user_id, ui_locale))

    monkeypatch.setattr(web_routes.IdentityService, "set_locale", _set)
    r = client.get("/app/set-locale?locale=vi&next=/app/report", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/report"
    assert calls == [("jlee", "vi")]                 # 寫回 user_account(非 cookie)
    assert "cmms_locale" not in r.headers.get("set-cookie", "")


def test_set_locale_guard_open_redirect(as_user, monkeypatch):
    async def _set(self, user_id, *, actor, ui_locale=None, jira_output_locale=None):
        return None
    monkeypatch.setattr(web_routes.IdentityService, "set_locale", _set)
    r = client.get("/app/set-locale?locale=en&next=https://evil.example", follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders"


# ---- 寫入:報修開單(①)+ 加一筆更新(②),actor = 登入者 ----

def test_attachment_owner_wiring():
    """work_order_note 已註冊為 attachment owner(照片可掛 note)。"""
    from cmms.domain.attachment.service import _OWNER_MODELS
    from cmms.domain.attachment.transform import OWNER_PREFIX
    assert "work_order_note" in OWNER_PREFIX
    assert "work_order_note" in _OWNER_MODELS


def test_report_form_renders(as_user):
    r = client.get("/app/report")
    assert r.status_code == 200
    assert 'name="asset_id"' in r.text
    assert 'name="brief_description"' in r.text
    assert 'type="file"' in r.text
    # 0031:多負責人欄 + 自動帶入 hook(data-owner-autofill 指向多值容器 id)
    assert 'name="assignees"' in r.text
    assert 'data-owner-autofill="report-owners"' in r.text
    assert 'id="report-owners"' in r.text


@pytest.fixture
def _write_env(monkeypatch):
    """登入 + 假 session(asset 存在)+ monkeypatch open/add_note/add_attachment,記錄呼叫。"""
    holder = {
        "asset_exists": True, "asset_owner": "Owner Bob",
        "opened": [], "notes": [], "atts": [],
    }

    class _FS:
        async def get(self, model, key):
            if not holder["asset_exists"]:
                return None
            return SimpleNamespace()  # 0031:負責人改由 get_owners 讀(見下 stub),不再讀 asset.owner

    async def _session():
        yield _FS()

    async def _get_owners(self, eid):  # 0031:設備負責人清單(單元素或空)
        return [holder["asset_owner"]] if holder["asset_owner"] else []

    async def _open(self, *, asset_id, work_type, actor, brief_description=None,
                    opened_by=None, assigned_person=None, assignees=None, **kwargs):
        # 0031:記錄「首位負責人」(assignees 優先,否則單值),沿用既有 6-tuple 斷言形狀
        names = assignees if assignees is not None else (
            [assigned_person] if assigned_person else [])
        holder["opened"].append(
            (asset_id, work_type, brief_description, opened_by, actor.value,
             names[0] if names else None)
        )
        return SimpleNamespace(work_order_no=999)

    async def _add_note(self, work_order_no, *, entry_type, body, actor, **kwargs):
        holder["notes"].append((work_order_no, entry_type, body, actor.value))
        return SimpleNamespace(id=7)

    async def _add_att(self, *, owner_type, owner_id, ext, **kwargs):
        holder["atts"].append((owner_type, owner_id, ext))
        return SimpleNamespace(id=1), True

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: _fake_user()
    monkeypatch.setattr(web_routes.WorkOrderService, "open_work_order", _open)
    monkeypatch.setattr(web_routes.WorkOrderService, "add_note", _add_note)
    monkeypatch.setattr(web_routes.AttachmentService, "add_attachment", _add_att)
    monkeypatch.setattr(web_routes.AssetService, "get_owners", _get_owners)
    try:
        yield holder
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


def test_report_submit_opens_wo_note_photo(_write_env):
    r = client.post(
        "/app/report",
        data={"asset_id": "eid-70021", "brief_description": "吸嘴堵塞、取料失敗"},
        files={"photos": ("fault.jpg", b"\xff\xd8jpgbytes", "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/999"
    # 開 REACTIVE 工單,actor = 登入者,EID 正規化大寫;0029:未指派 → 衍生 asset.owner
    assert _write_env["opened"] == [
        ("EID-70021", "REACTIVE", "吸嘴堵塞、取料失敗", "jlee", "human:jlee", "Owner Bob")
    ]
    # 初始報修 = 第一筆 note(entry_type=report)
    assert _write_env["notes"] == [(999, "report", "吸嘴堵塞、取料失敗", "human:jlee")]
    # 照片掛 work_order_note(owner_id = note.id)
    assert _write_env["atts"] == [("work_order_note", "7", "jpg")]


def test_report_submit_unknown_asset_errors(_write_env):
    _write_env["asset_exists"] = False
    r = client.post("/app/report", data={"asset_id": "EID-NOPE", "brief_description": "x"})
    assert r.status_code == 200
    assert "not found" in r.text.lower()          # report.error(en 預設)
    assert _write_env["opened"] == []             # 不存在 → 未開單(不靜默建殘缺)


def test_add_note_submit(_write_env):
    r = client.post(
        "/app/work-orders/999/notes",
        data={"body": "拆檢吸嘴組,送量測"},
        files={"photos": ("p.png", b"pngdata", "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/999"
    assert _write_env["notes"] == [(999, "progress", "拆檢吸嘴組,送量測", "human:jlee")]
    assert _write_env["atts"] == [("work_order_note", "7", "png")]


def test_report_submit_optional_brief_with_photo(_write_env):
    """簡述留空 + 有照片 → brief=None,仍建初始 report note(body 空)並掛照片。"""
    r = client.post(
        "/app/report",
        data={"asset_id": "EID-70021", "brief_description": "  "},
        files={"photos": ("f.jpg", b"\xff\xd8jpg", "image/jpeg")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _write_env["opened"] == [
        ("EID-70021", "REACTIVE", None, "jlee", "human:jlee", "Owner Bob")
    ]
    assert _write_env["notes"] == [(999, "report", "", "human:jlee")]  # 純照片:body 空
    assert _write_env["atts"] == [("work_order_note", "7", "jpg")]


def test_report_submit_optional_brief_no_photo(_write_env):
    """簡述留空 + 無照片 → 只開單、不建空 note。"""
    r = client.post(
        "/app/report",
        data={"asset_id": "EID-70021", "brief_description": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _write_env["opened"] == [
        ("EID-70021", "REACTIVE", None, "jlee", "human:jlee", "Owner Bob")
    ]
    assert _write_env["notes"] == []
    assert _write_env["atts"] == []


def test_report_submit_explicit_assignee_wins(_write_env):
    """0031:表單填 assignees → 勝過設備負責人。"""
    r = client.post(
        "/app/report",
        data={"asset_id": "EID-70021", "brief_description": "x",
              "assignees": ["Ben Yeh"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert _write_env["opened"][0][5] == "Ben Yeh"  # 明確指派勝出(首位)


def test_report_submit_ownerless_asset_errors(_write_env):
    """0029:空 assignee + 設備無 owner → 重顯 owner_missing 錯誤,不開單。"""
    _write_env["asset_owner"] = None
    r = client.post("/app/report", data={"asset_id": "EID-70021", "brief_description": "x"})
    assert r.status_code == 200
    assert "no owner set" in r.text.lower()          # report.owner_missing(en 預設)
    assert _write_env["opened"] == []                # 未開單


def test_asset_owner_endpoint(_write_env):
    """0031:GET /app/asset-owner 回設備負責人清單 JSON;空 eid / 查無 → owners []。"""
    r = client.get("/app/asset-owner?eid=eid-70021")
    assert r.status_code == 200
    assert r.json() == {"owners": ["Owner Bob"]}
    assert client.get("/app/asset-owner?eid=").json() == {"owners": []}
    _write_env["asset_owner"] = None
    assert client.get("/app/asset-owner?eid=EID-99999").json() == {"owners": []}


def test_issue_part_unknown_code(monkeypatch):
    """未知料號 → part_unknown flash + #parts 錨點(不進 domain 領料)。"""
    class _FS:
        async def get(self, model, key):
            return None  # 查無此 InventoryItem

    async def _session():
        yield _FS()

    called = {"issue": False}

    async def _issue(self, **kwargs):
        called["issue"] = True
        return True

    monkeypatch.setattr(web_routes.WorkOrderService, "issue_part_to_work_order", _issue)
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[web_routes.get_current_user] = lambda: _fake_user()
    try:
        r = client.post(
            "/app/work-orders/999/parts",
            data={"item_code": "nope", "quantity": "1", "nonce": "n1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/app/work-orders/999?msg=part_unknown#parts"
        assert called["issue"] is False   # 未知料號:短路,未進 domain 領料
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(web_routes.get_current_user, None)


def test_report_form_strict_attrs(as_user):
    """報修 EID 欄帶 data-suggest-strict + data-strict-msg(前端擋垃圾值)。"""
    r = client.get("/app/report")
    assert "data-suggest-strict" in r.text
    assert "data-strict-msg=" in r.text


def test_work_order_detail_strict_and_brief(as_user, _detail_data):
    """詳情頁:領料 item_code strict + 故障簡述可編輯表單。"""
    r = client.get("/app/work-orders/30318")
    assert "data-suggest-strict" in r.text                 # 領料 item_code
    assert "/work-orders/30318/brief" in r.text            # 簡述編輯


# ---- 狀態操作(chips:progress/hold/finish + legacy start/resume/complete/close)----

def test_work_order_transition_hold(as_user, monkeypatch):
    """等待 chip → set_hold(任何活單態一鍵切換)。Jordan 2026-07-07:延誤說明欄已移除,
    web 一律以 note_body=None 呼叫(即使前端殘留 hold_note 欄位也不採用)。"""
    calls: list = []

    async def _set_hold(self, no, reason, actor, **kwargs):
        calls.append((no, reason, actor.value, kwargs.get("note_body")))
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "set_hold", _set_hold)
    r = client.post(
        "/app/work-orders/30318/transition",
        # hold_note 即使被送出也不再被 route 讀取(欄位已移除)→ note_body 恆 None
        data={"action": "hold", "hold_reason": "WAITING_PARTS",
              "hold_note": "泵浦 7/10 到貨"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/30318"
    assert calls == [(30318, "WAITING_PARTS", "human:jlee", None)]  # 不收延誤說明


def test_work_order_transition_progress_chip(as_user, monkeypatch):
    """「處理中」chip → resume_or_start(route 依現態分派交給 domain)。"""
    calls: list = []

    async def _ros(self, no, actor, **kwargs):
        calls.append((no, actor.value))
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "resume_or_start", _ros)
    r = client.post(
        "/app/work-orders/30318/transition", data={"action": "progress"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert calls == [(30318, "human:jlee")]


def test_work_order_transition_finish(as_user, monkeypatch):
    """單一「結單」鍵 → finish_work_order(COMPLETED→CLOSED 一鍵)。Jordan 2026-07-07:
    處置摘要欄已移除、結單不強制總結 → web 不送 action_taken → domain 收到 None(選填參數保留)。"""
    calls: list = []

    async def _finish(self, no, actor, *, action_taken=None, labor_hours=None, **kwargs):
        calls.append((no, actor.value, action_taken, labor_hours))
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "finish_work_order", _finish)
    r = client.post(
        "/app/work-orders/30318/transition",
        data={"action": "finish"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/30318"
    # action_taken 與 labor_hours 皆不由 web 收集 → domain 收到 None
    assert calls == [(30318, "human:jlee", None, None)]


def test_wo_detail_hold_reason_chip_and_note_input(as_user, monkeypatch):
    """ON_HOLD 顯示目前等待原因;hold 表單有等機台空檔選項、但**不再**含延誤說明欄
    (Jordan 2026-07-07 移除)。"""
    wo = SimpleNamespace(
        work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
        status="ON_HOLD", hold_reason="WAITING_MACHINE_TIME",
        brief_description="x", downtime_minutes=None,
    )

    async def _get(self, no):
        return wo

    async def _empty(self, no):
        return []

    async def _find_pending(self, **kw):
        return None

    async def _hold_reasons(self):
        return [SimpleNamespace(code=c, is_downtime=d) for c, d in (
            ("WAITING_MACHINE_TIME", False), ("WAITING_PARTS", True),
            ("WAITING_VENDOR", True), ("OTHER", True), ("TEST_RUN", False),
        )]

    async def _atts_map(self, owner_type, owner_ids):
        return {}

    async def _get_asset(self, asset_id):
        return SimpleNamespace(asset_id=asset_id, description="Aligner46")

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_notes", _empty)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_parts", _empty)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_external_links", _empty)
    monkeypatch.setattr(web_routes.WorkOrderService, "find_pending_proposal", _find_pending)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_hold_reasons", _hold_reasons)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_confirmed_reason_options", _empty)
    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get_asset)
    monkeypatch.setattr(web_routes.AttachmentService, "attachments_map", _atts_map)
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/work-orders/30318")
    assert "等機台空檔" in r.text                        # 目前等待原因 chip(holdreason zh-TW)
    # IN_PROGRESS 才有 hold 表單;ON_HOLD 有 resume。改 IN_PROGRESS 驗表單:
    wo.status = "IN_PROGRESS"
    wo.hold_reason = None
    r2 = client.get("/app/work-orders/30318")
    assert 'name="hold_note"' not in r2.text             # 延誤說明欄已移除(2026-07-07)
    assert 'value="WAITING_MACHINE_TIME"' in r2.text     # 新等待原因選項
    assert 'value="OTHER"' not in r2.text                # #2:工程師 chips 面隱藏「其他」
    # REACTIVE 現態(IN_PROGRESS)走通用「進行中」;OPEN 才顯示「維修中」(下一測試蓋)
    assert "維修中" not in r2.text


def test_work_order_transition_invalid_is_noop(as_user, monkeypatch):
    from cmms.domain.work_order.service import InvalidTransition

    async def _start(self, no, actor, **kwargs):
        raise InvalidTransition("bad")

    monkeypatch.setattr(web_routes.WorkOrderService, "start_work", _start)
    r = client.post(
        "/app/work-orders/30318/transition", data={"action": "start"}, follow_redirects=False
    )
    assert r.status_code == 303                     # 非法轉移不炸,回詳情 + 顯性提示
    assert r.headers["location"] == "/app/work-orders/30318?msg=transition_err"


# ---- operator RBAC(iPad 產線共用帳號:只讀 + 開報修 + 取消自己誤報)----

def test_operator_role_label_i18n():
    assert i18n.translate("admin.role.operator", "en") == "Operator"
    assert i18n.translate("admin.role.operator", "zh-TW") == "作業員"
    assert i18n.translate("admin.role.operator", "vi") == "Vận hành"


def test_operator_detail_hides_write_ui(as_user, _detail_data, monkeypatch):
    """operator 詳情頁:無狀態 chips / 結單 / 指派 / 領料 / MRQ / 加日誌;僅取消自己開的 OPEN 單。"""
    as_user["user"].role = "operator"

    async def _get_open(self, no):
        return SimpleNamespace(
            work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
            status="OPEN", brief_description="堵塞", downtime_minutes=0,
            created_by="human:jlee", hold_reason=None, confirmed_reason_code=None,
            action_taken=None, labor_hours=None, assigned_vendor=None,
        )

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get_open)
    r = client.get("/app/work-orders/30318")
    assert r.status_code == 200
    assert "/transition" not in r.text     # 無狀態 chips / 結單鍵
    assert "/assign" not in r.text         # 無改派
    assert "/parts" not in r.text          # 無領料區
    assert "/links" not in r.text          # 無 MRQ 連結區
    assert "/notes" not in r.text          # 無加日誌 / 日誌更正
    assert "/cancel" in r.text             # 取消自己開的 OPEN 單(白名單)


def test_operator_detail_cannot_cancel_others(as_user, _detail_data, monkeypatch):
    """operator 對「別人開的」OPEN 單:取消表單不渲染(created_by != me)。"""
    as_user["user"].role = "operator"

    async def _get_open(self, no):
        return SimpleNamespace(
            work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
            status="OPEN", brief_description="堵塞", downtime_minutes=0,
            created_by="human:someone_else", hold_reason=None, confirmed_reason_code=None,
            action_taken=None, labor_hours=None, assigned_vendor=None,
        )

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get_open)
    r = client.get("/app/work-orders/30318")
    assert r.status_code == 200
    assert "/cancel" not in r.text         # 非自己開的 → 無取消表單


def test_operator_transition_blocked(as_user):
    """operator POST 狀態機 → route 先擋,303 回 transition_err(不觸 domain)。"""
    as_user["user"].role = "operator"
    r = client.post(
        "/app/work-orders/30318/transition", data={"action": "finish"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/30318?msg=transition_err"


def test_operator_cancel_own_ok(as_user, monkeypatch):
    """operator POST /cancel 自己開的單 → 放行(domain 管『自己開的』限制)。"""
    as_user["user"].role = "operator"
    captured: dict = {}

    async def _cancel(self, no, actor, *, at=None, reason=None):
        captured["no"] = no
        return SimpleNamespace(work_order_no=no, status="CANCELLED")

    monkeypatch.setattr(web_routes.WorkOrderService, "cancel_reactive_report", _cancel)
    r = client.post(
        "/app/work-orders/30318/cancel", data={"reason": "false alarm"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert captured["no"] == 30318


# ---- 備品查詢 ----

@pytest.fixture
def _inv_data(monkeypatch):
    item = SimpleNamespace(
        item_code="ES000804", name="ASMB 氣壓電磁閥", description="desc",
        vendor_part_no="VP-1", quantity_on_hand=2, reorder_point=5,
        bin_location="A-12", supplier="CMB",
        supplier_org_id="CMB", reorder_quantity=12.0,  # ADR-026(RFQ 按鈕 + 再訂購量)
        unit_cost=None, lead_time_weeks=None, weblink=None, comment=None,
        is_stocked=True, is_obsolete=False,             # admin 編輯表單欄位(完整 ORM 形狀)
    )

    async def _list(self, **kwargs):
        return [item] if kwargs.get("search") else []

    async def _get(self, code):
        return item if code == "ES000804" else None

    async def _alts(self, code):
        return ["ES000805"]

    async def _empty(self, code):
        return []

    async def _subs(self, code):
        return ["DISPENSER"]

    async def _all_subs(self):
        return [SimpleNamespace(code="DISPENSER", label="DISPENSER"),
                SimpleNamespace(code="ASMB", label="ASMB")]

    async def _list_atts(self, owner_type, owner_id, **kwargs):
        if owner_type == "inventory_item":
            return [SimpleNamespace(id=1, r2_bucket="b", r2_key="inventory/ES000804/x.jpg")]
        return []

    def _presign(self, att, **kwargs):
        return (f"memory://{att.r2_key}", 900)

    async def _first(self, owner_type, owner_ids):
        return {}

    async def _org(self, oid):
        return SimpleNamespace(org_id=oid, name="CMB Corp")

    async def _find_pending(self, **kw):
        return None

    monkeypatch.setattr(web_routes.InventoryService, "list_items", _list)
    monkeypatch.setattr(web_routes.InventoryService, "get_item", _get)
    monkeypatch.setattr(web_routes.InventoryService, "get_alternatives", _alts)
    monkeypatch.setattr(web_routes.InventoryService, "get_kit_children", _empty)
    monkeypatch.setattr(web_routes.InventoryService, "get_applicable_subtypes", _subs)
    monkeypatch.setattr(web_routes.InventoryService, "get_parent_kits", _empty)
    monkeypatch.setattr(web_routes.InventoryService, "list_all_asset_subtypes", _all_subs)
    monkeypatch.setattr(web_routes.ContactsService, "get_organization", _org)
    monkeypatch.setattr(web_routes.AttachmentService, "list_attachments", _list_atts)
    monkeypatch.setattr(web_routes.AttachmentService, "presigned_url", _presign)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    monkeypatch.setattr(web_routes.WorkOrderService, "find_pending_proposal", _find_pending)


def test_inventory_search(as_user, _inv_data):
    r = client.get("/app/inventory?q=ASMB")
    assert r.status_code == 200
    assert "ES000804" in r.text
    assert "ASMB 氣壓電磁閥" in r.text
    assert "badge--red" in r.text                 # on_hand 2 < reorder 5 → 低於再訂購點


def test_inventory_search_empty_query(as_user, _inv_data):
    r = client.get("/app/inventory")              # 無 q → 不查
    assert r.status_code == 200
    assert "ES000804" not in r.text


def test_inventory_detail(as_user, _inv_data):
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/inventory/es000804")     # 小寫 → 轉大寫
    assert r.status_code == 200
    assert "ES000804" in r.text
    assert "替代品" in r.text                       # inv.alternatives zh-TW
    assert "ES000805" in r.text                    # 替代品連結
    assert "適用機種" in r.text                      # inv.subtypes
    assert "inventory/ES000804/x.jpg" in r.text    # 照片 presigned url


def test_inventory_detail_admin_edit_batch(as_user, _inv_data):
    """#7:admin 明細有取消鈕、供應商自動完成 + 唯讀機構代碼、適用機種複選、盤點事由 datalist。"""
    as_user["user"].role = "admin"
    r = client.get("/app/inventory/ES000804")
    assert r.status_code == 200
    # #7a 取消鈕 = 退回明細連結
    assert 'href="/app/inventory/ES000804"' in r.text and "btn-ghost" in r.text
    # #7b/#7g supplier 自動完成 + 唯讀 org 代碼欄
    assert 'data-suggest="supplier"' in r.text and 'data-fill="edit-org-id"' in r.text
    assert 'id="edit-org-id"' in r.text and "readonly" in r.text
    # #7c 標籤正名
    assert "Plant description" in r.text and "Supplier description" in r.text
    # #7d 適用機種複選(select multiple 帶全清單,現行 DISPENSER 已選)
    assert 'name="subtypes" multiple' in r.text and "DISPENSER" in r.text
    # #7f 盤點事由 datalist
    assert 'list="adjust-reasons"' in r.text and "Cycle-count correction" in r.text


def test_inventory_set_subtypes_route(as_user, monkeypatch):
    """#7d:POST 設適用機種(admin);engineer 被擋。"""
    captured: dict = {}

    async def _set(self, item_code, subtypes, actor):
        captured.update(code=item_code, subs=subtypes, actor=actor.value)
        return sorted(subtypes)

    monkeypatch.setattr(web_routes.InventoryService, "set_applicable_subtypes", _set)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/es000804/subtypes",
                    data={"subtypes": ["DISPENSER", "ASMB"]}, follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES000804?rfq=saved"
    assert captured == {"code": "ES000804", "subs": ["DISPENSER", "ASMB"], "actor": "human:jlee"}
    # engineer → adminonly(不呼叫 domain)
    as_user["user"].role = "engineer"
    r2 = client.post("/app/inventory/es000804/subtypes",
                     data={"subtypes": ["ASMB"]}, follow_redirects=False)
    assert r2.headers["location"] == "/app/inventory/ES000804?rfq=adminonly"


# ---- 建新備品(create_item;admin-only)+ 照片管理 + 面板整併 ----


def test_inventory_new_form_admin(as_user):
    """admin GET 建新表單(200 + 表單 action + item_code 欄)。同時是 route 順序回歸測試:
    若 /inventory/new 被 /{item_code} 捕獲,session=None 的 detail 會炸而非回表單。"""
    as_user["user"].role = "admin"
    r = client.get("/app/inventory/new")
    assert r.status_code == 200
    assert 'action="/app/inventory/new"' in r.text
    assert 'name="item_code"' in r.text
    assert "New spare part" in r.text


def test_inventory_new_form_engineer_redirects(as_user):
    as_user["user"].role = "engineer"
    r = client.get("/app/inventory/new", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/inventory?msg=adminonly"


def test_inventory_create_admin(as_user, monkeypatch):
    """admin 建新品項 → 303 到明細?rfq=saved;期初在庫 → 另呼叫 adjust_on_hand。"""
    created: dict = {}
    adjusted: list = []

    async def _create(self, item_code, *, actor, name, **kw):
        created.update(code=item_code, name=name, actor=actor.value)
        return SimpleNamespace(item_code=item_code.upper())

    async def _adjust(self, item_code, *, new_quantity, reason, actor, idempotency_key=None):
        adjusted.append((item_code, new_quantity, reason, idempotency_key))
        return True

    monkeypatch.setattr(web_routes.InventoryService, "create_item", _create)
    monkeypatch.setattr(web_routes.InventoryService, "adjust_on_hand", _adjust)
    as_user["user"].role = "admin"
    # 無期初在庫 → 不呼叫 adjust
    r = client.post("/app/inventory/new",
                    data={"item_code": "new-1", "name": "Widget", "nonce": "nx"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/NEW-1?rfq=saved"
    assert created == {"code": "new-1", "name": "Widget", "actor": "human:jlee"}
    assert adjusted == []
    # 有期初在庫 → adjust 帶 webcreate 冪等鍵
    r2 = client.post("/app/inventory/new",
                     data={"item_code": "new-2", "name": "Bolt",
                           "initial_quantity": "12", "nonce": "ny"},
                     follow_redirects=False)
    assert r2.headers["location"] == "/app/inventory/NEW-2?rfq=saved"
    assert adjusted == [("NEW-2", "12", "initial count at item creation",
                         "webcreate:v1:ny:NEW-2:12")]


def test_inventory_create_with_photo(as_user, monkeypatch):
    """建立時附照片 → 品項建立後掛到新 item_code(owner=inventory_item)。"""
    atts: list = []

    async def _create(self, item_code, *, actor, name, **kw):
        return SimpleNamespace(item_code=item_code.upper())

    async def _add(self, *, owner_type, owner_id, ext, **kw):
        atts.append((owner_type, owner_id, ext))
        return SimpleNamespace(id=1), True

    monkeypatch.setattr(web_routes.InventoryService, "create_item", _create)
    monkeypatch.setattr(web_routes.AttachmentService, "add_attachment", _add)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/new",
                    data={"item_code": "new-3", "name": "Seal", "nonce": "nz"},
                    files={"photos": ("part.jpg", b"\xff\xd8jpg", "image/jpeg")},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/NEW-3?rfq=saved"
    assert atts == [("inventory_item", "NEW-3", "jpg")]


def test_inventory_create_err_and_engineer(as_user, monkeypatch):
    from cmms.domain.inventory.service import InventoryError

    async def _boom(self, item_code, *, actor, name, **kw):
        raise InventoryError("dup")

    monkeypatch.setattr(web_routes.InventoryService, "create_item", _boom)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/new",
                    data={"item_code": "X", "name": "y", "nonce": "n"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/new?msg=err"
    # engineer → adminonly(不呼叫 domain)
    as_user["user"].role = "engineer"
    r2 = client.post("/app/inventory/new",
                     data={"item_code": "X", "name": "y", "nonce": "n"},
                     follow_redirects=False)
    assert r2.headers["location"] == "/app/inventory?msg=adminonly"


def test_inventory_photo_upload(as_user, monkeypatch):
    """admin 上傳照片 → add_attachment(owner=inventory_item);engineer 擋;空檔 → photo_err。"""
    atts: list = []

    async def _get(self, code):
        return SimpleNamespace(item_code=code)

    async def _add(self, *, owner_type, owner_id, ext, **kw):
        atts.append((owner_type, owner_id, ext))
        return SimpleNamespace(id=1), True

    monkeypatch.setattr(web_routes.InventoryService, "get_item", _get)
    monkeypatch.setattr(web_routes.AttachmentService, "add_attachment", _add)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/es000804/photos",
                    files={"photos": ("new.jpg", b"\xff\xd8jpg", "image/jpeg")},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES000804?rfq=photo_ok"
    assert atts == [("inventory_item", "ES000804", "jpg")]
    # 空內容檔(有檔名但零位元組)→ 跳過 → photo_err(未上傳)
    r2 = client.post("/app/inventory/es000804/photos",
                     files={"photos": ("empty.jpg", b"", "image/jpeg")},
                     follow_redirects=False)
    assert r2.headers["location"] == "/app/inventory/ES000804?rfq=photo_err"
    # engineer → adminonly
    as_user["user"].role = "engineer"
    r3 = client.post("/app/inventory/es000804/photos",
                     files={"photos": ("x.jpg", b"j", "image/jpeg")},
                     follow_redirects=False)
    assert r3.headers["location"] == "/app/inventory/ES000804?rfq=adminonly"


def test_inventory_photo_delete(as_user, monkeypatch):
    """admin 刪照片:owner 相符 → 軟刪 + 乾淨轉址;不符 → photo_err 不呼叫;engineer 擋。"""
    deleted: list = []

    async def _get_att(self, att_id):
        # att 3 屬 ES000804;att 9 屬別的 owner(不符)
        if att_id == 3:
            return SimpleNamespace(id=3, owner_type="inventory_item", owner_id="ES000804")
        return SimpleNamespace(id=att_id, owner_type="inventory_item", owner_id="OTHER")

    async def _soft(self, att_id, actor, *, reason=None):
        deleted.append(att_id)
        return SimpleNamespace(id=att_id)

    monkeypatch.setattr(web_routes.AttachmentService, "get_attachment", _get_att)
    monkeypatch.setattr(web_routes.AttachmentService, "soft_delete_attachment", _soft)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/es000804/photos/3/delete", follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES000804"
    assert deleted == [3]
    # owner 不符 → photo_err,不軟刪
    r2 = client.post("/app/inventory/es000804/photos/9/delete", follow_redirects=False)
    assert r2.headers["location"] == "/app/inventory/ES000804?rfq=photo_err"
    assert deleted == [3]
    # engineer → adminonly
    as_user["user"].role = "engineer"
    r3 = client.post("/app/inventory/es000804/photos/3/delete", follow_redirects=False)
    assert r3.headers["location"] == "/app/inventory/ES000804?rfq=adminonly"
    assert deleted == [3]


def test_inventory_detail_admin_consolidated(as_user, _inv_data):
    """整併:admin 明細有單一「品項管理」面板,四個治理端點齊在同一入口下。"""
    as_user["user"].role = "admin"
    r = client.get("/app/inventory/ES000804")
    assert r.status_code == 200
    assert "Manage item (admin)" in r.text                          # inv.admin 單一入口
    assert "/app/inventory/ES000804/update" in r.text
    assert "/app/inventory/ES000804/subtypes" in r.text
    assert "/app/inventory/ES000804/adjust" in r.text
    assert 'action="/app/inventory/ES000804/photos"' in r.text      # 照片上傳
    assert 'name="photos"' in r.text and 'type="file"' in r.text


def test_inventory_list_admin_new_entry(as_user, _inv_data):
    """清單頁:admin 見「新增備品」入口;engineer 不見。"""
    as_user["user"].role = "admin"
    r = client.get("/app/inventory?q=ASMB")
    assert '/app/inventory/new' in r.text
    as_user["user"].role = "engineer"
    r2 = client.get("/app/inventory?q=ASMB")
    assert '/app/inventory/new' not in r2.text


# ---- 保養排程(PM)----

def test_pm_due_list(as_user, monkeypatch):
    pm = SimpleNamespace(
        pm_id="PM-001", asset_id="EID-001", task_id="T-1",
        next_due_date=date(2026, 6, 1), frequency_interval=90, frequency_unit="day",
    )

    async def _list(self, **kwargs):
        return [pm]

    async def _get_task(self, tid):
        return SimpleNamespace(description="季度保養")

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _list)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _get_task)
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/pm")
    assert r.status_code == 200
    assert "PM-001" in r.text
    assert "季度保養" in r.text                      # task description
    assert "EID-001" in r.text
    assert "逾期" in r.text                          # next_due 2026-06-01 < today → overdue


def test_pm_generate(as_user, monkeypatch):
    calls: list = []

    async def _gen(self, *, pm_id, actor, **kwargs):
        calls.append((pm_id, actor.value))
        return SimpleNamespace(work_order_no=555)

    monkeypatch.setattr(web_routes.WorkOrderService, "generate_pm_work_order", _gen)
    r = client.post("/app/pm/PM-001/generate", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/work-orders/555"
    assert calls == [("PM-001", "human:jlee")]


# ---- 工單佇列:分類標籤 + 綜合查詢 + owner/work_type(Slice 1)----

def test_work_order_queue_filter_and_search(as_user, monkeypatch):
    """分類標籤 → 狀態群組;q → search;都要傳進 service。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders?tab=waiting&q=CMB")
    assert r.status_code == 200
    assert captured["statuses"] == ["ON_HOLD"]        # waiting → ON_HOLD
    assert captured["search"] == "CMB"
    assert "tab=waiting" in r.text                      # chip 連結帶保留 q
    assert "q=CMB" in r.text


def test_work_order_queue_all_tab_no_status_filter(as_user, monkeypatch):
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    client.get("/app/work-orders?tab=all")              # 明確 all
    assert captured["statuses"] is None                 # all → 不過濾狀態


def test_work_order_queue_default_tab_is_active(as_user, monkeypatch):
    """Jordan 2026-07-05 #3d:「我的」預設 = 活單(OPEN/IN_PROGRESS/ON_HOLD;含 legacy O)。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders")                  # 無 tab → active
    assert captured["statuses"] == ["OPEN", "O", "IN_PROGRESS", "ON_HOLD"]
    assert "tab=cancelled" in r.text                    # 新分類 chip:已取消
    assert "tab=done" in r.text                         # 已結
    # 已取消群組 → CANCELLED/VOIDED
    client.get("/app/work-orders?tab=cancelled")
    assert captured["statuses"] == ["CANCELLED", "VOIDED"]


def test_work_order_queue_shows_owner_and_type(as_user, monkeypatch):
    async def _list(self, **kwargs):
        return [SimpleNamespace(
            work_order_no=24001, asset_id="EID-70021", work_type="PM", status="OPEN",
            brief_description="季度保養", downtime_minutes=None, opened_date=date(2026, 7, 1),
            assigned_person="Alice Fang", assigned_vendor="CMB",
        )]

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders")
    assert "Alice Fang" in r.text                      # owner 顯示(#2)
    assert "badge--indigo" in r.text                    # work_type=PM → wtype_css indigo(保養分色)


def test_inventory_default_browse(as_user, monkeypatch):
    """無查詢字串 → 預設列出(不再空白,#3)+ 縮圖(有照片顯示 img,#UI 缺失 2)。"""
    async def _list(self, **kwargs):
        return [SimpleNamespace(
            item_code="ES000804", name="ASMB 閥", description="d", quantity_on_hand=2,
            reorder_point=5, bin_location="A-12",
        )]

    async def _first(self, owner_type, owner_ids):
        assert owner_type == "inventory_item" and "ES000804" in owner_ids
        return {"ES000804": SimpleNamespace(r2_bucket="b", r2_key="inventory/ES000804/x.jpg")}

    def _presign(self, att, **kwargs):
        return (f"memory://{att.r2_key}", 900)

    monkeypatch.setattr(web_routes.InventoryService, "list_items", _list)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    monkeypatch.setattr(web_routes.AttachmentService, "presigned_url", _presign)
    r = client.get("/app/inventory")                    # 無 q
    assert r.status_code == 200
    assert "ES000804" in r.text                          # 預設就有內容
    assert "Recent parts" in r.text                      # inv.browse_hint(en)
    assert '<img src="memory://inventory/ES000804/x.jpg"' in r.text  # 卡片縮圖


# ---- Slice 2:我的/全部(assigned_person 過濾)----

def test_work_order_scope_mine_vs_all(as_user, monkeypatch):
    """mine → 用登入者 emaint_assignee 過濾;all → 不過濾指派人。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    client.get("/app/work-orders?scope=mine")
    assert captured["assigned_person"] == "Alice Fang"   # 命中登入者的指派名
    client.get("/app/work-orders?scope=all")
    assert captured["assigned_person"] is None             # 全部 → 不過濾


def test_work_order_scope_mine_empty_hint(as_user, monkeypatch):
    """監督者無 emaint_assignee → Mine 直接空 + 提示切 All,且不查 DB。"""
    called = {"n": 0}

    async def _list(self, **kwargs):
        called["n"] += 1
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    as_user["user"].emaint_assignee = None
    r = client.get("/app/work-orders?scope=mine")
    assert r.status_code == 200
    assert "switch to All" in r.text                       # scope.empty(en)
    assert called["n"] == 0                                 # 無指派名 → 不打 service


# ---- Slice 3:設備查詢(browse + detail + rollup)----

@pytest.fixture
def _equip_data(monkeypatch):
    main = SimpleNamespace(
        asset_id="EID-70021", description="Aligner46 打線機", asset_type="Production",
        asset_subtype="WIREBOND", department="EQ", line="10K", model_no="BX-820",
        serial_no="SN-1", manufacturer="Kestrel", available_for_service=True, is_active=True,
        site="PLANT-1", host_name=None, asset_ref=None, product=None, weblink=None,
        comments=None, process_segment_class=None,
    )
    module = SimpleNamespace(
        asset_id="EID-70005", description="Curer9 供料模組", asset_type="Production",
        asset_subtype="FEEDER", department="EQ", line="10K", model_no="", serial_no="",
        manufacturer=None, available_for_service=False, is_active=True,
        site="PLANT-1", host_name=None, asset_ref=None, product=None, weblink=None,
        comments=None, process_segment_class=None,
    )
    wo = SimpleNamespace(
        work_order_no=30318, asset_id="EID-70005", work_type="REACTIVE",
        status="IN_PROGRESS", brief_description="供料卡料",
    )

    async def _get(self, aid):
        return {"EID-70021": main, "EID-70005": module}.get(aid)

    async def _list(self, **kwargs):
        return [main]                       # 有 search 或預設瀏覽都回主機台

    async def _ext(self, aid):
        return [SimpleNamespace(namespace="mes_equipment", external_id="EID-70021")]

    async def _desc(self, aid):
        return ["EID-70005"]                # contains_module 後代

    async def _rollup(self, aid, **kwargs):
        return [wo]

    async def _usage(self, ids, **kw):
        return []

    async def _first(self, owner_type, owner_ids):
        return {}

    async def _atts(self, owner_type, owner_id, **kw):
        return []

    async def _types(self):
        return [SimpleNamespace(code="Production", label="Production"),
                SimpleNamespace(code="Meter", label="Meter")]

    async def _depts(self):
        return [SimpleNamespace(code="EQ", label="Equipment Eng.")]

    async def _lines(self):
        return [SimpleNamespace(code="10K", label="10K")]

    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get)
    monkeypatch.setattr(web_routes.AssetService, "list_assets", _list)
    monkeypatch.setattr(web_routes.AssetService, "list_external_ids", _ext)
    monkeypatch.setattr(web_routes.AssetService, "get_contained_descendants", _desc)
    monkeypatch.setattr(web_routes.AssetService, "rollup_work_orders", _rollup)
    monkeypatch.setattr(web_routes.AssetService, "list_asset_types", _types)
    monkeypatch.setattr(web_routes.AssetService, "list_departments", _depts)
    monkeypatch.setattr(web_routes.AssetService, "list_lines", _lines)
    monkeypatch.setattr(web_routes.InventoryService, "list_asset_part_usage", _usage)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    monkeypatch.setattr(web_routes.AttachmentService, "list_attachments", _atts)


def test_equipment_search(as_user, _equip_data):
    r = client.get("/app/equipment?q=aligner")
    assert r.status_code == 200
    assert "EID-70021" in r.text
    assert "Aligner46 打線機" in r.text


def test_equipment_browse_default(as_user, _equip_data):
    r = client.get("/app/equipment")                       # 無 q → 預設瀏覽(不空白)
    assert r.status_code == 200
    assert "EID-70021" in r.text
    assert "Recent equipment" in r.text                    # eq.browse_hint(en)


def test_equipment_detail(as_user, _equip_data):
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/equipment/eid-70021")             # 小寫 → 轉大寫
    assert r.status_code == 200
    assert "Aligner46 打線機" in r.text
    assert "WIREBOND" in r.text                            # asset_subtype
    assert "EID-70005" in r.text                           # 模組 chip(組成)
    assert "Curer9 供料模組" in r.text
    assert "WO-30318" in r.text                            # rollup 工單(含後代模組)
    assert "供料卡料" in r.text
    assert "/app/report?eid=EID-70021" in r.text           # 報修 CTA
    assert "近期工單" in r.text                             # eq.recent_wos zh-TW


def test_equipment_detail_not_found(as_user, _equip_data):
    r = client.get("/app/equipment/EID-NOPE")              # get_asset → None → placeholder
    assert r.status_code == 200
    assert "EID-NOPE" in r.text                            # placeholder screen_title


# ---- 建新資產(admin-only;create_asset,內部規格)----

def test_equipment_new_form_admin(as_user, _equip_data):
    """admin GET 建新表單 → 200 + 表單(lookup 下拉)。"""
    as_user["user"].role = "admin"
    r = client.get("/app/equipment/new")
    assert r.status_code == 200
    assert 'action="/app/equipment/new"' in r.text
    assert 'name="asset_id"' in r.text                     # EID 可編輯欄
    assert "New equipment" in r.text                       # eq.create.title(en)
    assert "Production" in r.text                           # asset_type 下拉


def test_equipment_new_form_engineer_redirects(as_user, _equip_data):
    """engineer GET → 轉設備清單(adminonly),不渲染表單。"""
    as_user["user"].role = "engineer"
    r = client.get("/app/equipment/new", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/equipment?msg=adminonly"


def test_equipment_create_admin(as_user, monkeypatch):
    """admin POST → 呼叫 create_asset 並轉新設備明細頁(msg=saved)。"""
    captured: dict = {}

    async def _create(self, asset_id, *, actor, **kwargs):
        captured.update(asset_id=asset_id, actor=actor.value, **kwargs)
        return SimpleNamespace(asset_id=asset_id.strip().upper())

    monkeypatch.setattr(web_routes.AssetService, "create_asset", _create)
    as_user["user"].role = "admin"
    r = client.post(
        "/app/equipment/new",
        data={"asset_id": "eid-90001", "description": "P2 arm",
              "asset_type": "Production", "site": "PLANT-1", "model_no": "ARM-1",
              "owners": ["Owner Bob", "Ben Yeh"]},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/equipment/EID-90001?msg=saved"
    assert captured["asset_id"] == "eid-90001"
    assert captured["actor"] == "human:jlee"
    assert captured["description"] == "P2 arm"
    assert captured["asset_type"] == "Production"
    assert captured["owners"] == ["Owner Bob", "Ben Yeh"]  # 0031:多負責人傳入 create_asset


def test_equipment_create_admin_error_redirects(as_user, monkeypatch):
    """create_asset 拋 AssetError → 轉回建新表單(msg=err)。"""
    async def _create(self, asset_id, **kwargs):
        raise AssetError("bad eid")

    monkeypatch.setattr(web_routes.AssetService, "create_asset", _create)
    as_user["user"].role = "admin"
    r = client.post(
        "/app/equipment/new",
        data={"asset_id": "bad", "description": "x",
              "asset_type": "Production", "site": "PLANT-1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/equipment/new?msg=err"


def test_equipment_create_engineer_blocked(as_user, monkeypatch):
    """engineer POST → adminonly,domain 未被呼叫。"""
    called = {"n": 0}

    async def _create(self, asset_id, **kwargs):
        called["n"] += 1
        return SimpleNamespace(asset_id=asset_id)

    monkeypatch.setattr(web_routes.AssetService, "create_asset", _create)
    as_user["user"].role = "engineer"
    r = client.post(
        "/app/equipment/new",
        data={"asset_id": "EID-90001", "description": "x",
              "asset_type": "Production", "site": "PLANT-1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/app/equipment?msg=adminonly"
    assert called["n"] == 0


def test_equipment_nav_and_wo_detail_link(as_user, _equip_data, monkeypatch):
    """導覽含設備入口;工單詳情的 EID 連到設備明細(手機路徑)。"""
    wo = SimpleNamespace(
        work_order_no=30318, asset_id="EID-70021", work_type="REACTIVE",
        status="IN_PROGRESS", brief_description="x", downtime_minutes=None,
    )

    async def _get_wo(self, no):
        return wo

    async def _notes(self, no):
        return []

    async def _atts(self, owner_type, owner_id, **kwargs):
        return []

    async def _empty2(self, no):
        return []

    async def _find_pending(self, **kw):
        return None

    async def _hold_reasons(self):
        return []

    async def _atts_map(self, owner_type, owner_ids):
        return {}

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get_wo)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_notes", _notes)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_parts", _empty2)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_external_links", _empty2)
    monkeypatch.setattr(web_routes.WorkOrderService, "find_pending_proposal", _find_pending)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_hold_reasons", _hold_reasons)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_confirmed_reason_options", _empty2)
    monkeypatch.setattr(web_routes.AttachmentService, "attachments_map", _atts_map)
    r = client.get("/app/work-orders/30318")
    assert r.status_code == 200
    assert 'href="/app/equipment/EID-70021"' in r.text     # EID → 設備明細連結
    assert "/app/equipment" in r.text                       # 導覽設備入口


# ---- Slice 4:供應商查詢(browse + detail + PII gate)----

@pytest.fixture
def _sup_data(monkeypatch):
    org = SimpleNamespace(
        org_id="NORDIC", name="NORDIC GmbH", org_type="Supplier", is_active=True,
        website="https://nordic.example.com", address="Munich, DE", phone="+49-89-1",
    )
    person = SimpleNamespace(
        person_id="HANS01", org_id="NORDIC", category="Sales", full_name="Hans Muller",
        first_name="Hans", last_name="Muller", email="hans@nordic.example.com",
        work_phone="+49-89-2", extension="12", mobile="+49-170-9", work_address="Munich",
    )

    async def _list_orgs(self, **kwargs):
        return [org]

    async def _get_org(self, oid):
        return org if oid == "NORDIC" else None

    async def _list_persons(self, oid):
        return [person]

    async def _list_org_types(self):
        return [SimpleNamespace(code="Supplier", label="Supplier")]

    async def _list_categories(self):
        return [SimpleNamespace(code="Sales", label="Sales")]

    monkeypatch.setattr(web_routes.ContactsService, "list_organizations", _list_orgs)
    monkeypatch.setattr(web_routes.ContactsService, "get_organization", _get_org)
    monkeypatch.setattr(web_routes.ContactsService, "list_org_persons", _list_persons)
    monkeypatch.setattr(web_routes.ContactsService, "list_org_types", _list_org_types)
    monkeypatch.setattr(web_routes.ContactsService, "list_contact_categories", _list_categories)
    return {"org": org, "person": person}


def test_suppliers_browse_default(as_user, _sup_data):
    r = client.get("/app/suppliers")                       # 無 q → 預設瀏覽
    assert r.status_code == 200
    assert "NORDIC GmbH" in r.text
    assert "Recent organizations" in r.text                # sup.browse_hint(en)


def test_suppliers_search(as_user, _sup_data, monkeypatch):
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return [_sup_data["org"]]

    monkeypatch.setattr(web_routes.ContactsService, "list_organizations", _list)
    r = client.get("/app/suppliers?q=NORDIC")
    assert r.status_code == 200
    assert captured.get("search") == "NORDIC"
    assert 'href="/app/suppliers/NORDIC"' in r.text


def test_supplier_detail_engineer_hides_pii(as_user, _sup_data):
    r = client.get("/app/suppliers/NORDIC")                # engineer(預設 role)
    assert r.status_code == 200
    assert "Hans Muller" in r.text                          # 姓名 = 非 PII,顯示
    assert "hans@nordic.example.com" not in r.text          # email PII → 隱藏(PersonSummary 不含)
    assert "+49-170-9" not in r.text                        # mobile PII → 隱藏
    assert "admins only" in r.text                          # sup.pii_admin_only(en)


def test_supplier_detail_admin_shows_pii(as_user, _sup_data):
    as_user["user"].role = "admin"
    r = client.get("/app/suppliers/NORDIC")
    assert r.status_code == 200
    assert "hans@nordic.example.com" in r.text                      # email → admin 可見(PersonRead)
    assert "+49-170-9" in r.text                            # mobile → admin 可見


def test_supplier_detail_admin_edit_forms(as_user, _sup_data):
    """#6:admin 明細有機構編輯(org_id 唯讀)、聯絡人編輯、新增聯絡人表單。"""
    as_user["user"].role = "admin"
    r = client.get("/app/suppliers/NORDIC")
    assert r.status_code == 200
    # #6a 機構編輯 + org_id 唯讀
    assert 'action="/app/suppliers/NORDIC/update"' in r.text
    assert "readonly" in r.text and "Org code (read-only)" in r.text
    # #6b 編輯既有聯絡人 + 新增聯絡人
    assert 'action="/app/suppliers/NORDIC/persons/HANS01/update"' in r.text
    assert 'action="/app/suppliers/NORDIC/persons"' in r.text
    assert "Add contact (admin)" in r.text


def test_supplier_detail_engineer_no_edit_forms(as_user, _sup_data):
    """#6c:engineer 對機構/聯絡人唯讀 —— 無任何編輯表單。"""
    r = client.get("/app/suppliers/NORDIC")                 # engineer(預設 role)
    assert r.status_code == 200
    assert "/app/suppliers/NORDIC/update" not in r.text
    assert "/app/suppliers/NORDIC/persons" not in r.text


def test_supplier_update_route(as_user, monkeypatch, _sup_data):
    """#6a:POST 機構編輯(admin)→ domain update_organization;engineer 被擋。"""
    captured: dict = {}

    async def _upd(self, org_id, **kw):
        captured.update(org_id=org_id, **kw)
        return SimpleNamespace(org_id=org_id)

    monkeypatch.setattr(web_routes.ContactsService, "update_organization", _upd)
    as_user["user"].role = "admin"
    r = client.post("/app/suppliers/NORDIC/update",
                    data={"name": "NORDIC X", "org_type": "Supplier", "website": "",
                          "address": "", "phone": "02-1"}, follow_redirects=False)
    assert r.headers["location"] == "/app/suppliers/NORDIC?msg=saved"
    assert captured["org_id"] == "NORDIC" and captured["name"] == "NORDIC X"
    assert captured["actor"].value == "human:jlee"
    as_user["user"].role = "engineer"
    r2 = client.post("/app/suppliers/NORDIC/update",
                     data={"name": "X"}, follow_redirects=False)
    assert r2.headers["location"] == "/app/suppliers/NORDIC?msg=adminonly"


# ---- 新增供應商(create_organization;admin-only)+ 清單入口 ----


def test_supplier_new_form_admin(as_user, _sup_data):
    """admin GET 建新表單(200 + 表單 action + name 欄)。同時是 route 順序回歸:
    若 /suppliers/new 被 /{org_id} 捕獲,session=None 的 detail 會炸而非回表單。"""
    as_user["user"].role = "admin"
    r = client.get("/app/suppliers/new")
    assert r.status_code == 200
    assert 'action="/app/suppliers/new"' in r.text
    assert 'name="name"' in r.text


def test_supplier_new_form_engineer_redirects(as_user, _sup_data):
    as_user["user"].role = "engineer"
    r = client.get("/app/suppliers/new", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/suppliers?msg=adminonly"


def test_supplier_create_admin(as_user, monkeypatch):
    """admin 建新供應商 → 303 到明細?msg=saved;captured kwargs 帶 name + actor。"""
    captured: dict = {}

    async def _create(self, *, actor, name, **kw):
        captured.update(name=name, actor=actor.value, **kw)
        return SimpleNamespace(org_id="ACME_PUMPS")

    monkeypatch.setattr(web_routes.ContactsService, "create_organization", _create)
    as_user["user"].role = "admin"
    r = client.post("/app/suppliers/new",
                    data={"name": "Acme Pumps", "org_type": "Supplier",
                          "website": "https://acme.x", "address": "", "phone": ""},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/suppliers/ACME_PUMPS?msg=saved"
    assert captured["name"] == "Acme Pumps"
    assert captured["actor"] == "human:jlee"
    assert captured["org_type"] == "Supplier"


def test_supplier_create_err_and_engineer(as_user, monkeypatch):
    from cmms.domain.contacts.service import ContactsError

    called: list = []

    async def _boom(self, *, actor, name, **kw):
        called.append(name)
        raise ContactsError("dup")

    monkeypatch.setattr(web_routes.ContactsService, "create_organization", _boom)
    as_user["user"].role = "admin"
    r = client.post("/app/suppliers/new",
                    data={"name": "X"}, follow_redirects=False)
    assert r.headers["location"] == "/app/suppliers/new?msg=err"
    # engineer → adminonly(不呼叫 domain)
    as_user["user"].role = "engineer"
    r2 = client.post("/app/suppliers/new",
                     data={"name": "X"}, follow_redirects=False)
    assert r2.headers["location"] == "/app/suppliers?msg=adminonly"
    assert called == ["X"]  # 只有 admin 那次呼叫了 domain


def test_suppliers_list_create_entry_admin_only(as_user, _sup_data):
    """清單頁:admin 見「新增供應商」入口;engineer 不見。"""
    as_user["user"].role = "admin"
    r = client.get("/app/suppliers")
    assert "/app/suppliers/new" in r.text
    as_user["user"].role = "engineer"
    r2 = client.get("/app/suppliers")
    assert "/app/suppliers/new" not in r2.text


def test_pm_list_admin_create_entry(as_user, monkeypatch):
    """PM 清單頁:admin 見連往後台 PM 編輯的入口;engineer 不見。"""
    async def _list(self, **kwargs):
        return []

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _list)
    as_user["user"].role = "admin"
    r = client.get("/app/pm")
    assert 'href="/admin/pm"' in r.text
    as_user["user"].role = "engineer"
    r2 = client.get("/app/pm")
    assert 'href="/admin/pm"' not in r2.text


def test_supplier_detail_not_found(as_user, _sup_data):
    r = client.get("/app/suppliers/NOPE")                  # get_organization → None → placeholder
    assert r.status_code == 200
    assert "NOPE" in r.text


def test_suppliers_zh_labels(as_user, _sup_data):
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/suppliers")
    assert "供應商" in r.text                                # nav.suppliers(zh-TW)


# ---- PM(a):月曆視圖 ----

@pytest.fixture
def _pm_cal_data(monkeypatch):
    pm = SimpleNamespace(
        pm_id="PM-CAL", asset_id="EID-777", task_id="T-9",
        next_due_date=date(2026, 7, 10), frequency_interval=90, frequency_unit="day",
    )
    wo = SimpleNamespace(
        work_order_no=6001, asset_id="EID-777", work_type="PM", status="OPEN",
        brief_description="季保", opened_date=date(2026, 7, 10),
    )

    async def _pm(self, **kwargs):
        return [pm]

    async def _wo(self, **kwargs):
        return [wo]

    async def _task(self, tid):
        return SimpleNamespace(description="季度保養")

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _pm)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _wo)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)


def test_pm_calendar_month_renders(as_user, _pm_cal_data):
    r = client.get("/app/pm/calendar?view=month&d=2026-07-15")  # 顯式錨定 → 7/10 格必存在
    assert r.status_code == 200
    assert "2026-07" in r.text                              # 月標題
    assert "EID-777" in r.text                              # 到期 PM 標記
    assert "focus=PM-CAL" in r.text                         # 點擊 → 聚焦該筆(非全清單)
    assert "/app/work-orders/6001" in r.text                # 已生成 PM 工單連結
    assert "cal-grid" in r.text


def test_pm_calendar_month_nav(as_user, _pm_cal_data):
    r = client.get("/app/pm/calendar?view=month&d=2026-01-15")
    assert r.status_code == 200
    assert "d=2025-12-01" in r.text                         # prev(2026-01 → 2025-12)
    assert "d=2026-02-01" in r.text                         # next → 2026-02
    assert "view=week" in r.text and "view=day" in r.text   # 三視圖 toggle
    r2 = client.get("/app/pm")                              # 清單頁也有 toggle
    assert "/app/pm/calendar" in r2.text


def test_pm_calendar_week_view(as_user, _pm_cal_data):
    # 2026-07-08(週三)→ 週日起始 7/5–7/11;7/10 的到期 PM 以完整內容顯示(任務名)
    r = client.get("/app/pm/calendar?view=week&d=2026-07-08")
    assert r.status_code == 200
    assert "2026-07-05" in r.text and "2026-07-11" in r.text  # 週標題範圍
    assert "季度保養" in r.text                                # 週視圖顯示任務名(格子大→內容多)
    assert "d=2026-06-28" in r.text                            # prev = −7 天
    assert "d=2026-07-12" in r.text                            # next = +7 天


def test_pm_calendar_day_view_has_execute(as_user, monkeypatch):
    """日視圖:已到期 PM 顯示「補開工單」鈕(#5e 降級 —— 只在已到期且本期未生成時出現)。"""
    pm = SimpleNamespace(
        pm_id="PM-DUE", asset_id="EID-777", task_id="T-9",
        next_due_date=date(2026, 6, 1), frequency_interval=90, frequency_unit="day",
        assigned_person=None, last_work_order_no=None, is_suppressed=False,
    )

    async def _pm(self, **kwargs):
        return [pm]

    async def _wo(self, **kwargs):
        return []

    async def _task(self, tid):
        return SimpleNamespace(description="季度保養")

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _pm)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _wo)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)
    r = client.get("/app/pm/calendar?view=day&d=2026-06-01")  # PM 到期日(過去)→ 鈕顯示
    assert r.status_code == 200
    assert "季度保養" in r.text
    assert "/app/pm/PM-DUE/backfill" in r.text              # 日視圖含補開工單鈕(#5b:連結至確認頁)
    assert "d=2026-05-31" in r.text and "d=2026-06-02" in r.text  # prev/next ±1 天


def test_pm_focus_single(as_user, monkeypatch):
    """月曆點某保養項 → /app/pm?focus=<pm_id> 只顯示該筆(非全部清單)+ 保養細項(#5c)。"""
    pm = SimpleNamespace(
        pm_id="PM-X", asset_id="EID-9", task_id="T-9",
        next_due_date=date(2026, 7, 10), frequency_interval=30, frequency_unit="day",
        assigned_person=None, last_work_order_no=None,
    )

    async def _get(self, pm_id):
        return pm if pm_id == "PM-X" else None

    async def _task(self, tid):
        return SimpleNamespace(description="聚焦任務")

    async def _steps(self, tn):
        return [SimpleNamespace(id=1, proc_seq=10, task_desc="檢查吸嘴")]

    async def _parts(self, ids):
        return {1: [SimpleNamespace(item_code="EC000807", replace_qty=2)]}

    monkeypatch.setattr(web_routes.PmScheduleService, "get_pm_schedule", _get)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)
    monkeypatch.setattr(web_routes.TaskService, "get_task_steps", _steps)
    monkeypatch.setattr(web_routes.TaskService, "get_parts_for_steps", _parts)
    r = client.get("/app/pm?focus=PM-X")
    assert r.status_code == 200
    assert "PM-X" in r.text and "聚焦任務" in r.text
    assert "Show all" in r.text                             # pm.focus.showall(en)退路
    assert "檢查吸嘴" in r.text                              # 保養細項步驟(#5c)
    assert 'href="/app/inventory/EC000807"' in r.text       # 每步用料連到備品明細


def test_detail_pages_have_back_link(as_user, _detail_data):
    r = client.get("/app/work-orders/30318")
    assert 'class="backlink"' in r.text                     # 詳情頁返回鍵(UI 缺失 1)
    assert 'href="/app/work-orders"' in r.text


def test_sidebar_has_settings_and_admin_links(as_user):
    """桌面入口(UI 缺失 4):側欄使用者區 → 設定;admin 才有管理齒輪。"""
    r = client.get("/app/report")
    assert 'class="sidebar__userlink"' in r.text            # 頭像/名字 → 設定
    assert r.text.count('href="/admin"') == 0               # engineer 無管理入口
    # admin → 齒輪出現(側欄 + topbar 兩處)
    as_user["user"].role = "admin"
    r2 = client.get("/app/report")
    assert 'href="/admin"' in r2.text


def test_report_scan_zxing_fallback_wired(as_user):
    r = client.get("/app/report")
    assert "/static/zxing.min.js" in r.text                 # 無原生 API → ZXing fallback
    assert "BarcodeDetector" in r.text


def test_pm_calendar_unauth(anon):
    r = client.get("/app/pm/calendar", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


# ---- /admin 管理台(RBAC gating + 帳號操作)+ /app/settings ----

def test_admin_requires_login(anon):
    r = client.get("/admin/accounts", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


def test_admin_forbids_engineer(as_user):
    r = client.get("/admin/accounts")                      # 預設 engineer
    assert r.status_code == 403


def test_admin_accounts_lists(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _list(self, **kwargs):
        return [SimpleNamespace(
            user_id="tony", username="tony", display_name="Ben Yeh", org="contractor",
            role="engineer", is_active=True, emaint_assignee="Ben Yeh",
        )]

    monkeypatch.setattr(web_routes.IdentityService, "list_users", _list)
    r = client.get("/admin/accounts")
    assert r.status_code == 200
    assert "tony" in r.text and "Ben Yeh" in r.text


def test_admin_create_account(as_user, monkeypatch):
    as_user["user"].role = "admin"
    captured: dict = {}

    async def _create(self, **kwargs):
        captured.update(kwargs)
        return kwargs["user_id"]

    monkeypatch.setattr(web_routes.IdentityService, "create_user", _create)
    # 表單不再送 user_id;server 由 username(strip 後)自動導出 user_id == username。
    r = client.post("/admin/accounts", data={
        "username": "  newbie  ", "display_name": "New Bie",
        "org": "contractor", "role": "engineer", "password": "secret12",
        "emaint_assignee": "New Bie",
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/accounts"
    assert captured["user_id"] == "newbie" == captured["username"]  # 導出 + 前後空白已 strip
    assert captured["role"] == "engineer"
    assert captured["actor"].value == "human:jlee"       # 操作者 = 登入 admin


def test_admin_mutations_call_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _deact(self, uid, *, actor):
        calls.append(("deactivate", uid, actor.value))

    async def _role(self, uid, role, *, actor):
        calls.append(("role", uid, role, actor.value))

    async def _reset(self, uid, pw, *, actor):
        calls.append(("reset", uid, pw, actor.value))

    async def _assignee(self, uid, *, assignee, actor):
        calls.append(("assignee", uid, assignee, actor.value))

    monkeypatch.setattr(web_routes.IdentityService, "deactivate_user", _deact)
    monkeypatch.setattr(web_routes.IdentityService, "set_role", _role)
    monkeypatch.setattr(web_routes.IdentityService, "reset_password", _reset)
    monkeypatch.setattr(web_routes.IdentityService, "set_emaint_assignee", _assignee)
    client.post("/admin/accounts/tony/deactivate", follow_redirects=False)
    client.post("/admin/accounts/tony/role", data={"role": "admin"}, follow_redirects=False)
    client.post("/admin/accounts/tony/reset-password", data={"new_password": "newpass12"},
                follow_redirects=False)
    client.post("/admin/accounts/tony/assignee", data={"assignee": "Tony L"},
                follow_redirects=False)
    assert ("deactivate", "tony", "human:jlee") in calls
    assert ("role", "tony", "admin", "human:jlee") in calls
    assert ("reset", "tony", "newpass12", "human:jlee") in calls
    assert ("assignee", "tony", "Tony L", "human:jlee") in calls


def test_admin_mutation_error_redirects_with_msg(as_user, monkeypatch):
    as_user["user"].role = "admin"
    from cmms.domain.identity.service import IdentityError

    async def _deact(self, uid, *, actor):
        raise IdentityError("cannot deactivate the last active admin")

    monkeypatch.setattr(web_routes.IdentityService, "deactivate_user", _deact)
    r = client.post("/admin/accounts/me/deactivate", follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]  # 守門錯誤 → banner


async def _no_creds(self, uid):
    return []


def test_settings_page_renders(as_user, monkeypatch):
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)
    r = client.get("/app/settings")
    assert r.status_code == 200
    assert 'name="current_password"' in r.text and 'name="new_password"' in r.text


def test_settings_change_password_ok(as_user, monkeypatch):
    async def _cp(self, uid, old, new, *, actor):
        return None

    monkeypatch.setattr(web_routes.IdentityService, "change_password", _cp)
    r = client.post("/app/settings/password",
                    data={"current_password": "a", "new_password": "secret12"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/settings?changed=1"


def test_settings_change_password_wrong(as_user, monkeypatch):
    from cmms.domain.identity.service import AuthenticationError

    async def _cp(self, uid, old, new, *, actor):
        raise AuthenticationError("nope")

    monkeypatch.setattr(web_routes.IdentityService, "change_password", _cp)
    r = client.post("/app/settings/password", data={"current_password": "x", "new_password": "y"})
    assert r.status_code == 200
    assert "incorrect" in r.text.lower()                    # settings.password.wrong(en)


def test_settings_telegram_generate_code_shows_once(as_user, monkeypatch):
    """產生綁定碼 → 直接渲染設定頁,明文碼內嵌顯示一次 + deep link;不 redirect(碼不進 URL)。"""
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)

    async def _mk(self, user_id, actor):
        return "CODE-XYZ-123"

    monkeypatch.setattr(web_routes.TelegramBridgeService, "create_link_code", _mk)
    r = client.post("/app/settings/telegram/code", follow_redirects=False)
    assert r.status_code == 200                               # 直接渲染,非 303
    assert "CODE-XYZ-123" in r.text                           # 明文碼顯示一次
    assert "?start=CODE-XYZ-123" in r.text                    # deep link 帶碼


def test_settings_telegram_unlink_redirects(as_user, monkeypatch):
    called = {}

    async def _unlink(self, user_id, actor):
        called["uid"] = user_id
        return True

    monkeypatch.setattr(web_routes.TelegramBridgeService, "unlink", _unlink)
    r = client.post("/app/settings/telegram/unlink", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app/settings"
    assert called["uid"] == "jlee"


def test_settings_telegram_bound_state(as_user, monkeypatch):
    """已綁定 → 顯示解除綁定表單 + chat_id 尾 4 碼。"""
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)

    async def _link(self, user_id):
        return SimpleNamespace(user_id=user_id, chat_id="123456789")

    monkeypatch.setattr(web_routes.TelegramBridgeService, "get_link", _link)
    r = client.get("/app/settings")
    assert r.status_code == 200
    assert "/app/settings/telegram/unlink" in r.text
    assert "6789" in r.text                                   # chat_id 尾 4 碼


def test_admin_link_only_for_admin(as_user, monkeypatch):
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)
    r = client.get("/app/settings")                         # engineer
    assert "/app/settings" in r.text                         # avatar 連到設定
    assert 'href="/admin"' not in r.text                     # engineer 不見 admin 入口
    as_user["user"].role = "admin"
    r2 = client.get("/app/settings")
    assert 'href="/admin"' in r2.text                        # admin 才見 admin 入口


# ---- /admin PM master:保養任務 + 細項瀏覽(PM(b) task_step/task_part)----

def test_admin_pm_lists_tasks(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _list(self, **kwargs):
        return [SimpleNamespace(task_no="TSK0007", description="Sorter PM")]

    monkeypatch.setattr(web_routes.TaskService, "list_tasks", _list)
    r = client.get("/admin/pm")
    assert r.status_code == 200
    assert "TSK0007" in r.text and "Sorter PM" in r.text
    assert 'href="/admin/pm/TSK0007"' in r.text


def test_admin_pm_task_steps(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _get(self, tn):
        if tn != "TSK0007":
            return None
        return SimpleNamespace(task_no="TSK0007", description="Sorter PM")

    async def _steps(self, tn):
        return [
            SimpleNamespace(id=1, proc_seq=10, task_desc="Clean the head"),
            SimpleNamespace(id=2, proc_seq=20, task_desc="Replace the blade"),
        ]

    async def _parts(self, ids):
        return {2: [SimpleNamespace(item_code="EC000807", replace_qty=2)]}

    async def _scheds(self, **kw):
        return [SimpleNamespace(
            pm_id="P1", asset_id="EID-001", task_id="TSK0007", frequency_interval=3,
            frequency_unit="Months", next_due_date=date(2026, 8, 1),
            assigned_person="Iris Chiu", is_suppressed=False,
        )]

    monkeypatch.setattr(web_routes.TaskService, "get_task", _get)
    monkeypatch.setattr(web_routes.TaskService, "get_task_steps", _steps)
    monkeypatch.setattr(web_routes.TaskService, "get_parts_for_steps", _parts)
    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _scheds)
    r = client.get("/admin/pm/TSK0007")
    assert r.status_code == 200
    assert "Clean the head" in r.text and "Replace the blade" in r.text
    assert "EC000807" in r.text                              # 步驟 2 的用料
    assert "<ol" in r.text                                   # 有序清單 = 顯示 1..N
    # PM 排程區:串好的設備 + 編輯表單 + 暫停鈕 + 新增排程表單(Jordan #2)
    assert "EID-001" in r.text and 'name="next_due_date"' in r.text
    assert "/admin/pm/TSK0007/schedules/P1/suppress" in r.text
    assert "/admin/pm/TSK0007/schedules" in r.text
    assert "/admin/pm/TSK0007/steps" in r.text                 # 加步驟表單


# ---- 自主收尾批:條碼掃描 / 直領 / RFQ 一鍵 / PAT 表單 / 提案審核 ----

def test_report_barcode_scan_present(as_user):
    """報修表單含條碼掃描(BarcodeDetector feature-detect;不支援自動隱藏、手動輸入照常)。"""
    r = client.get("/app/report")
    assert 'id="scan-btn"' in r.text
    assert "BarcodeDetector" in r.text


def test_equipment_parts_usage_renders(as_user, _equip_data, monkeypatch):
    async def _usage(self, ids, **kw):
        assert "EID-70021" in ids and "EID-70005" in ids     # 自身 + 後代模組
        return [
            SimpleNamespace(item_code="ES001", qty_delta=-2.0, work_order_no=None,
                            occurred_at=datetime(2026, 7, 1, 10, 0), txn_id=2, kind="ISSUE"),
            SimpleNamespace(item_code="ES002", qty_delta=-1.0, work_order_no=30318,
                            occurred_at=datetime(2026, 6, 30, 9, 0), txn_id=1, kind="ISSUE"),
            # 取消/改量的補償 RETURN 帳 → 誠實呈現(+數量 + Return badge)
            SimpleNamespace(item_code="ES003", qty_delta=3.0, work_order_no=None,
                            occurred_at=datetime(2026, 6, 29, 9, 0), txn_id=3, kind="RETURN"),
        ]

    async def _cancelled(self, ids):
        return set()

    monkeypatch.setattr(web_routes.InventoryService, "list_asset_part_usage", _usage)
    monkeypatch.setattr(web_routes.InventoryService, "cancelled_asset_issue_ids", _cancelled)
    r = client.get("/app/equipment/EID-70021")
    assert r.status_code == 200
    assert 'href="/app/inventory/ES001"' in r.text           # 料號連結
    assert "Direct issue" in r.text                           # 直領 badge(en)
    assert "WO-30318" in r.text                               # 工單領料連結
    assert "/app/equipment/EID-70021/issue" in r.text         # 直領表單 action
    # 直領列(ES001 txn 2)有改量/取消入口;工單領料列(txn 1)沒有
    assert "/app/equipment/EID-70021/issue/2/update" in r.text
    assert "/app/equipment/EID-70021/issue/2/cancel" in r.text
    assert "/app/equipment/EID-70021/issue/1/update" not in r.text
    assert "Return" in r.text                                 # RETURN 補償帳 badge(en)


def test_equipment_issue_amend_and_cancel_post(as_user, monkeypatch):
    calls: dict = {}

    async def _upd(self, *, asset_id, txn_id, new_quantity, actor, idempotency_key=None):
        calls["upd"] = (asset_id, txn_id, new_quantity, actor.value, idempotency_key)
        return True

    async def _cancel(self, *, asset_id, txn_id, actor):
        calls["cancel"] = (asset_id, txn_id, actor.value)
        return True

    monkeypatch.setattr(web_routes.InventoryService, "update_asset_issue_quantity", _upd)
    monkeypatch.setattr(web_routes.InventoryService, "cancel_asset_issue", _cancel)
    r = client.post("/app/equipment/eid-70021/issue/7/update",
                    data={"quantity": "5", "nonce": "n9"}, follow_redirects=False)
    assert r.headers["location"] == "/app/equipment/EID-70021?issue=upd"
    assert calls["upd"] == ("EID-70021", 7, "5", "human:jlee", "webissueupd:v1:n9:7:5")
    r2 = client.post("/app/equipment/EID-70021/issue/7/cancel",
                     data={"nonce": "n9"}, follow_redirects=False)
    assert r2.headers["location"] == "/app/equipment/EID-70021?issue=cancelled"
    assert calls["cancel"] == ("EID-70021", 7, "human:jlee")


def test_equipment_issue_part_post(as_user, monkeypatch):
    captured: dict = {}

    async def _issue(self, *, asset_id, item_code, quantity, actor, reason=None,
                     idempotency_key=None, at=None):
        captured.update(asset_id=asset_id, item_code=item_code, quantity=quantity,
                        actor=actor.value, key=idempotency_key)
        return True

    monkeypatch.setattr(web_routes.InventoryService, "issue_to_asset", _issue)
    r = client.post("/app/equipment/eid-70021/issue",
                    data={"item_code": "es001", "quantity": "2", "reason": "swap", "nonce": "n1"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/equipment/EID-70021?issue=ok"
    assert captured["asset_id"] == "EID-70021" and captured["item_code"] == "ES001"  # 正規化大寫
    assert captured["actor"] == "human:jlee"               # actor = 登入者
    assert captured["key"] == "webissue:v2:n1:ES001:2"        # 冪等鍵綁 payload(review f14cf8d)


def test_equipment_issue_part_error(as_user, monkeypatch):
    from cmms.domain.inventory.service import InventoryError

    async def _issue(self, **kw):
        raise InventoryError("inventory_item ZZZ not found")

    monkeypatch.setattr(web_routes.InventoryService, "issue_to_asset", _issue)
    r = client.post("/app/equipment/EID-70021/issue",
                    data={"item_code": "ZZZ", "quantity": "1", "nonce": "n2"},
                    follow_redirects=False)
    assert r.headers["location"].endswith("?issue=err")       # 未知料號 → banner,不炸


def test_inventory_rfq_button_admin_only(as_user, _inv_data):
    """RFQ 一鍵 = admin-only(Jordan 2026-07-03):engineer 不見按鈕、admin 見。"""
    r = client.get("/app/inventory/ES000804")                 # engineer(預設)
    assert "/app/inventory/ES000804/rfq" not in r.text
    as_user["user"].role = "admin"
    r2 = client.get("/app/inventory/ES000804")
    assert "/app/inventory/ES000804/rfq" in r2.text           # RFQ 一鍵表單(supplier_org 已連)
    assert "12" in r2.text                                     # reorder_quantity 顯示


def test_inventory_rfq_post_engineer_blocked(as_user, _inv_data, monkeypatch):
    called = {"n": 0}

    async def _rfq(self, **kw):
        called["n"] += 1
        return SimpleNamespace(status="drafted")

    monkeypatch.setattr(web_routes.ProcurementService, "create_rfq", _rfq)
    r = client.post("/app/inventory/ES000804/rfq", data={"nonce": "nx"}, follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES000804?rfq=adminonly"
    assert called["n"] == 0                                    # engineer → 不觸發寄送


def test_inventory_rfq_post_drafted_when_smtp_unset(as_user, _inv_data, monkeypatch):
    as_user["user"].role = "admin"
    captured: dict = {}

    async def _rfq(self, *, supplier_org_id, item_codes, actor, dry_run=False,
                   idempotency_key=None, sender=None):
        captured.update(org=supplier_org_id, items=item_codes, actor=actor.value,
                        dry=dry_run, key=idempotency_key)
        return SimpleNamespace(status="drafted")

    monkeypatch.setattr(web_routes.ProcurementService, "create_rfq", _rfq)
    r = client.post("/app/inventory/es000804/rfq", data={"nonce": "n3"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/inventory/ES000804?rfq=drafted"
    assert captured["org"] == "CMB" and captured["items"] == ["ES000804"]
    assert captured["dry"] is True                            # SMTP 未配置 → 誠實降級 dry_run
    assert captured["key"] == "webrfq:v2:n3:ES000804"         # 冪等鍵綁 payload(review f14cf8d)


def test_inventory_rfq_nosupplier(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _get(self, code):
        return SimpleNamespace(item_code=code, supplier_org_id=None)

    monkeypatch.setattr(web_routes.InventoryService, "get_item", _get)
    r = client.post("/app/inventory/ES999/rfq", data={"nonce": "n4"}, follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES999?rfq=nosupplier"  # 未連 org → 提示


def test_settings_pat_form_and_store(as_user, monkeypatch):
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)
    r = client.get("/app/settings")
    assert 'name="pat_secret"' in r.text and "/app/settings/pat" in r.text  # 表單(無現行憑證)

    captured: dict = {}

    async def _store(self, *, user_id, system, secret, actor, label=None):
        captured.update(uid=user_id, system=system, secret=secret, label=label)
        return 1

    monkeypatch.setattr(web_routes.CredentialVault, "store_credential", _store)
    # #8:標籤欄移除,一人一支;route 固定 label=None(system 固定 'jira')
    r2 = client.post("/app/settings/pat", data={"pat_secret": "PAT-XYZ"},
                     follow_redirects=False)
    assert r2.headers["location"] == "/app/settings?pat=saved#pat"  # #pat 錨點捲到 PAT 區
    assert captured == {"uid": "jlee", "system": "jira", "secret": "PAT-XYZ", "label": None}


def test_settings_pat_single_token_ui(as_user, monkeypatch):
    """#8:已有一支 PAT → 顯示 metadata + 更換 + 刪除(無標籤欄);無新增第二支的表單。"""

    async def _one_cred(self, uid):
        return [SimpleNamespace(system="jira", label=None,
                                created_at=datetime(2026, 7, 1), last_used_at=None)]

    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _one_cred)
    r = client.get("/app/settings")
    assert r.status_code == 200
    assert "Replace token" in r.text and "Delete token" in r.text     # 更換 + 刪除
    assert "created" in r.text and "2026-07-01" in r.text             # 建立時間 metadata
    assert 'name="label"' not in r.text                              # 標籤欄已移除


def test_settings_pat_keyunset_and_revoke(as_user, monkeypatch):
    from cmms.domain.identity.vault import VaultKeyUnset

    async def _store(self, **kw):
        raise VaultKeyUnset("no key")

    monkeypatch.setattr(web_routes.CredentialVault, "store_credential", _store)
    r = client.post("/app/settings/pat", data={"pat_secret": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/app/settings?pat=keyunset#pat"  # fail-closed,不明文暫存

    calls: list = []

    async def _rev(self, *, user_id, system, actor):
        calls.append((user_id, system, actor.value))
        return True

    monkeypatch.setattr(web_routes.CredentialVault, "revoke", _rev)
    r2 = client.post("/app/settings/pat/revoke", follow_redirects=False)
    assert r2.headers["location"] == "/app/settings?pat=revoked#pat"
    assert calls == [("jlee", "jira", "human:jlee")]    # 限本人


def test_settings_pat_keyinvalid_and_empty(as_user, monkeypatch):
    """主鑰格式無效(誤用 token_urlsafe)→ keyinvalid banner;空輸入 → empty(不再誤導成 keyunset)。"""
    from cmms.domain.identity.vault import VaultKeyInvalid

    async def _store(self, **kw):
        raise VaultKeyInvalid("not a valid Fernet key")

    monkeypatch.setattr(web_routes.CredentialVault, "store_credential", _store)
    r = client.post("/app/settings/pat", data={"pat_secret": "x"}, follow_redirects=False)
    assert r.headers["location"] == "/app/settings?pat=keyinvalid#pat"

    # 空輸入 → empty(不觸 vault,不再共用 keyunset)
    r2 = client.post("/app/settings/pat", data={"pat_secret": "   "}, follow_redirects=False)
    assert r2.headers["location"] == "/app/settings?pat=empty#pat"

    # GET 渲染 keyinvalid banner(在地化文案)+ PAT 區錨點(redirect #pat 的落點)
    monkeypatch.setattr(web_routes.CredentialVault, "list_credentials", _no_creds)
    r3 = client.get("/app/settings?pat=keyinvalid")
    assert r3.status_code == 200
    assert "Fernet" in r3.text  # keyinvalid 文案含 Fernet 提示
    assert 'id="pat"' in r3.text  # 錨點存在,#pat fragment 才捲得到


def test_admin_proposals_list_and_confirm(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _list(self, **kw):
        return [SimpleNamespace(
            pending_token="tok1", operation="open_work_order",
            params={"asset_id": "EID-1", "work_type": "REACTIVE"},
            dry_run_diff={"action": "create_work_order"}, proposed_by="agent:analytics",
            expires_at=datetime(2026, 7, 3, 12, 0), created_at=datetime(2026, 7, 2, 12, 0),
        )]

    async def _sweep(self, **kw):
        return 0

    monkeypatch.setattr(web_routes.WorkOrderService, "list_proposals", _list)
    monkeypatch.setattr(web_routes.WorkOrderService, "expire_stale_proposals", _sweep)
    r = client.get("/admin/proposals")
    assert r.status_code == 200
    assert "Open work order" in r.text and "agent:analytics" in r.text   # 人讀操作標籤
    assert "EID-1" in r.text                                            # 原始參數(details)
    assert "/admin/proposals/tok1/confirm" in r.text

    calls: list = []

    async def _confirm(self, *, pending_token, confirmer, at=None):
        calls.append((pending_token, confirmer.value))
        return SimpleNamespace(work_order_no=1)

    monkeypatch.setattr(web_routes.WorkOrderService, "confirm", _confirm)
    r2 = client.post("/admin/proposals/tok1/confirm", follow_redirects=False)
    assert r2.status_code == 303
    assert calls == [("tok1", "human:jlee")]               # confirmer = 登入 admin(拒匿名)


def test_admin_proposals_forbids_engineer(as_user):
    r = client.get("/admin/proposals")                        # 預設 engineer
    assert r.status_code == 403


def test_admin_proposals_shows_open_feedback(as_user, monkeypatch):
    """內部規格:/admin/proposals 同頁顯示開放中使用者回饋 + 標記已處理按鈕。"""
    as_user["user"].role = "admin"

    async def _list(self, **kw):
        return []

    async def _sweep(self, **kw):
        return 0

    async def _open(self, *a, **kw):
        return [SimpleNamespace(
            id=7, user_id="operator.1k", message="想要一份掃碼 SOP",
            created_at=datetime(2026, 7, 13, 9, 30),
        )]

    async def _resolved(self, *a, **kw):
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_proposals", _list)
    monkeypatch.setattr(web_routes.WorkOrderService, "expire_stale_proposals", _sweep)
    monkeypatch.setattr(web_routes.FeedbackService, "list_open", _open)
    monkeypatch.setattr(web_routes.FeedbackService, "list_recent_resolved", _resolved)
    r = client.get("/admin/proposals")
    assert r.status_code == 200
    assert "想要一份掃碼 SOP" in r.text
    assert "operator.1k" in r.text
    assert "/admin/feedback/7/resolve" in r.text


def test_admin_resolve_feedback_redirects(as_user, monkeypatch):
    """內部規格:標記已處理端點 → mark_resolved + 303 回提案頁。"""
    as_user["user"].role = "admin"
    calls: list = []

    async def _resolve(self, feedback_id, actor):
        calls.append((feedback_id, actor.value))
        return SimpleNamespace(id=feedback_id)

    monkeypatch.setattr(web_routes.FeedbackService, "mark_resolved", _resolve)
    r = client.post("/admin/feedback/7/resolve", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/proposals"
    assert calls == [(7, "human:jlee")]                    # admin-only,actor = 登入者


def test_admin_resolve_feedback_forbids_engineer(as_user):
    r = client.post("/admin/feedback/7/resolve", follow_redirects=False)  # 預設 engineer
    assert r.status_code == 403


# ---- 2026-07-03 批:自動完成 / 即時過濾 / 工單操作 / 備品編輯 / 批次 RFQ / admin PM CRUD ----


def test_suggest_endpoint_asset(as_user, monkeypatch):
    """輸入 "0038" → 建議 EID(自動完成資料源;fragment 由 app.js 渲染)。"""
    async def _list(self, **kwargs):
        assert kwargs["search"] == "0038"
        return [SimpleNamespace(asset_id="EID-70004", description="ASMB 母機")]

    monkeypatch.setattr(web_routes.AssetService, "list_assets", _list)
    r = client.get("/app/suggest?kind=asset&q=0038")
    assert r.status_code == 200
    assert 'data-value="EID-70004"' in r.text and "ASMB 母機" in r.text


def test_suggest_endpoint_person_and_empty(as_user, monkeypatch):
    async def _names(self, q, **kw):
        return ["Alice Fang"]

    monkeypatch.setattr(web_routes.WorkOrderService, "list_assignee_suggestions", _names)
    r = client.get("/app/suggest?kind=person&q=smi")
    assert 'data-value="Alice Fang"' in r.text
    assert client.get("/app/suggest?kind=asset&q=").text == ""     # 空 q → 空


def test_suggest_endpoint_anon_empty(anon):
    r = client.get("/app/suggest?kind=asset&q=EID")
    assert r.status_code == 200 and r.text == ""                    # 未登入 → 空(不洩資料)


def test_search_inputs_have_live_filter_and_htmx_partial(as_user, monkeypatch):
    """搜尋欄輸入即過濾(hx-trigger);HX-Request → 只回結果 fragment(無整頁外殼)。"""
    async def _list(self, **kwargs):
        return []

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders")
    # 21.5k 列 ilike 掃描:單字元不觸發(0 字=清除照常;review f14cf8d 效能)
    assert "input[target.value.length==0||target.value.length" in r.text
    assert "changed delay:250ms, search" in r.text
    assert 'id="wo-results"' in r.text
    r2 = client.get("/app/work-orders", headers={"HX-Request": "true"})
    assert r2.status_code == 200
    assert "<html" not in r2.text and "sidebar" not in r2.text      # fragment,非整頁


def test_report_form_has_owner_and_suggest(as_user):
    r = client.get("/app/report")
    assert 'name="assignees"' in r.text                             # 0031 多負責人欄
    assert "data-multi" in r.text                                   # 多值輸入容器(＋/×)
    assert 'data-suggest="asset"' in r.text                         # EID 自動完成
    assert 'data-suggest="person"' in r.text                        # 負責人自動完成
    assert "/static/app.js" in r.text


def test_report_submit_passes_assignee(_write_env):
    r = client.post(
        "/app/report",
        data={"asset_id": "EID-70021", "brief_description": "x", "assignees": ["Ben Yeh"]},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # 指派 pass-through 有實質斷言(0031:assignees 首位落 element[5])
    assert _write_env["opened"] == [
        ("EID-70021", "REACTIVE", "x", "jlee", "human:jlee", "Ben Yeh")
    ]


def test_wo_assign_route(as_user, monkeypatch):
    captured: dict = {}

    async def _assign(self, no, *, assignees, actor):
        captured.update(no=no, people=assignees, actor=actor.value)
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "set_assignees", _assign)
    r = client.post("/app/work-orders/30318/assign",
                    data={"assignees": ["Cara Lo", "Ben Yeh"]}, follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders/30318?msg=assign_ok"
    assert captured == {"no": 30318, "people": ["Cara Lo", "Ben Yeh"],
                        "actor": "human:jlee"}


def test_wo_note_edit_route(as_user, monkeypatch):
    captured: dict = {}

    async def _upd(self, note_id, *, body, actor, work_order_no=None):
        # 歸屬/本人-or-admin/終態凍結全在 domain 強制(review f14cf8d);route 只轉譯錯誤
        if note_id != 7:
            raise WorkOrderError(f"note {note_id} does not belong to wo {work_order_no}")
        captured.update(note_id=note_id, body=body, actor=actor.value, wo=work_order_no)
        return SimpleNamespace(id=note_id, work_order_no=30318)

    monkeypatch.setattr(web_routes.WorkOrderService, "update_note", _upd)
    r = client.post("/app/work-orders/30318/notes/7/edit", data={"body": "更正內容"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert captured == {"note_id": 7, "body": "更正內容", "actor": "human:jlee",
                        "wo": 30318}
    # note 不屬本工單 → domain 擋(note_err banner)
    captured.clear()
    r2 = client.post("/app/work-orders/30318/notes/99/edit", data={"body": "x"},
                     follow_redirects=False)
    assert r2.headers["location"].endswith("msg=note_err")
    assert not captured


def test_wo_note_photo_delete_guarded(as_user, monkeypatch):
    """刪日誌照片:owner 驗證(note 屬本工單、att 屬該 note、本人/admin)後 soft delete。"""
    note = SimpleNamespace(id=7, author="human:jlee", work_order_no=30318)
    att = SimpleNamespace(id=3, owner_type="work_order_note", owner_id="7")
    deleted: list = []

    async def _get_note(self, nid):
        return note if nid == 7 else None

    async def _get_att(self, aid):
        return att if aid == 3 else None

    async def _del(self, aid, actor, **kw):
        deleted.append((aid, actor.value))
        return att

    monkeypatch.setattr(web_routes.WorkOrderService, "get_note", _get_note)
    monkeypatch.setattr(web_routes.AttachmentService, "get_attachment", _get_att)
    monkeypatch.setattr(web_routes.AttachmentService, "soft_delete_attachment", _del)
    r = client.post("/app/work-orders/30318/notes/7/photos/3/delete", follow_redirects=False)
    assert r.status_code == 303 and deleted == [(3, "human:jlee")]
    # 別人的 note(非 admin)→ 拒
    note.author = "human:someone-else"
    r2 = client.post("/app/work-orders/30318/notes/7/photos/3/delete", follow_redirects=False)
    assert r2.headers["location"].endswith("msg=note_err") and len(deleted) == 1


def test_wo_parts_issue_route(as_user, monkeypatch):
    captured: dict = {}

    # 領料 pre-check 讀 session.get(InventoryItem, code):給一個回既存料件的假 session
    class _FS:
        async def get(self, model, key):
            return object()

    async def _sess():
        yield _FS()

    app.dependency_overrides[get_session] = _sess

    async def _issue(self, *, work_order_no, item_code, quantity, actor, idempotency_key=None,
                     at=None):
        captured.update(no=work_order_no, item=item_code, qty=quantity, key=idempotency_key)
        return True

    monkeypatch.setattr(web_routes.WorkOrderService, "issue_part_to_work_order", _issue)
    r = client.post("/app/work-orders/30318/parts",
                    data={"item_code": "es001", "quantity": "2", "nonce": "n9"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders/30318?msg=part_ok#parts"
    assert captured["item"] == "ES001"
    assert captured["key"] == "webwopart:v2:n9:ES001:2"       # 冪等鍵綁 payload(review f14cf8d)
    # 終態拒領 → part_err banner
    from cmms.domain.work_order.service import WorkOrderError

    async def _reject(self, **kw):
        raise WorkOrderError("terminal")

    monkeypatch.setattr(web_routes.WorkOrderService, "issue_part_to_work_order", _reject)
    r2 = client.post("/app/work-orders/30318/parts",
                     data={"item_code": "ES001", "quantity": "1", "nonce": "na"},
                     follow_redirects=False)
    assert r2.headers["location"].endswith("msg=part_err#parts")


def test_wo_link_route(as_user, monkeypatch):
    captured: dict = {}

    async def _link(self, *, work_order_no, external_key, link_type, actor, **kw):
        captured.update(no=work_order_no, key=external_key, lt=link_type, actor=actor.value)
        return SimpleNamespace(id=1)

    monkeypatch.setattr(web_routes.WorkOrderService, "record_external_link", _link)
    r = client.post("/app/work-orders/30318/links", data={"external_key": "mrq-4220"},
                    follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders/30318?msg=link_ok"
    assert captured == {"no": 30318, "key": "MRQ-4220", "lt": "referenced",
                        "actor": "human:jlee"}


def test_wo_cancel_and_void_routes(as_user, monkeypatch):
    calls: list = []

    async def _cancel(self, no, actor, *, reason=None, **kw):
        calls.append(("cancel", no, actor.value, reason))
        return SimpleNamespace(work_order_no=no)

    async def _void(self, no, actor, *, reason=None, **kw):
        calls.append(("void", no, actor.value, reason))
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "cancel_reactive_report", _cancel)
    monkeypatch.setattr(web_routes.WorkOrderService, "void_work_order", _void)
    # 取消(engineer 可):事由與轉移同交易原子寫(reason= 進 domain,review f14cf8d)
    client.post("/app/work-orders/30318/cancel", data={"reason": "誤開"}, follow_redirects=False)
    assert ("cancel", 30318, "human:jlee", "誤開") in calls
    # 作廢:engineer 擋(void_err)、admin 執行
    r = client.post("/app/work-orders/30318/void", data={"reason": "重複"}, follow_redirects=False)
    assert r.headers["location"].endswith("msg=void_err")
    assert not any(c[0] == "void" for c in calls)
    as_user["user"].role = "admin"
    client.post("/app/work-orders/30318/void", data={"reason": "重複"}, follow_redirects=False)
    assert ("void", 30318, "human:jlee", "重複") in calls


def test_wo_request_void_creates_proposal(as_user, monkeypatch):
    captured: dict = {}

    async def _get(self, no):
        return SimpleNamespace(work_order_no=no)

    async def _propose(self, *, operation, params, proposed_by, idempotency_key=None,
                       ttl_seconds=None, at=None):
        captured.update(op=operation, params=params, by=proposed_by.value, ttl=ttl_seconds)
        return SimpleNamespace(pending_token="tokX")

    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _get)
    monkeypatch.setattr(web_routes.WorkOrderService, "propose", _propose)
    r = client.post("/app/work-orders/30318/request-void",
                    data={"reason": "設備已除役", "nonce": "n5"}, follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders/30318?msg=voidreq_ok"
    assert captured["op"] == "void_work_order" and captured["by"] == "human:jlee"
    assert captured["params"] == {"work_order_no": 30318, "reason": "設備已除役"}
    # 空事由 → 擋
    r2 = client.post("/app/work-orders/30318/request-void",
                     data={"reason": "  ", "nonce": "n6"}, follow_redirects=False)
    assert r2.headers["location"].endswith("msg=voidreq_err")


def test_transition_complete_passes_fields(as_user, monkeypatch):
    captured: dict = {}

    async def _complete(self, no, actor, *, at=None, action_taken=None, labor_hours=None):
        captured.update(no=no, action=action_taken, hours=labor_hours)
        return SimpleNamespace(work_order_no=no)

    monkeypatch.setattr(web_routes.WorkOrderService, "complete_work", _complete)
    # 工時欄已移除:web 不再送 labor_hours → complete_work 收到 None(選填參數保留)
    client.post("/app/work-orders/30318/transition",
                data={"action": "complete", "action_taken": "更換泵浦", "labor_hours": "1.5"},
                follow_redirects=False)
    assert captured == {"no": 30318, "action": "更換泵浦", "hours": None}


def test_wo_detail_shows_parts_links_and_actions(as_user, _detail_data, monkeypatch):
    """詳情頁渲染:領料區(含改量/取消入口)+ MRQ 連結區(help text)+ 指派表單;
    #3c:工程師面「請求作廢」按鈕已移除(void 提案 domain/admin 面保留)。"""
    async def _parts(self, no):
        return [SimpleNamespace(id=11, item_code="ES001", quantity=2,
                                created_at=datetime(2026, 7, 1))]

    async def _links(self, no):
        return [SimpleNamespace(id=1, external_key="MRQ-4220", link_type="referenced", title=None)]

    monkeypatch.setattr(web_routes.WorkOrderService, "get_parts", _parts)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_external_links", _links)
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/work-orders/30318")
    assert "ES001" in r.text and "/app/work-orders/30318/parts" in r.text     # 領料區(Jordan #1)
    assert "/app/work-orders/30318/parts/11/update" in r.text                 # 改數量(#9)
    assert "/app/work-orders/30318/parts/11/cancel" in r.text                 # 取消領料(#9)
    assert "MRQ-4220" in r.text and "/app/work-orders/30318/links" in r.text  # MRQ 連結區
    assert "逐則同步為 comment" in r.text  # #2 help text(zh-TW,轉發已上線)
    assert "僅連結(不同步)" in r.text  # referenced → 不同步徽章
    assert "/app/work-orders/30318/assign" in r.text                          # 就地改派
    assert "/app/work-orders/30318/request-void" not in r.text                # #3c:按鈕移除
    assert 'data-suggest="part"' in r.text and 'data-suggest="person"' in r.text


def test_wo_detail_state_chips_and_finish(as_user, _detail_data):
    """#3a/b:狀態 chip 一排(處理中 + 各等待原因,當前態高亮、不計停機小字)+ 單一結單鍵。"""
    as_user["user"].ui_locale = "zh-TW"
    r = client.get("/app/work-orders/30318")                  # IN_PROGRESS
    assert 'name="action" value="progress"' in r.text          # 處理中 chip
    assert 'name="hold_reason" value="WAITING_PARTS"' in r.text
    assert 'name="hold_reason" value="TEST_RUN"' in r.text
    assert "不計停機" in r.text                                 # 試跑/等機台空檔小字
    assert 'name="action" value="finish"' in r.text            # 單一結單鍵
    assert "結單" in r.text
    assert 'name="action" value="start"' not in r.text         # 「開始」按鈕已移除
    assert 'name="labor_hours"' not in r.text                   # 工時欄已移除(Jordan 裁決)


def test_wo_detail_note_delete_button(as_user, _detail_data):
    """#1:本人 note 有刪除入口;別人的(agent)沒有(非 admin);admin 全部有。"""
    r = client.get("/app/work-orders/30318")
    assert "/app/work-orders/30318/notes/1/delete" in r.text     # 本人
    assert "/app/work-orders/30318/notes/2/delete" not in r.text  # agent:hermes
    as_user["user"].role = "admin"
    r2 = client.get("/app/work-orders/30318")
    assert "/app/work-orders/30318/notes/2/delete" in r2.text


def test_wo_note_delete_route(as_user, monkeypatch):
    calls: list = []

    async def _del(self, note_id, actor, *, work_order_no=None):
        calls.append((note_id, actor.value, work_order_no))
        return work_order_no

    monkeypatch.setattr(web_routes.WorkOrderService, "delete_note", _del)
    r = client.post("/app/work-orders/30318/notes/7/delete", follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders/30318?msg=note_deleted"
    assert calls == [(7, "human:jlee", 30318)]


def test_wo_part_amend_cancel_routes(as_user, monkeypatch):
    calls: dict = {}

    async def _upd(self, *, work_order_no, part_id, new_quantity, actor, idempotency_key=None):
        calls["upd"] = (work_order_no, part_id, new_quantity, actor.value, idempotency_key)
        return True

    async def _cancel(self, *, work_order_no, part_id, actor, idempotency_key=None):
        calls["cancel"] = (work_order_no, part_id, actor.value)
        return True

    monkeypatch.setattr(web_routes.WorkOrderService, "update_part_issue_quantity", _upd)
    monkeypatch.setattr(web_routes.WorkOrderService, "cancel_part_issue", _cancel)
    r = client.post("/app/work-orders/30318/parts/11/update",
                    data={"quantity": "4", "nonce": "n7"}, follow_redirects=False)
    assert r.headers["location"] == "/app/work-orders/30318?msg=part_upd#parts"
    assert calls["upd"] == (30318, 11, "4", "human:jlee", "webpartupd:v1:n7:11:4")
    r2 = client.post("/app/work-orders/30318/parts/11/cancel",
                     data={"nonce": "n7"}, follow_redirects=False)
    assert r2.headers["location"] == "/app/work-orders/30318?msg=part_cancelled#parts"
    assert calls["cancel"] == (30318, 11, "human:jlee")


def test_wo_detail_note_edit_own_only(as_user, _detail_data):
    """時間線:本人 note 有更正入口;別人的(agent)沒有(非 admin)。"""
    r = client.get("/app/work-orders/30318")
    assert "/app/work-orders/30318/notes/1/edit" in r.text     # note 1 = human:jlee(本人)
    assert "/app/work-orders/30318/notes/2/edit" not in r.text  # note 2 = agent:hermes
    # admin 可代改全部
    as_user["user"].role = "admin"
    r2 = client.get("/app/work-orders/30318")
    assert "/app/work-orders/30318/notes/2/edit" in r2.text


def test_inventory_low_filter_passes_flag(as_user, monkeypatch):
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return []

    async def _first(self, owner_type, owner_ids):
        return {}

    monkeypatch.setattr(web_routes.InventoryService, "list_items", _list)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    r = client.get("/app/inventory?low=1")
    assert captured["below_reorder"] is True                   # 低庫存過濾(Jordan #5)
    assert r.status_code == 200
    client.get("/app/inventory")
    assert captured["below_reorder"] is False


def test_inventory_update_and_adjust_admin_only(as_user, monkeypatch):
    upd: dict = {}
    adj: dict = {}

    async def _upd(self, code, **kw):
        upd.update(code=code, **{k: v for k, v in kw.items() if k != "actor"})
        return SimpleNamespace(item_code=code)

    async def _adj(self, code, *, new_quantity, reason, actor, idempotency_key=None):
        adj.update(code=code, qty=new_quantity, reason=reason)
        return True

    monkeypatch.setattr(web_routes.InventoryService, "update_item", _upd)
    monkeypatch.setattr(web_routes.InventoryService, "adjust_on_hand", _adj)
    # engineer 擋
    r = client.post("/app/inventory/ES001/update", data={}, follow_redirects=False)
    assert r.headers["location"].endswith("rfq=adminonly") and not upd
    # admin:編輯(數字欄位解析)+ 盤點調整
    as_user["user"].role = "admin"
    r2 = client.post("/app/inventory/es001/update", data={
        "name": "新名", "reorder_point": "5", "reorder_quantity": "12",
        "lead_time_weeks": "4", "unit_cost": "9.99", "is_stocked": "true",
    }, follow_redirects=False)
    assert r2.headers["location"] == "/app/inventory/ES001?rfq=saved"
    assert upd["code"] == "ES001" and upd["name"] == "新名"
    assert upd["reorder_quantity"] == Decimal("12") and upd["lead_time_weeks"] == 4
    assert upd["is_stocked"] is True and upd["is_obsolete"] is False
    r3 = client.post("/app/inventory/ES001/adjust",
                     data={"new_quantity": "7", "reason": "盤點", "nonce": "n7"},
                     follow_redirects=False)
    assert r3.headers["location"] == "/app/inventory/ES001?rfq=saved"
    assert adj == {"code": "ES001", "qty": "7", "reason": "盤點"}


def test_inventory_update_normalizes_supplier(as_user, monkeypatch):
    """#4:手打全小寫已知 org 名 → 儲存前正規化為 canonical 名 + 帶入 org_id。"""
    upd: dict = {}

    async def _upd(self, code, **kw):
        upd.update(code=code, **{k: v for k, v in kw.items() if k != "actor"})
        return SimpleNamespace(item_code=code)

    async def _orgs(self, *, search=None, limit=100, **kw):
        # 模擬 ilike substring 命中(輸入 "cmb corp" 命中 canonical "CMB Corp")
        return [SimpleNamespace(name="CMB Corp", org_id="ORG-9")]

    monkeypatch.setattr(web_routes.InventoryService, "update_item", _upd)
    monkeypatch.setattr(web_routes.ContactsService, "list_organizations", _orgs)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/ES001/update", data={
        "supplier": "cmb corp",  # 全小寫、org_id 留空
    }, follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES001?rfq=saved"
    assert upd["supplier"] == "CMB Corp"           # 正規化為 canonical 大小寫
    assert upd["supplier_org_id"] == "ORG-9"        # org_id 連動帶入


def test_inventory_update_freetext_supplier_preserved(as_user, monkeypatch):
    """#4:找不到匹配 org 的自由文字供應商 → 維持原輸入(不硬擋)。"""
    upd: dict = {}

    async def _upd(self, code, **kw):
        upd.update(code=code, **{k: v for k, v in kw.items() if k != "actor"})
        return SimpleNamespace(item_code=code)

    async def _orgs(self, *, search=None, limit=100, **kw):
        return []  # 查無匹配

    monkeypatch.setattr(web_routes.InventoryService, "update_item", _upd)
    monkeypatch.setattr(web_routes.ContactsService, "list_organizations", _orgs)
    as_user["user"].role = "admin"
    r = client.post("/app/inventory/ES001/update", data={
        "supplier": "某本地五金行",
    }, follow_redirects=False)
    assert r.headers["location"] == "/app/inventory/ES001?rfq=saved"
    assert upd["supplier"] == "某本地五金行"        # 自由文字維持原樣
    assert upd["supplier_org_id"] is None


def test_inventory_rfq_batch_admin_flow(as_user, monkeypatch):
    from cmms.domain.procurement.service import RfqDraft

    async def _drafts(self, **kw):
        return [RfqDraft(supplier_org_id="CMB", supplier_name="CMB Corp",
                         recipient_email="po@cmb.com",
                         lines=[("ES001", Decimal("12")), ("ES002", Decimal("3"))])]

    monkeypatch.setattr(web_routes.ProcurementService, "draft_below_safety_stock", _drafts)
    # engineer 進不來
    r = client.get("/app/inventory-rfq", follow_redirects=False)
    assert r.status_code == 303
    # admin:預覽 + 送出
    as_user["user"].role = "admin"
    r2 = client.get("/app/inventory-rfq")
    assert r2.status_code == 200
    assert "CMB Corp" in r2.text and "ES001" in r2.text
    assert 'value="ES001,ES002"' in r2.text                    # 一組一鍵送出

    captured: dict = {}

    async def _rfq(self, *, supplier_org_id, item_codes, actor, dry_run=False,
                   idempotency_key=None, sender=None):
        captured.update(org=supplier_org_id, items=item_codes, dry=dry_run)
        return SimpleNamespace(status="drafted")

    monkeypatch.setattr(web_routes.ProcurementService, "create_rfq", _rfq)
    r3 = client.post("/app/inventory-rfq",
                     data={"supplier_org_id": "CMB", "item_codes": "ES001,ES002", "nonce": "n8"},
                     follow_redirects=False)
    assert r3.headers["location"] == "/app/inventory-rfq?sent=drafted"  # 真實狀態,不再一律報成功
    assert captured["org"] == "CMB" and captured["items"] == ["ES001", "ES002"]


def test_admin_pm_crud_routes(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _create_task(self, *, task_no, description, actor):
        calls.append(("task", task_no, description))
        return SimpleNamespace(task_no=task_no.upper())

    async def _add_step(self, task_no, *, task_desc, actor):
        calls.append(("step", task_no, task_desc))
        return SimpleNamespace(id=1)

    async def _add_part(self, step_id, *, item_code, replace_qty, actor, task_no=None):
        calls.append(("part", step_id, item_code, replace_qty))
        return SimpleNamespace(id=1)

    async def _create_sched(self, **kw):
        calls.append(("sched", kw["asset_id"], kw["frequency_interval"], kw["frequency_unit"],
                      kw["next_due_date"], kw["assigned_person"]))
        return SimpleNamespace(pm_id="PMW-1")

    async def _suppress(self, pm_id, suppressed, actor, *, task_id=None):
        calls.append(("suppress", pm_id, suppressed))
        return SimpleNamespace(pm_id=pm_id)

    async def _active(self, task_no, active, actor):
        calls.append(("active", task_no, active))
        return SimpleNamespace(task_no=task_no)

    from cmms.web import admin_routes as adm
    monkeypatch.setattr(adm.TaskService, "create_task", _create_task)
    monkeypatch.setattr(adm.TaskService, "add_task_step", _add_step)
    monkeypatch.setattr(adm.TaskService, "add_task_part", _add_part)
    monkeypatch.setattr(adm.TaskService, "set_task_active", _active)
    monkeypatch.setattr(adm.PmScheduleService, "create_pm_schedule", _create_sched)
    monkeypatch.setattr(adm.PmScheduleService, "set_suppressed", _suppress)

    r = client.post("/admin/pm", data={"task_no": "NEWPM1", "description": "新保養"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/pm/NEWPM1"
    client.post("/admin/pm/NEWPM1/steps", data={"task_desc": "清潔"}, follow_redirects=False)
    client.post("/admin/pm/NEWPM1/steps/1/parts",
                data={"item_code": "EC000807", "replace_qty": "2"}, follow_redirects=False)
    client.post("/admin/pm/NEWPM1/schedules", data={
        "asset_id": "eid-001", "frequency_interval": "3", "frequency_unit": "Months",
        "next_due_date": "2026-08-01", "assigned_person": "Iris Chiu",
    }, follow_redirects=False)
    client.post("/admin/pm/NEWPM1/schedules/P1/suppress", data={"suppressed": "1"},
                follow_redirects=False)
    client.post("/admin/pm/NEWPM1/active", data={"active": "0"}, follow_redirects=False)
    assert ("task", "NEWPM1", "新保養") in calls
    assert ("step", "NEWPM1", "清潔") in calls
    assert ("part", 1, "EC000807", "2") in calls
    assert ("sched", "EID-001", 3, "Months", date(2026, 8, 1), "Iris Chiu") in calls
    assert ("suppress", "P1", True) in calls
    assert ("active", "NEWPM1", False) in calls


def test_admin_pm_crud_forbids_engineer(as_user):
    r = client.post("/admin/pm", data={"task_no": "X", "description": "y"},
                    follow_redirects=False)
    assert r.status_code == 403                                # RBAC:engineer 進不了 PM 管理寫入


def test_bottomnav_five_tabs_and_fab_hint(as_user):
    """手機導覽 5 tab(供應商移出;桌面側欄仍有)+ FAB 開啟助理 dock/sheet(ADR-020 實接)。"""
    r = client.get("/app/report")
    body = r.text.split('class="bottomnav"', 1)[1]
    assert body.count("/app/suppliers") == 0                   # bottomnav 無供應商
    assert "data-assistant-toggle" in r.text                    # FAB 開啟助理 dock/sheet
    sidebar = r.text.split('class="sidebar"', 1)[1].split("</aside>")[0]
    assert "/app/suppliers" in sidebar                          # 桌面側欄仍有


def test_inventory_engineer_propose_edit_flow(as_user, _inv_data, monkeypatch):
    """裁決 #3 後續:engineer 主檔修改 = 提案(admin 審);admin 仍為直接編輯表單。"""
    # engineer:看到「提案修改」fold(非直接儲存)
    r = client.get("/app/inventory/ES000804")
    assert "/app/inventory/ES000804/propose-update" in r.text
    assert "/app/inventory/ES000804/update" not in r.text          # 無 admin 直改表單
    # admin:直接編輯表單(不變)
    as_user["user"].role = "admin"
    r2 = client.get("/app/inventory/ES000804")
    assert "/app/inventory/ES000804/update" in r2.text

    captured: dict = {}

    async def _propose(self, *, operation, params, proposed_by, idempotency_key=None,
                       ttl_seconds=None, at=None):
        captured.update(op=operation, params=params, by=proposed_by.value)
        return SimpleNamespace(pending_token="t1")

    as_user["user"].role = "engineer"
    monkeypatch.setattr(web_routes.WorkOrderService, "propose", _propose)
    r3 = client.post("/app/inventory/es000804/propose-update",
                     data={"name": "新品名", "reorder_quantity": "20", "is_stocked": "true"},
                     follow_redirects=False)
    assert r3.headers["location"] == "/app/inventory/ES000804?rfq=proposed"
    assert captured["op"] == "update_item" and captured["by"] == "human:jlee"
    assert captured["params"]["item_code"] == "ES000804"
    assert captured["params"]["name"] == "新品名"
    assert captured["params"]["is_stocked"] is True


def test_inventory_pending_proposal_hides_propose_form(as_user, _inv_data, monkeypatch):
    async def _find_pending(self, **kw):
        return SimpleNamespace(pending_token="t9")

    monkeypatch.setattr(web_routes.WorkOrderService, "find_pending_proposal", _find_pending)
    r = client.get("/app/inventory/ES000804")
    assert "propose-update" not in r.text          # 已有待審提案 → 不重收


# ---- /admin ADR-019 三讀取螢幕:稽核 feed / 附件治理 / PAT 憑證總覽 ----

@pytest.fixture
def _audit_data(monkeypatch):
    """monkeypatch 五個來源的 list 方法(統一 feed 由 route 合併排序)。"""
    async def _status(self, **kwargs):
        return [SimpleNamespace(
            work_order_no=30318, from_status="IN_PROGRESS", to_status="ON_HOLD",
            hold_reason="WAITING_PARTS", changed_at=datetime(2026, 7, 3, 15, 0),
            source_actor="human:jlee",
        )]

    async def _notes(self, **kwargs):
        return [SimpleNamespace(
            work_order_no=30318, entry_type="progress",
            updated_at=datetime(2026, 7, 3, 14, 0), updated_by="human:tony",
        )]

    async def _txns(self, **kwargs):
        return [SimpleNamespace(
            item_code="ES000804", kind="ISSUE", qty_delta=Decimal("-2"),
            work_order_no=30318, charge_target_asset_id=None,
            occurred_at=datetime(2026, 7, 3, 16, 0), source_actor="agent:hermes",
        )]

    async def _props(self, **kwargs):
        return [SimpleNamespace(
            status="CONFIRMED", operation="void_work_order",
            params={"work_order_no": 30318}, resolved_at=datetime(2026, 7, 3, 13, 0),
            confirmed_by="human:jlee", proposed_by="human:tony",
        )]

    async def _dels(self, **kwargs):
        return [SimpleNamespace(
            kind="step", task_no="TSK0007", detail="Clean the head",
            deleted_at=datetime(2026, 7, 3, 12, 0), deleted_by="human:jlee",
        )]

    monkeypatch.setattr(web_routes.WorkOrderService, "list_recent_status_changes", _status)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_recent_note_edits", _notes)
    monkeypatch.setattr(web_routes.InventoryService, "list_recent_transactions", _txns)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_resolved_proposals", _props)
    monkeypatch.setattr(web_routes.TaskService, "list_recent_deletions", _dels)


def test_admin_audit_forbids_engineer(as_user):
    r = client.get("/admin/audit")                          # 預設 engineer
    assert r.status_code == 403                              # require_admin


def test_admin_audit_feed_merges(as_user, _audit_data):
    as_user["user"].role = "admin"
    r = client.get("/admin/audit")
    assert r.status_code == 200
    # 五源都出現在合併 feed
    assert "WO-30318" in r.text                              # 狀態轉移 / 提案 / 領料連結
    assert "ES000804" in r.text                              # 庫存異動品項
    assert "TSK0007" in r.text                                 # 範本細項軟刪
    assert "Clean the head" in r.text
    assert "agent:hermes" in r.text and "human:jlee" in r.text
    # 逆時序:txn(16:00,最新)排在範本軟刪(12:00,最舊)之前
    assert r.text.index("ES000804") < r.text.index("Clean the head")


def test_admin_audit_source_filter_and_actor(as_user, monkeypatch):
    """source=stock -> 只查庫存源;actor 子字串轉進 service;limit 鉗到 200。"""
    as_user["user"].role = "admin"
    captured: dict = {}
    called = {"status": 0}

    async def _txns(self, **kwargs):
        captured.update(kwargs)
        return []

    async def _status(self, **kwargs):
        called["status"] += 1
        return []

    monkeypatch.setattr(web_routes.InventoryService, "list_recent_transactions", _txns)
    monkeypatch.setattr(web_routes.WorkOrderService, "list_recent_status_changes", _status)
    r = client.get("/admin/audit?source=stock&actor=hermes&limit=250")
    assert r.status_code == 200
    assert captured["actor_like"] == "hermes"               # actor 子字串轉進 service
    assert captured["limit"] == 200                          # 上限鉗到 200
    assert called["status"] == 0                             # 單源 -> 不查其他源


@pytest.fixture
def _att_gov_data(monkeypatch):
    async def _counts(self):
        return {"work_order_note": 12, "inventory_item": 1042}

    async def _recent(self, **kwargs):
        return [SimpleNamespace(
            id=7, owner_type="work_order_note", owner_id="7",
            original_filename="fault.jpg", r2_key="work_order_note/7/ab.jpg",
            created_by="human:jlee",
        )]

    async def _deleted(self, **kwargs):
        return [SimpleNamespace(
            id=9, owner_type="inventory_item", owner_id="ES000804",
            original_filename="old.jpg", r2_key="inventory/ES000804/x.jpg",
            created_by="human:tony", updated_by="human:jlee",
        )]

    monkeypatch.setattr(web_routes.AttachmentService, "counts_by_owner_type", _counts)
    monkeypatch.setattr(web_routes.AttachmentService, "list_recent_uploads", _recent)
    monkeypatch.setattr(web_routes.AttachmentService, "list_soft_deleted", _deleted)


def test_admin_attachments_forbids_engineer(as_user):
    assert client.get("/admin/attachments").status_code == 403


def test_admin_attachments_lists(as_user, _att_gov_data):
    as_user["user"].role = "admin"
    r = client.get("/admin/attachments")
    assert r.status_code == 200
    assert "work_order_note" in r.text and "1042" in r.text      # 統計計數
    assert "fault.jpg" in r.text                                  # 最近上傳
    assert "old.jpg" in r.text                                    # 已軟刪
    assert "/admin/attachments/9/restore" in r.text               # 還原按鈕
    assert "/admin/attachments/7/delete" in r.text                # 軟刪按鈕


def test_admin_attachment_soft_delete_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _del(self, aid, actor, **kwargs):
        calls.append((aid, actor.value))
        return SimpleNamespace(id=aid)

    monkeypatch.setattr(web_routes.AttachmentService, "soft_delete_attachment", _del)
    r = client.post("/admin/attachments/7/delete", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/attachments"
    assert calls == [(7, "human:jlee")]                        # actor = 登入 admin


def test_admin_attachment_restore_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _restore(self, aid, actor):
        calls.append((aid, actor.value))
        return SimpleNamespace(id=aid)

    monkeypatch.setattr(web_routes.AttachmentService, "restore_attachment", _restore)
    r = client.post("/admin/attachments/9/restore", follow_redirects=False)
    assert r.status_code == 303
    assert calls == [(9, "human:jlee")]


def test_admin_credentials_forbids_engineer(as_user):
    assert client.get("/admin/credentials").status_code == 403


def test_admin_credentials_lists_no_secret(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _list_all(self):
        return [SimpleNamespace(
            id=3, user_id="tony", system="jira", label="prod",
            secret_ciphertext="SHOULD-NEVER-RENDER",
            created_at=datetime(2026, 7, 1, 9, 0), last_used_at=None,
        )]

    monkeypatch.setattr(web_routes.CredentialVault, "list_all_credentials", _list_all)
    r = client.get("/admin/credentials")
    assert r.status_code == 200
    assert "tony" in r.text and "jira" in r.text and "prod" in r.text
    assert "SHOULD-NEVER-RENDER" not in r.text                    # 密文永不顯示
    assert "/admin/credentials/3/revoke" in r.text


def test_admin_credential_revoke_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _revoke(self, cid, actor):
        calls.append((cid, actor.value))
        return True

    monkeypatch.setattr(web_routes.CredentialVault, "admin_revoke", _revoke)
    r = client.post("/admin/credentials/3/revoke", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/credentials"
    assert calls == [(3, "human:jlee")]                        # actor = 登入 admin


# ---- /admin batch 2:受控詞彙維護 / 資產關係維護 ----

@pytest.fixture
def _vocab_data(monkeypatch):
    async def _holds(self):
        return [SimpleNamespace(code="WAITING_PARTS", label="Waiting parts", is_downtime=True)]

    async def _statuses(self):
        return [SimpleNamespace(code="ON_HOLD", label="On Hold", rank=3,
                                is_terminal=False, is_downtime=True)]

    async def _notes(self):
        return [SimpleNamespace(code="progress", label="Progress")]

    async def _freq(self):
        return [SimpleNamespace(code="Weeks", label="Weeks")]

    async def _orgs(self):
        return [SimpleNamespace(code="Supplier", label="Supplier")]

    async def _kinds(self):
        return [SimpleNamespace(code="ISSUE", label="Issue")]

    async def _bins(self, include_inactive=False):
        return [SimpleNamespace(code="02A", is_active=True),
                SimpleNamespace(code="OLD1", is_active=False)]

    async def _mfc(self, station=None):
        return [SimpleNamespace(station="sta1", label="CycleStop",
                                signal_id="mes.failmode.cyclestop", entry_kind="fail_flag",
                                semantic_zh="循環停止")]

    async def _efc(self, station_hint=None):
        return [SimpleNamespace(code="efcSTA7_AirPressure", descr="Low Air Pressure",
                                station_hint="sta7")]

    monkeypatch.setattr(_adm.WorkOrderService, "list_hold_reasons", _holds)
    monkeypatch.setattr(_adm.WorkOrderService, "list_statuses", _statuses)
    monkeypatch.setattr(_adm.WorkOrderService, "list_note_types", _notes)
    monkeypatch.setattr(_adm.PmScheduleService, "list_freq_units", _freq)
    monkeypatch.setattr(_adm.ContactsService, "list_org_types", _orgs)
    monkeypatch.setattr(_adm.InventoryService, "list_stock_txn_kinds", _kinds)
    monkeypatch.setattr(_adm.InventoryService, "list_storage_bins", _bins)
    monkeypatch.setattr(_adm.FailureVocabService, "list_mes_failmodes", _mfc)
    monkeypatch.setattr(_adm.FailureVocabService, "list_equipment_failure_codes", _efc)


def test_admin_vocab_forbids_engineer(as_user):
    assert client.get("/admin/vocab").status_code == 403      # 預設 engineer


def test_admin_vocab_lists_all_tables(as_user, _vocab_data):
    as_user["user"].role = "admin"
    r = client.get("/admin/vocab")
    assert r.status_code == 200
    assert "WAITING_PARTS" in r.text                          # 可編輯 hold reason
    assert "/admin/vocab/hold-reasons/WAITING_PARTS/update" in r.text
    assert "ON_HOLD" in r.text and "wo_status" in r.text      # 唯讀狀態表
    assert "freq_unit" in r.text and "Weeks" in r.text        # 唯讀週期單位
    assert "org_type" in r.text and "stock_txn_kind" in r.text
    # C2 失效詞彙兩軸唯讀顯示
    assert "mes_failmode" in r.text and "equipment_failure_code" in r.text
    assert "CycleStop" in r.text and "efcSTA7_AirPressure" in r.text
    # storage_bin 儲位受控詞彙(含 inactive + 啟停 toggle)
    assert "Storage bins" in r.text and "02A" in r.text and "OLD1" in r.text
    assert "/admin/vocab/storage-bins/02A/toggle" in r.text


def test_admin_vocab_add_hold_reason_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _add(self, code, label, *, is_downtime, actor):
        calls.append((code, label, is_downtime, actor.value))
        return SimpleNamespace(code=code)

    monkeypatch.setattr(_adm.WorkOrderService, "add_hold_reason", _add)
    r = client.post("/admin/vocab/hold-reasons", follow_redirects=False, data={
        "code": "WAITING_TOOLING", "label": "Waiting tooling", "is_downtime": "1",
    })
    assert r.status_code == 303 and r.headers["location"] == "/admin/vocab"
    assert calls == [("WAITING_TOOLING", "Waiting tooling", True, "human:jlee")]


def test_admin_vocab_update_hold_reason_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _upd(self, code, *, label, is_downtime, actor):
        calls.append((code, label, is_downtime, actor.value))
        return SimpleNamespace(code=code)

    monkeypatch.setattr(_adm.WorkOrderService, "update_hold_reason", _upd)
    # is_downtime 未勾 → 缺欄位 → False
    r = client.post("/admin/vocab/hold-reasons/WAITING_PARTS/update", follow_redirects=False,
                    data={"label": "Awaiting parts"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/vocab"
    assert calls == [("WAITING_PARTS", "Awaiting parts", False, "human:jlee")]


def test_admin_vocab_add_error_redirects_with_msg(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _add(self, code, label, *, is_downtime, actor):
        raise WorkOrderError("hold reason WAITING_PARTS already exists")

    monkeypatch.setattr(_adm.WorkOrderService, "add_hold_reason", _add)
    r = client.post("/admin/vocab/hold-reasons", follow_redirects=False, data={
        "code": "WAITING_PARTS", "label": "dup", "is_downtime": "1",
    })
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_admin_vocab_add_bin_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _add(self, code, *, actor):
        calls.append((code, actor.value))
        return SimpleNamespace(code=code.strip())

    monkeypatch.setattr(_adm.InventoryService, "add_storage_bin", _add)
    r = client.post("/admin/vocab/storage-bins", follow_redirects=False, data={"code": "42A"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/vocab"
    assert calls == [("42A", "human:jlee")]


def test_admin_vocab_toggle_bin_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _toggle(self, code, *, is_active, actor):
        calls.append((code, is_active, actor.value))
        return SimpleNamespace(code=code, is_active=is_active)

    monkeypatch.setattr(_adm.InventoryService, "set_storage_bin_active", _toggle)
    # is_active='0' → 停用
    r = client.post("/admin/vocab/storage-bins/02A/toggle", follow_redirects=False,
                    data={"is_active": "0"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/vocab"
    # is_active='1' → 啟用
    client.post("/admin/vocab/storage-bins/OLD1/toggle", follow_redirects=False,
                data={"is_active": "1"})
    assert calls == [("02A", False, "human:jlee"), ("OLD1", True, "human:jlee")]


def test_inventory_add_bin_json_endpoint(as_user, monkeypatch):
    """combobox quick-add:未登入 401 / engineer 403 / admin 成功回 JSON / 錯誤 400。"""
    from cmms.domain.inventory.service import InventoryError

    async def _add(self, code, *, actor):
        if code == "bad!":
            raise InventoryError("storage bin code must be alphanumeric")
        return SimpleNamespace(code=code.strip())

    monkeypatch.setattr(web_routes.InventoryService, "add_storage_bin", _add)
    # engineer → 403
    r_eng = client.post("/app/inventory/bins", data={"code": "42A"})
    assert r_eng.status_code == 403 and r_eng.json()["ok"] is False
    # admin 成功 → JSON {ok, code}
    as_user["user"].role = "admin"
    r_ok = client.post("/app/inventory/bins", data={"code": "42A"})
    assert r_ok.status_code == 200 and r_ok.json() == {"ok": True, "code": "42A"}
    # admin 壞值 → 400 {ok:false, error}
    r_err = client.post("/app/inventory/bins", data={"code": "bad!"})
    assert r_err.status_code == 400 and r_err.json()["ok"] is False


def test_inventory_add_bin_requires_login(anon):
    r = client.post("/app/inventory/bins", data={"code": "42A"})
    assert r.status_code == 401 and r.json()["ok"] is False


def test_inventory_new_form_renders_bin_combobox(as_user, monkeypatch):
    """建新表單的儲位欄 = combobox(data-efc-combo + name=bin_location);admin 有 quick-add url。"""
    async def _bins(self, include_inactive=False):
        return [SimpleNamespace(code="02A", is_active=True)]

    monkeypatch.setattr(web_routes.InventoryService, "list_storage_bins", _bins)
    as_user["user"].role = "admin"
    r = client.get("/app/inventory/new")
    assert r.status_code == 200
    assert "data-efc-combo" in r.text
    assert 'name="bin_location"' in r.text
    assert 'data-add-url="/app/inventory/bins"' in r.text   # admin quick-add 啟用
    assert "02A" in r.text                                  # active bin 進 options/datalist


@pytest.fixture
def _rel_data(monkeypatch):
    async def _all(self, **kwargs):
        return [SimpleNamespace(relationship_type="contains_module", id=1),
                SimpleNamespace(relationship_type="contains_module", id=2),
                SimpleNamespace(relationship_type="shared_dependency", id=3)]

    async def _get(self, aid):
        return SimpleNamespace(asset_id=aid, description="Curer 9")

    async def _rels(self, asset_id, *, direction="both", **kwargs):
        if direction == "from":
            return [SimpleNamespace(id=10, relationship_type="contains_module",
                                    from_asset_id=asset_id, to_asset_id="EID-70006"),
                    SimpleNamespace(id=11, relationship_type="shared_dependency",
                                    from_asset_id=asset_id, to_asset_id="EID-70014")]
        return [SimpleNamespace(id=12, relationship_type="contains_module",
                                from_asset_id="EID-70001", to_asset_id=asset_id)]

    monkeypatch.setattr(_adm.AssetService, "list_relationships_all", _all)
    monkeypatch.setattr(_adm.AssetService, "get_asset", _get)
    monkeypatch.setattr(_adm.AssetService, "list_relationships", _rels)


def test_admin_relationships_forbids_engineer(as_user):
    assert client.get("/admin/relationships").status_code == 403


def test_admin_relationships_counts_and_lookup(as_user, _rel_data):
    as_user["user"].role = "admin"
    # 載入即統計(無 eid)
    r = client.get("/admin/relationships")
    assert r.status_code == 200
    assert ">2<" in r.text and ">1<" in r.text                # 2 contains + 1 shared
    # 查一台 EID → 顯示父/子/共用 + unlink
    r2 = client.get("/admin/relationships?eid=eid-70005")
    assert r2.status_code == 200
    assert "EID-70005" in r2.text                             # normalized upper 回顯
    assert "EID-70006" in r2.text and "EID-70014" in r2.text  # 子模組 + 共用
    assert "EID-70001" in r2.text                             # 單親容器
    assert "/admin/relationships/10/unlink" in r2.text        # 子模組 unlink
    assert "/admin/relationships/12/unlink" in r2.text        # 父容器 unlink


def test_admin_relationships_link_containment_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    class _WriteCtx:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False

    def _write(self, actor): return _WriteCtx()

    async def _link(self, machine_id, module_id, actor, **kwargs):
        calls.append((machine_id, module_id, actor.value))
        return SimpleNamespace(id=1)

    monkeypatch.setattr(_adm.AssetService, "write", _write)
    monkeypatch.setattr(_adm.AssetService, "link_containment", _link)
    r = client.post("/admin/relationships/contain", follow_redirects=False,
                    data={"parent_eid": "eid-70005", "child_eid": "eid-70006"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/relationships?eid=EID-70005"
    assert calls == [("EID-70005", "EID-70006", "human:jlee")]  # actor + 大寫正規化


def test_admin_relationships_shared_error_redirects(as_user, monkeypatch):
    as_user["user"].role = "admin"

    class _WriteCtx:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False

    def _write(self, actor): return _WriteCtx()

    async def _link(self, resource_id, machine_id, actor, **kwargs):
        raise AssetError("shared_dependency self-loop not allowed")

    monkeypatch.setattr(_adm.AssetService, "write", _write)
    monkeypatch.setattr(_adm.AssetService, "link_shared_dependency", _link)
    r = client.post("/admin/relationships/shared", follow_redirects=False,
                    data={"resource_eid": "EID-1", "machine_eid": "EID-1"})
    assert r.status_code == 303 and "err=" in r.headers["location"]


def test_admin_relationships_unlink_calls_service(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    class _WriteCtx:
        async def __aenter__(self): return None
        async def __aexit__(self, *a): return False

    def _write(self, actor): return _WriteCtx()

    async def _unlink(self, rel_id, actor, **kwargs):
        calls.append((rel_id, actor.value))

    monkeypatch.setattr(_adm.AssetService, "write", _write)
    monkeypatch.setattr(_adm.AssetService, "unlink_relationship", _unlink)
    r = client.post("/admin/relationships/10/unlink", follow_redirects=False,
                    data={"eid": "EID-70005"})
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/relationships?eid=EID-70005"
    assert calls == [(10, "human:jlee")]


# ---- 清單分頁(取代固定筆數上限)----

def _wo_rows(n: int) -> list:
    return [
        SimpleNamespace(
            work_order_no=1000 + i, asset_id="EID-1", work_type="REACTIVE", status="OPEN",
            brief_description="x", downtime_minutes=None, opened_date=date(2026, 7, 1),
            assigned_person=None, assigned_vendor=None,
        )
        for i in range(n)
    ]


def test_wo_pager_offset_and_has_next(as_user, monkeypatch):
    """page=2 → offset 轉發給 service;多撈 1 筆(26)→ has_next → 下一頁連結;只渲染 25 筆。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return _wo_rows(26)                       # per_page(25)+1 → 有下一頁

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders?scope=all&page=2")
    assert r.status_code == 200
    assert captured["limit"] == 26                # per_page+1 偵測下一頁(不 COUNT(*))
    assert captured["offset"] == 25               # (2-1)*25
    assert "page=3" in r.text                     # has_next → next
    assert "page=1" in r.text                     # page>1 → prev
    assert r.text.count("wo-card--") == 25        # 探測用第 26 筆不渲染


def test_wo_pager_hidden_when_single_page(as_user, monkeypatch):
    """第一頁且無下一頁 → 不顯示分頁列。"""
    async def _list(self, **kwargs):
        return _wo_rows(3)

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders?scope=all")
    assert r.status_code == 200
    assert 'class="pager"' not in r.text
    assert "page=2" not in r.text


def test_wo_pager_preserves_filter(as_user, monkeypatch):
    """分頁連結保留當前查詢參數(tab/q/scope)→ 過濾與分頁可疊。"""
    async def _list(self, **kwargs):
        return _wo_rows(26)

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders?scope=all&tab=waiting&q=CMB&page=1")
    assert r.status_code == 200
    assert "tab=waiting" in r.text and "q=CMB" in r.text and "scope=all" in r.text
    assert "page=2" in r.text                     # 下一頁沿用同一組過濾


def test_inventory_pager_offset(as_user, monkeypatch):
    """備品分頁:page → offset(per_page 50);filter(q)保留於分頁連結。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return [
            SimpleNamespace(item_code=f"ES{i:05d}", name="n", description="d",
                            quantity_on_hand=1, reorder_point=None, bin_location="A")
            for i in range(51)                    # 50+1 → 有下一頁
        ]

    async def _first(self, owner_type, owner_ids):
        return {}

    monkeypatch.setattr(web_routes.InventoryService, "list_items", _list)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    r = client.get("/app/inventory?q=ES&page=2")
    assert r.status_code == 200
    assert captured["limit"] == 51 and captured["offset"] == 50
    assert "page=3" in r.text and "page=1" in r.text
    assert "q=ES" in r.text                        # 過濾參數保留於分頁基底


def test_filter_typing_resets_to_page_one(as_user, monkeypatch):
    """即時過濾走搜尋表單(無 page 欄)→ 請求回第一頁(hidden 欄不帶 page)。"""
    async def _list(self, **kwargs):
        return _wo_rows(1)

    monkeypatch.setattr(web_routes.WorkOrderService, "list_work_orders", _list)
    r = client.get("/app/work-orders?scope=all")
    assert 'name="q"' in r.text
    # 搜尋表單只帶 scope/tab 隱藏欄,無 page 欄 → 打字送出的 hx-get 自然 page=1
    assert 'name="page"' not in r.text


# ---- 供應商 / 設備啟停(admin-only)----

def test_supplier_toggle_admin_calls_domain(as_user, _sup_data, monkeypatch):
    calls: list = []

    async def _set(self, org_id, active, *, actor):
        calls.append((org_id, active, actor.value))
        return SimpleNamespace()

    monkeypatch.setattr(web_routes.ContactsService, "set_organization_active", _set)
    as_user["user"].role = "admin"
    r = client.post("/app/suppliers/NORDIC/active", data={"active": "0"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/suppliers/NORDIC?msg=saved"
    assert calls == [("NORDIC", False, "human:jlee")]   # admin actor,active=0→False


def test_supplier_toggle_engineer_blocked(as_user, monkeypatch):
    called = {"n": 0}

    async def _set(self, *a, **k):
        called["n"] += 1

    monkeypatch.setattr(web_routes.ContactsService, "set_organization_active", _set)
    r = client.post("/app/suppliers/NORDIC/active", data={"active": "0"},
                    follow_redirects=False)       # 預設 engineer
    assert r.status_code == 303
    assert "msg=adminonly" in r.headers["location"]
    assert called["n"] == 0                        # route 先擋(domain 亦擋)


def test_supplier_detail_inactive_badge_all_users(as_user, _sup_data):
    """停用機構 → 所有使用者(含 engineer)見 muted 徽章。"""
    _sup_data["org"].is_active = False
    r = client.get("/app/suppliers/NORDIC")        # engineer
    assert r.status_code == 200
    assert i18n.translate("sup.inactive", "en") in r.text
    assert "badge--muted" in r.text
    assert 'action="/app/suppliers/NORDIC/active"' not in r.text  # engineer 無 admin 表單


def test_supplier_detail_admin_toggle_and_hint(as_user, _sup_data):
    as_user["user"].role = "admin"
    r = client.get("/app/suppliers/NORDIC")
    assert 'action="/app/suppliers/NORDIC/active"' in r.text
    assert "does not block RFQ" in r.text          # sup.status.hint(誠實描述真實行為)


def test_equipment_toggle_admin_calls_domain(as_user, _equip_data, monkeypatch):
    calls: list = []

    async def _sa(self, aid, active, *, actor):
        calls.append(("active", aid, active, actor.value))
        return SimpleNamespace()

    async def _sav(self, aid, available, *, actor):
        calls.append(("avail", aid, available, actor.value))
        return SimpleNamespace()

    monkeypatch.setattr(web_routes.AssetService, "set_asset_active", _sa)
    monkeypatch.setattr(web_routes.AssetService, "set_available_for_service", _sav)
    as_user["user"].role = "admin"
    r1 = client.post("/app/equipment/EID-70021/flags",
                     data={"flag": "available", "value": "0"}, follow_redirects=False)
    r2 = client.post("/app/equipment/EID-70021/flags",
                     data={"flag": "active", "value": "1"}, follow_redirects=False)
    assert r1.status_code == 303 and r2.status_code == 303
    assert ("avail", "EID-70021", False, "human:jlee") in calls
    assert ("active", "EID-70021", True, "human:jlee") in calls


def test_equipment_toggle_engineer_blocked(as_user, monkeypatch):
    called = {"n": 0}

    async def _sav(self, *a, **k):
        called["n"] += 1

    monkeypatch.setattr(web_routes.AssetService, "set_available_for_service", _sav)
    r = client.post("/app/equipment/EID-1/flags",
                    data={"flag": "available", "value": "0"}, follow_redirects=False)
    assert r.status_code == 303 and "msg=adminonly" in r.headers["location"]
    assert called["n"] == 0


def test_equipment_detail_admin_toggles_render(as_user, _equip_data):
    as_user["user"].role = "admin"
    r = client.get("/app/equipment/EID-70021")
    assert r.status_code == 200
    assert 'action="/app/equipment/EID-70021/flags"' in r.text
    assert 'name="flag"' in r.text
    # #3:旗標 hint 誠實化 —— available=純資訊性;is_active(退役)擋報修 + PM 生成
    assert "does not block any operation" in r.text          # eq.avail.help
    assert "blocks fault reporting and PM work-order generation" in r.text  # eq.active.help


def test_equipment_edit_form_admin_only_render(as_user, _equip_data):
    """admin 見主檔編輯區 + EID 唯讀;engineer 完全不渲染編輯表單。"""
    as_user["user"].role = "admin"
    r = client.get("/app/equipment/EID-70021")
    assert r.status_code == 200
    assert 'action="/app/equipment/EID-70021/edit"' in r.text
    assert "Edit equipment details" in r.text                # eq.edit(en)
    assert 'name="description"' in r.text and 'name="asset_type"' in r.text
    # engineer 不渲染
    as_user["user"].role = "engineer"
    r2 = client.get("/app/equipment/EID-70021")
    assert 'action="/app/equipment/EID-70021/edit"' not in r2.text


def test_equipment_edit_admin_calls_domain(as_user, _equip_data, monkeypatch):
    """admin POST /edit → 呼叫 update_asset(EID 大寫化 + actor + 各欄);302/303 saved。"""
    captured: dict = {}

    async def _upd(self, aid, **kwargs):
        captured["aid"] = aid
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(web_routes.AssetService, "update_asset", _upd)
    as_user["user"].role = "admin"
    r = client.post("/app/equipment/eid-70021/edit", follow_redirects=False, data={
        "description": "Aligner46 打線機", "asset_type": "Production",
        "asset_subtype": "WIREBOND", "department": "EQ", "line": "10K", "site": "PLANT-1",
        "model_no": "BX-820", "serial_no": "SN-1", "manufacturer": "Kestrel Systems",
        "host_name": "", "asset_ref": "", "product": "", "weblink": "", "comments": "",
        "process_segment_class": "", "owners": ["Owner Bob", "Ben Yeh"],
    })
    assert r.status_code == 303
    assert r.headers["location"] == "/app/equipment/EID-70021?msg=saved"
    assert captured["aid"] == "EID-70021"
    assert captured["actor"].value == "human:jlee"
    assert captured["description"] == "Aligner46 打線機"
    assert captured["owners"] == ["Owner Bob", "Ben Yeh"]  # 0031:多負責人傳入 update_asset


def test_equipment_edit_engineer_blocked(as_user, monkeypatch):
    """engineer POST /edit → adminonly redirect,domain 不被呼叫。"""
    called = {"n": 0}

    async def _upd(self, *a, **k):
        called["n"] += 1

    monkeypatch.setattr(web_routes.AssetService, "update_asset", _upd)
    r = client.post("/app/equipment/EID-70021/edit", follow_redirects=False, data={
        "description": "x", "asset_type": "Production", "site": "PLANT-1",
    })
    assert r.status_code == 303 and "msg=adminonly" in r.headers["location"]
    assert called["n"] == 0


def test_equipment_edit_domain_error_flashes(as_user, monkeypatch):
    """domain AssetError(未知 lookup 等)→ msg=err redirect(誠實訊息)。"""
    async def _upd(self, *a, **k):
        raise AssetError("unknown lookup")

    monkeypatch.setattr(web_routes.AssetService, "update_asset", _upd)
    as_user["user"].role = "admin"
    r = client.post("/app/equipment/EID-70021/edit", follow_redirects=False, data={
        "description": "x", "asset_type": "Nope", "site": "PLANT-1",
    })
    assert r.status_code == 303 and "msg=err" in r.headers["location"]


def test_equipment_detail_retired_badge_all_users(as_user, _equip_data, monkeypatch):
    """is_active=false → 所有使用者(含 engineer)見退役 muted 徽章。"""
    retired = SimpleNamespace(
        asset_id="EID-70021", description="Aligner46 打線機", asset_type="Production",
        asset_subtype="WIREBOND", department="EQ", line="10K", model_no="BX-820",
        serial_no="SN-1", manufacturer="Kestrel", available_for_service=True, is_active=False,
    )

    async def _get(self, aid):
        return retired if aid == "EID-70021" else None

    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get)
    r = client.get("/app/equipment/EID-70021")     # engineer
    assert r.status_code == 200
    assert i18n.translate("eq.inactive", "en") in r.text   # 退役徽章對所有使用者顯示
    assert 'action="/app/equipment/EID-70021/flags"' not in r.text  # engineer 無 admin 表單


# ---- 批 W2:設備 + 保養 UX(Jordan 2026-07-05 #4 #5)----

def test_equipment_type_chips_and_default_production(as_user, monkeypatch):
    """設備清單類別 chips(#4d):預設 Production 過濾;其他類別 + 全部 chip;類別碼原樣顯示。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.update(kwargs)
        return []

    async def _first(self, ot, oids):
        return {}

    monkeypatch.setattr(web_routes.AssetService, "list_assets", _list)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)
    r = client.get("/app/equipment")                       # 無 atype → 預設 Production
    assert r.status_code == 200
    assert captured["asset_type"] == "Production"           # 預設優先顯示 Production
    assert "atype=Support" in r.text and "atype=Meter" in r.text  # 其他類別 chip
    assert "atype=all" in r.text                            # 全部 chip
    client.get("/app/equipment?atype=all")                 # 全部 → 不過濾
    assert captured["asset_type"] is None
    client.get("/app/equipment?atype=Jig")                 # 指定類別
    assert captured["asset_type"] == "Jig"


def test_equipment_parts_truncated_and_expand(as_user, _equip_data, monkeypatch):
    """單機用料截 5 筆 + 「全部(N)」展開(#4c)。"""
    rows = [
        SimpleNamespace(item_code=f"ES{i:03d}", qty_delta=-1.0, work_order_no=None,
                        occurred_at=datetime(2026, 7, i, 10, 0), txn_id=i, kind="ISSUE")
        for i in range(1, 9)                               # 8 筆(> 5)
    ]

    async def _usage(self, ids, **kw):
        return rows

    async def _cancelled(self, ids):
        return set()

    monkeypatch.setattr(web_routes.InventoryService, "list_asset_part_usage", _usage)
    monkeypatch.setattr(web_routes.InventoryService, "cancelled_asset_issue_ids", _cancelled)
    r = client.get("/app/equipment/EID-70021")             # 預設:截 5 筆
    assert r.status_code == 200
    assert "ES001" in r.text and "ES005" in r.text
    assert "ES006" not in r.text                           # 第 6 筆被截
    assert "parts=all" in r.text and "(8)" in r.text       # 全部(N)連結
    r2 = client.get("/app/equipment/EID-70021?parts=all")  # 展開:全部 + 收合
    assert "ES006" in r2.text and "ES008" in r2.text
    assert i18n.translate("eq.parts_collapse", "en") in r2.text


def test_equipment_retired_report_disabled(as_user, _equip_data, monkeypatch):
    """退役設備(is_active=false):報修按鈕 disable + 原因(#4b UI)。"""
    retired = SimpleNamespace(
        asset_id="EID-70021", description="Aligner46", asset_type="Production",
        asset_subtype="WIREBOND", department="EQ", line="10K", model_no="BX-820",
        serial_no="SN-1", manufacturer="Kestrel", available_for_service=True, is_active=False,
    )

    async def _get(self, aid):
        return retired if aid == "EID-70021" else None

    monkeypatch.setattr(web_routes.AssetService, "get_asset", _get)
    r = client.get("/app/equipment/EID-70021")
    assert r.status_code == 200
    assert "disabled" in r.text                            # 報修鈕停用
    assert "cannot report a fault" in r.text               # eq.report_retired(en)
    assert '/app/report?eid=EID-70021' not in r.text       # 無可點的報修連結


def test_report_submit_retired_asset_banner(_write_env, monkeypatch):
    """退役資產報修:domain 拒 → 友善 banner(非 500,#4b)。"""
    async def _open(self, **kw):
        raise WorkOrderError("asset EID-9 is retired")

    monkeypatch.setattr(web_routes.WorkOrderService, "open_work_order", _open)
    r = client.post("/app/report", data={"asset_id": "EID-9", "brief_description": "x"})
    assert r.status_code == 200
    assert "reactive work orders" in r.text                # report.retired(en)友善提示


def test_equipment_avail_help_shown_for_admin(as_user, _equip_data):
    """設備旗標白話文案(#4a):可排單服務 help「與 up/down 無關」+ 在籍語意標籤。"""
    as_user["user"].role = "admin"
    r = client.get("/app/equipment/EID-70021")
    assert r.status_code == 200
    assert "up/down" in r.text                             # eq.avail.help(誠實澄清)
    assert i18n.translate("eq.active.label", "en") in r.text  # 在籍狀態(在籍/已退役)


def test_pm_view_month_label(as_user, _pm_cal_data):
    """「月曆」標籤改「月」(#5a):Month / 月 / Tháng。"""
    assert i18n.translate("pm.view.calendar", "en") == "Month"
    assert i18n.translate("pm.view.calendar", "zh-TW") == "月"
    assert i18n.translate("pm.view.calendar", "vi") == "Tháng"
    r = client.get("/app/pm/calendar?view=month&d=2026-07-15")
    assert "Month" in r.text


def test_pm_calendar_scope_toggle_all_views(as_user, _pm_cal_data):
    """Mine/All 切換在月/週/日三視圖都保留且穿透 view + 錨定日(#5b)。"""
    for view in ("month", "week", "day"):
        r = client.get(f"/app/pm/calendar?view={view}&d=2026-07-10")
        assert r.status_code == 200
        assert f"/app/pm/calendar?view={view}&d=2026-07-10&scope=all" in r.text
        assert f"/app/pm/calendar?view={view}&d=2026-07-10&scope=mine" in r.text


def test_pm_backfill_button_when_due(as_user, monkeypatch):
    """補開工單鈕(#5e):已到期(含週末提前)且本期未生成 → 顯示 + help;鈕改名「補開工單」。"""
    due_pm = SimpleNamespace(
        pm_id="PM-DUE", asset_id="EID-1", task_id="T-1",
        next_due_date=date(2020, 1, 1), frequency_interval=30, frequency_unit="Days",
        assigned_person=None, last_work_order_no=None,
    )

    async def _list(self, **kw):
        return [due_pm]

    async def _task(self, tid):
        return SimpleNamespace(description="季保")

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _list)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)
    r = client.get("/app/pm")
    assert r.status_code == 200
    assert i18n.translate("pm.backfill", "en") in r.text          # 「Open WO now」
    assert "/app/pm/PM-DUE/backfill" in r.text                     # #5b:改連結至確認頁
    assert "opened automatically by the scheduler" in r.text      # pm.backfill.hint


def test_pm_backfill_hidden_when_generated_this_cycle(as_user, monkeypatch):
    """本期已生成(last WO 非終態)→ 補開工單鈕不顯示(#5e 冪等一致)。"""
    pm = SimpleNamespace(
        pm_id="PM-GEN", asset_id="EID-1", task_id="T-1",
        next_due_date=date(2020, 1, 1), frequency_interval=30, frequency_unit="Days",
        assigned_person=None, last_work_order_no=555,
    )

    async def _list(self, **kw):
        return [pm]

    async def _task(self, tid):
        return SimpleNamespace(description="季保")

    async def _getwo(self, no):
        return SimpleNamespace(work_order_no=555, status="OPEN")  # 非終態 = 本期已生成

    monkeypatch.setattr(web_routes.PmScheduleService, "list_pm_schedules", _list)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)
    monkeypatch.setattr(web_routes.WorkOrderService, "get_work_order", _getwo)
    r = client.get("/app/pm")
    assert r.status_code == 200
    assert "/app/pm/PM-GEN/backfill" not in r.text                # 本期已生成 → 隱藏


def test_pm_backfill_confirm_page(as_user, monkeypatch):
    """#5b:補開工單確認頁 GET 200 + 預填 owner + 送出至 /generate + 取消回 /app/pm(零寫入)。"""
    pm = SimpleNamespace(
        pm_id="PM-DUE", asset_id="EID-1", task_id="T-1",
        next_due_date=date(2020, 1, 1), frequency_interval=30, frequency_unit="Days",
        assigned_person="Alice Fang", effective_assignee="Alice Fang",  # 0029:預填有效 assignee
        last_work_order_no=None,
    )

    async def _get(self, pid):
        return pm

    async def _task(self, tid):
        return SimpleNamespace(description="季保")

    monkeypatch.setattr(web_routes.PmScheduleService, "get_pm_schedule", _get)
    monkeypatch.setattr(web_routes.TaskService, "get_task", _task)
    r = client.get("/app/pm/PM-DUE/backfill")
    assert r.status_code == 200
    assert "Alice Fang" in r.text                                # 0031:顯示有效 assignee
    assert 'action="/app/pm/PM-DUE/generate"' in r.text           # 送出至既有生成 route
    assert 'href="/app/pm"' in r.text                             # 取消連結


def test_pm_backfill_confirm_redirects_when_not_due(as_user, monkeypatch):
    """#5b:PM 不存在 / 非到期 → GET backfill redirect 回 /app/pm(零寫入)。"""
    async def _get(self, pid):
        return None

    monkeypatch.setattr(web_routes.PmScheduleService, "get_pm_schedule", _get)
    r = client.get("/app/pm/NOPE/backfill", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/app/pm"


def test_pm_generate_passes_assigned_person_override(as_user, monkeypatch):
    """#5b:POST /generate 帶 assigned_person → 透傳 override;空字串 → None。"""
    cap = {}

    async def _gen(self, *, pm_id, actor, assigned_person=None):
        cap["assigned_person"] = assigned_person
        return SimpleNamespace(work_order_no=999)

    monkeypatch.setattr(web_routes.WorkOrderService, "generate_pm_work_order", _gen)
    r = client.post(
        "/app/pm/PM-DUE/generate", data={"assigned_person": "Ben Yeh"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert cap["assigned_person"] == "Ben Yeh"
    r = client.post(
        "/app/pm/PM-DUE/generate", data={"assigned_person": "  "}, follow_redirects=False
    )
    assert cap["assigned_person"] is None                         # 空 → None(沿用 pm 的)


# ---- 助理 dock → Hermes gateway 實接(ADR-020)----

def _hermes_settings(url="http://gw.internal:8080", secret="HSECRET-XYZ"):
    """假 Settings:只帶助理路由/_render 會讀的三個屬性(monkeypatch web_routes.get_settings)。"""
    return SimpleNamespace(
        hermes_gateway_url=url,
        hermes_gateway_secret=secret,
        hermes_configured=bool(url and secret),
    )


def _fake_httpx_ok(reply, capture):
    """假 httpx.AsyncClient:記錄 post 的 url/headers/json,回固定 reply(成功 2xx)。"""
    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"reply": reply}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            capture["url"] = url
            capture["headers"] = headers
            capture["json"] = json
            return _Resp()

    return _Client


def _fake_httpx_err(exc):
    """假 httpx.AsyncClient:post 一律 raise(模擬連線/逾時錯)。"""
    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise exc

    return _Client


def test_assistant_requires_login(anon):
    """未登入 POST /app/assistant(phase 1)→ 轉登入(拒匿名)。"""
    r = client.post("/app/assistant", data={"message": "hi"}, follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


def test_assistant_reply_requires_login(anon):
    """未登入 POST /app/assistant/{id}/reply(phase 2)→ 轉登入(拒匿名)。"""
    r = client.post(
        "/app/assistant/5/reply", data={"message_id": "1"}, follow_redirects=False
    )
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


def _patch_phase1(monkeypatch, *, count=0, conv=None, open_convs=None, add_capture=None,
                  new_conv_id=77, new_msg_id=101):
    """monkeypatch phase 1(POST /assistant)會呼叫的 AssistantService 方法。"""
    async def _count(self, user_id):
        return count

    async def _get_conv(self, user_id, cid):
        return conv

    async def _list_open(self, user_id):
        return open_convs if open_convs is not None else [
            SimpleNamespace(id=new_conv_id, title="hello there", closed_at=None)
        ]

    async def _add_user(self, *, user_id, conversation_id, content, actor):
        if add_capture is not None:
            add_capture["user"] = {"conversation_id": conversation_id, "content": content}
        return SimpleNamespace(id=new_conv_id, title=content), SimpleNamespace(id=new_msg_id)

    monkeypatch.setattr(web_routes.AssistantService, "count_open_conversations", _count)
    monkeypatch.setattr(web_routes.AssistantService, "get_conversation", _get_conv)
    monkeypatch.setattr(web_routes.AssistantService, "list_open_conversations", _list_open)
    monkeypatch.setattr(web_routes.AssistantService, "add_user_message", _add_user)


def test_assistant_disabled_when_unconfigured(as_user, monkeypatch):
    """gateway 未配置 → 「尚未啟用」誠實訊息,且不建 user 訊息(phase 1 前置擋)。"""
    called = {"add": False}

    async def _add_user(self, **kw):
        called["add"] = True
        return SimpleNamespace(id=1), SimpleNamespace(id=1)

    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings(url=None, secret=None))
    monkeypatch.setattr(web_routes.AssistantService, "add_user_message", _add_user)
    r = client.post("/app/assistant", data={"message": "hi", "conversation_id": ""})
    assert r.status_code == 200
    assert "enabled yet" in r.text          # assistant.disabled(單引號被 escape,取無引號片段)
    assert called["add"] is False           # 未建 user 訊息


def test_assistant_phase1_persists_user_and_updates_sessions(as_user, monkeypatch):
    """phase 1:落 user 訊息 → 回 user 泡泡 + pending 觸發器 + OOB 切換列 + OOB conv-id + cookie。"""
    add_cap: dict = {}
    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings())
    _patch_phase1(monkeypatch, count=0, conv=None, add_capture=add_cap,
                  new_conv_id=77, new_msg_id=101)
    r = client.post("/app/assistant", data={"message": "hello there", "conversation_id": ""})
    assert r.status_code == 200
    # 新對話 → 惰性建(conversation_id=None 傳進 domain)
    assert add_cap["user"]["conversation_id"] is None
    assert add_cap["user"]["content"] == "hello there"
    # user 泡泡(伺服端渲染,不再前端樂觀插入)
    assert 'class="chat-msg chat-msg--user">hello there</div>' in r.text
    # pending 觸發器 = phase 2:hx-post …/{conv}/reply + hx-vals message_id
    assert 'hx-post="/app/assistant/77/reply"' in r.text
    assert '{"message_id": 101}' in r.text
    assert "hx-trigger=\"load\"" in r.text
    # OOB 重渲染切換列(修「新會話標籤不出現」)+ 新會話 chip
    assert 'id="assistant-sessions"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "hello there" in r.text                            # 切換列 chip
    # OOB 更新 hidden conversation_id + cookie 記住當前對話
    assert 'id="assistant-conv-id"' in r.text
    assert 'value="77"' in r.text
    assert "cmms_assistant_conv=77" in r.headers.get("set-cookie", "")


def test_assistant_open_limit_blocks_new_conversation(as_user, monkeypatch):
    """開啟中對話達上限 + 新對話 → 友善提示、不建 user 訊息。"""
    called = {"add": False}

    async def _count(self, user_id):
        return web_routes.AssistantService.MAX_OPEN_CONVERSATIONS

    async def _add_user(self, **kw):
        called["add"] = True
        return SimpleNamespace(id=1), SimpleNamespace(id=1)

    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings())
    monkeypatch.setattr(web_routes.AssistantService, "count_open_conversations", _count)
    monkeypatch.setattr(web_routes.AssistantService, "add_user_message", _add_user)
    r = client.post("/app/assistant", data={"message": "hi", "conversation_id": ""})
    assert r.status_code == 200
    assert "end one" in r.text                               # assistant.limit(en)
    assert called["add"] is False


def _patch_phase2(monkeypatch, *, conv=None, umsg=None, nxt=None, history=None,
                  add_capture=None):
    """monkeypatch phase 2(POST /assistant/{id}/reply)會呼叫的 AssistantService 方法。"""
    async def _get_conv(self, user_id, cid):
        return conv

    async def _get_msg(self, user_id, mid):
        return umsg

    async def _next(self, user_id, cid, mid):
        return nxt

    async def _hist(self, user_id, cid, *a, **k):
        return history or []

    async def _add_asst(self, *, user_id, conversation_id, content, actor):
        if add_capture is not None:
            add_capture["assistant"] = {"conversation_id": conversation_id, "content": content}
        return SimpleNamespace(id=999)

    monkeypatch.setattr(web_routes.AssistantService, "get_conversation", _get_conv)
    monkeypatch.setattr(web_routes.AssistantService, "get_message", _get_msg)
    monkeypatch.setattr(web_routes.AssistantService, "next_message_after", _next)
    monkeypatch.setattr(web_routes.AssistantService, "recent_history", _hist)
    monkeypatch.setattr(web_routes.AssistantService, "add_assistant_message", _add_asst)


def test_assistant_phase2_success_persists_and_renders_safe(as_user, monkeypatch):
    """phase 2 成功:回覆安全渲染(<script> escape + /app 連結)、落 assistant 訊息、token 不外洩。"""
    cap: dict = {}
    add_cap: dict = {}
    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings(secret="HSECRET-XYZ"))

    async def _mint(self, *, session_token, agent, scope):
        assert agent == "hermes" and scope == "pilot"
        return "SCOPED-TOKEN-123"

    monkeypatch.setattr(web_routes.IdentityService, "mint_scoped_token", _mint)
    reply = "See [EID-70021](/app/equipment/EID-70021) <script>alert(1)</script>\nline2"
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _fake_httpx_ok(reply, cap))
    conv = SimpleNamespace(id=77, closed_at=None)
    umsg = SimpleNamespace(id=101, conversation_id=77, role="user", content="hello")
    _patch_phase2(monkeypatch, conv=conv, umsg=umsg, nxt=None, add_capture=add_cap)

    r = client.post("/app/assistant/77/reply", data={"message_id": "101"})
    assert r.status_code == 200
    # 安全渲染:script escape、/app 連結成 <a>
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert '<a href="/app/equipment/EID-70021">EID-70021</a>' in r.text
    # scoped token / gateway secret 絕不出現在回應 HTML
    assert "SCOPED-TOKEN-123" not in r.text
    assert "HSECRET-XYZ" not in r.text
    # gateway 收到 token + 該則 user 訊息內容 + secret header
    assert cap["json"]["scoped_token"] == "SCOPED-TOKEN-123"
    assert cap["json"]["message"] == "hello"
    assert cap["headers"]["X-Hermes-Secret"] == "HSECRET-XYZ"
    # 落 assistant 訊息(原文;渲染只在顯示層)
    assert add_cap["assistant"]["conversation_id"] == 77
    assert add_cap["assistant"]["content"] == reply


def test_assistant_phase2_gateway_error_is_friendly_with_retry_not_persisted(as_user, monkeypatch):
    """phase 2 gateway 錯 → 友善泡泡 + 重試鈕(同 message_id);user 訊息保留、不落 assistant。"""
    import httpx as _httpx

    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings())

    async def _mint(self, *, session_token, agent, scope):
        return "TOK-SECRET"

    monkeypatch.setattr(web_routes.IdentityService, "mint_scoped_token", _mint)
    monkeypatch.setattr(
        web_routes.httpx, "AsyncClient", _fake_httpx_err(_httpx.ConnectError("boom"))
    )
    add_cap: dict = {}
    conv = SimpleNamespace(id=77, closed_at=None)
    umsg = SimpleNamespace(id=101, conversation_id=77, role="user", content="hi")
    _patch_phase2(monkeypatch, conv=conv, umsg=umsg, nxt=None, add_capture=add_cap)

    r = client.post("/app/assistant/77/reply", data={"message_id": "101"})
    assert r.status_code == 200
    assert "unavailable right now" in r.text                 # assistant.error
    assert "TOK-SECRET" not in r.text
    # 重試鈕:同 message_id 重送 phase 2
    assert 'hx-post="/app/assistant/77/reply"' in r.text
    assert '{"message_id": 101}' in r.text
    assert "assistant" not in add_cap                        # gateway 失敗 → 不落 assistant


def test_assistant_phase2_replay_returns_existing_without_gateway(as_user, monkeypatch):
    """phase 2 重放(該 user 訊息之後已有 assistant)→ 直接回既存回覆,不鑄 token、不打 gateway。"""
    called = {"http": False, "mint": False}

    class _Boom:
        def __init__(self, *a, **kw):
            called["http"] = True

    async def _mint(self, **kw):
        called["mint"] = True
        return "X"

    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings())
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _Boom)
    monkeypatch.setattr(web_routes.IdentityService, "mint_scoped_token", _mint)
    add_cap: dict = {}
    conv = SimpleNamespace(id=77, closed_at=None)
    umsg = SimpleNamespace(id=101, conversation_id=77, role="user", content="hi")
    prior = SimpleNamespace(id=102, role="assistant", content="already answered EID-70021")
    _patch_phase2(monkeypatch, conv=conv, umsg=umsg, nxt=prior, add_capture=add_cap)

    r = client.post("/app/assistant/77/reply", data={"message_id": "101"})
    assert r.status_code == 200
    assert "already answered" in r.text                      # 既存回覆(安全渲染)
    assert '<a href="/app/equipment/EID-70021">EID-70021</a>' in r.text
    assert called["http"] is False                           # 不打 gateway
    assert called["mint"] is False                           # 不鑄 token
    assert "assistant" not in add_cap                        # 不重寫


def test_assistant_phase2_non_owner_no_write(as_user, monkeypatch):
    """phase 2 非本人 / 找不到會話 → 錯誤泡泡、不打 gateway、不落 assistant(不洩漏)。"""
    called = {"http": False}

    class _Boom:
        def __init__(self, *a, **kw):
            called["http"] = True

    monkeypatch.setattr(web_routes, "get_settings", lambda: _hermes_settings())
    monkeypatch.setattr(web_routes.httpx, "AsyncClient", _Boom)
    add_cap: dict = {}
    # get_conversation → None(非本人 / 不存在)
    _patch_phase2(monkeypatch, conv=None, umsg=None, nxt=None, add_capture=add_cap)

    r = client.post("/app/assistant/999/reply", data={"message_id": "1"})
    assert r.status_code == 200
    assert "unavailable right now" in r.text
    assert called["http"] is False
    assert "assistant" not in add_cap


def test_assistant_panel_unauth_no_crash(anon):
    """未登入 GET /app/assistant/panel → 空殼 200(不炸、不轉址)。"""
    r = client.get("/app/assistant/panel")
    assert r.status_code == 200
    assert r.text.strip() == ""


def test_assistant_panel_renders_current_conversation(as_user, monkeypatch):
    """panel 渲染切換列 + 當前對話訊息(bot 訊息經安全渲染)+ 表單 + 結束鈕 + set cookie。"""
    conv = SimpleNamespace(id=5, title="pump leak on line 2", closed_at=None)

    async def _list_open(self, user_id):
        return [conv]

    async def _get_conv(self, user_id, cid):
        return conv

    async def _msgs(self, user_id, cid):
        return [
            SimpleNamespace(role="user", content="<b>hi</b>"),
            SimpleNamespace(role="assistant", content="see [EID-70021](/app/equipment/EID-70021)"),
        ]

    monkeypatch.setattr(web_routes.AssistantService, "list_open_conversations", _list_open)
    monkeypatch.setattr(web_routes.AssistantService, "get_conversation", _get_conv)
    monkeypatch.setattr(web_routes.AssistantService, "get_messages", _msgs)
    r = client.get("/app/assistant/panel")
    assert r.status_code == 200
    assert 'id="assistant-form"' in r.text                   # 表單
    assert "pump leak on line 2" in r.text                   # 切換列 chip
    assert 'name="conversation_id"' in r.text
    assert 'value="5"' in r.text                             # 當前對話 id 進表單
    assert "&lt;b&gt;hi&lt;/b&gt;" in r.text                 # user 訊息純 escape
    assert '<a href="/app/equipment/EID-70021">EID-70021</a>' in r.text  # bot 安全渲染
    assert "cmms_assistant_conv=5" in r.headers.get("set-cookie", "")


def test_assistant_panel_new_is_blank(as_user, monkeypatch):
    """panel?new=1 → 空白新對話態(無當前 id、刪 cookie);切換列仍列既有開啟中對話。"""
    async def _list_open(self, user_id):
        return [SimpleNamespace(id=9, title="old chat", closed_at=None)]

    monkeypatch.setattr(web_routes.AssistantService, "list_open_conversations", _list_open)
    r = client.get("/app/assistant/panel?new=1")
    assert r.status_code == 200
    assert "old chat" in r.text                              # 切換列仍在
    # 表單 conversation_id 空(新對話);cookie 被刪
    assert 'name="conversation_id" value=""' in r.text
    assert 'cmms_assistant_conv=""' in r.headers.get("set-cookie", "") or \
        "cmms_assistant_conv=;" in r.headers.get("set-cookie", "")


def test_assistant_close_switches_to_empty(as_user, monkeypatch):
    """結束對話 → close_conversation 被呼叫;無其他開啟中 → 空白態、刪 cookie。"""
    called = {"close": None}

    async def _close(self, user_id, cid, actor):
        called["close"] = cid

    async def _list_open(self, user_id):
        return []

    monkeypatch.setattr(web_routes.AssistantService, "close_conversation", _close)
    monkeypatch.setattr(web_routes.AssistantService, "list_open_conversations", _list_open)
    r = client.post("/app/assistant/5/close")
    assert r.status_code == 200
    assert called["close"] == 5
    assert 'id="assistant-form"' in r.text                   # 仍可開新對話


def test_assistant_panel_hanging_user_renders_pending(as_user, monkeypatch):
    """panel 懸掛 user 訊息(最後一則是 user、無後續 assistant)→ 續跑 pending 觸發器
    (轉跳回來自動續等 / 撿回伺服端已完成回覆)。"""
    conv = SimpleNamespace(id=5, title="pump leak", closed_at=None)

    async def _list_open(self, user_id):
        return [conv]

    async def _get_conv(self, user_id, cid):
        return conv

    async def _msgs(self, user_id, cid):
        return [SimpleNamespace(role="user", content="still leaking", id=55)]

    monkeypatch.setattr(web_routes.AssistantService, "list_open_conversations", _list_open)
    monkeypatch.setattr(web_routes.AssistantService, "get_conversation", _get_conv)
    monkeypatch.setattr(web_routes.AssistantService, "get_messages", _msgs)
    r = client.get("/app/assistant/panel")
    assert r.status_code == 200
    # 續跑 pending 觸發器:對懸掛 user 訊息(id=55)自動觸發 phase 2
    assert 'hx-post="/app/assistant/5/reply"' in r.text
    assert '{"message_id": 55}' in r.text
    assert "data-assistant-pending" in r.text


def test_equipment_search_default_category_all_with_query(as_user, monkeypatch):
    """W2 殘留修:有搜尋字串且未顯式選類別 → 類別預設「全部」(不過濾);無 q → Production。"""
    captured: dict = {}

    async def _list(self, **kwargs):
        captured.clear()
        captured.update(kwargs)
        return []

    async def _first(self, ot, oids):
        return {}

    monkeypatch.setattr(web_routes.AssetService, "list_assets", _list)
    monkeypatch.setattr(web_routes.AttachmentService, "first_attachment_map", _first)

    client.get("/app/equipment?q=DISPENSER")                    # 有 q 無顯式類別 → 全部
    assert captured["asset_type"] is None
    assert captured["search"] == "DISPENSER"

    client.get("/app/equipment")                             # 無 q → Production 預設
    assert captured["asset_type"] == "Production"

    client.get("/app/equipment?q=DISPENSER&atype=Production")   # 顯式選類別 → 尊重(仍過濾)
    assert captured["asset_type"] == "Production"


# ---- 我的提案(唯讀;登入者只見自己的提案 + 各狀態 badge)----

def test_my_proposals_renders_and_filters_by_proposer(as_user, monkeypatch):
    """登入者見自己的提案(proposed_by == human:<user_id>)+ 各狀態 badge render。
    看不到別人的:route 只查 proposed_by == 登入者,以 captured 斷言傳入值。"""
    from datetime import UTC, datetime

    captured: dict = {}

    async def _list(self, *, proposed_by, limit, offset):
        captured.update(proposed_by=proposed_by, limit=limit, offset=offset)
        return [
            SimpleNamespace(
                pending_token="t1", operation="update_item",
                params={"item_code": "ES000701"},
                dry_run_diff={"changes": {"bin_location": {"from": "A1", "to": "B2"}}},
                status="PENDING", confirmed_by=None,
                created_at=datetime(2026, 7, 5, 10, 0, tzinfo=UTC),
                expires_at=datetime(2030, 1, 1, 0, 0, tzinfo=UTC),  # 未過期 → PENDING
                resolved_at=None,
            ),
            SimpleNamespace(
                pending_token="t2", operation="void_work_order",
                params={"work_order_no": 30318, "reason": "duplicate report"},
                dry_run_diff=None, status="CONFIRMED", confirmed_by="human:admin",
                created_at=datetime(2026, 7, 4, 9, 0, tzinfo=UTC),
                expires_at=datetime(2026, 7, 11, 9, 0, tzinfo=UTC),
                resolved_at=datetime(2026, 7, 4, 12, 0, tzinfo=UTC),
            ),
            SimpleNamespace(
                pending_token="t3", operation="void_work_order",
                params={"work_order_no": 24000, "reason": "mistake"},
                dry_run_diff=None, status="REJECTED", confirmed_by="human:admin",
                created_at=datetime(2026, 7, 3, 9, 0, tzinfo=UTC),
                expires_at=datetime(2026, 7, 10, 9, 0, tzinfo=UTC),
                resolved_at=datetime(2026, 7, 3, 10, 0, tzinfo=UTC),
            ),
            SimpleNamespace(  # PENDING 但已過 expires_at → 顯示 EXPIRED(不改 DB)
                pending_token="t4", operation="update_item",
                params={"item_code": "ES000702"}, dry_run_diff=None,
                status="PENDING", confirmed_by=None,
                created_at=datetime(2026, 7, 2, 9, 0, tzinfo=UTC),
                expires_at=datetime(2026, 7, 2, 10, 0, tzinfo=UTC),
                resolved_at=None,
            ),
        ]

    monkeypatch.setattr(
        web_routes.WorkOrderService, "list_proposals_by_proposer", _list
    )
    r = client.get("/app/proposals")
    assert r.status_code == 200
    # route 只查登入者本人(human:<user_id>)—— 看不到別人的
    assert captured["proposed_by"] == "human:jlee"
    # 各狀態 badge render(en 標籤)
    assert "Pending review" in r.text
    assert "Approved" in r.text
    assert "Rejected" in r.text
    assert "Expired" in r.text                       # t4:PENDING 過期 → EXPIRED 顯示
    assert "ES000701" in r.text and "WO-30318" in r.text
    assert "bin_location" in r.text                  # update_item dry-run diff 欄位差異


def test_my_proposals_empty(as_user, monkeypatch):
    async def _list(self, *, proposed_by, limit, offset):
        return []

    monkeypatch.setattr(
        web_routes.WorkOrderService, "list_proposals_by_proposer", _list
    )
    r = client.get("/app/proposals")
    assert r.status_code == 200
    assert "submitted any proposals." in r.text  # 撇號經 Jinja 轉義,比對無撇號子字串


def test_my_proposals_requires_login(anon):
    r = client.get("/app/proposals", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


# ---- Slice B:通知收件人 admin 頁(/admin/notify)----

def test_admin_notify_forbids_engineer(as_user):
    r = client.get("/admin/notify")                        # 預設 engineer
    assert r.status_code == 403


def test_admin_notify_requires_login(anon):
    r = client.get("/admin/notify", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/app/login"


def test_admin_notify_renders(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _recips(self):
        return [SimpleNamespace(
            id=1, name="team", email="team@x.com", telegram_chat_id="-100",
            assignee_name="Sam Wu", notify_on_open=True, notify_on_close=True,
            is_active=True, watches=["Sam Wu", "Jordan Lee"],
        )]

    async def _recent(self, limit=20):
        return [SimpleNamespace(
            work_order_no=30160, event="opened", channel="email", recipient_id=1,
            status="sent", attempts=0, last_error=None,
        )]

    async def _failed(self):
        return 0

    monkeypatch.setattr(_adm.NotificationService, "list_recipients", _recips)
    monkeypatch.setattr(_adm.NotificationService, "list_recent_outbox", _recent)
    monkeypatch.setattr(_adm.NotificationService, "count_failed", _failed)
    r = client.get("/admin/notify")
    assert r.status_code == 200
    assert "team@x.com" in r.text and "30160" in r.text
    # Slice D:關注輸入(multi_input field)+ 目前關注清單顯示
    assert 'name="watch_assignees"' in r.text
    assert "Sam Wu" in r.text and "Jordan Lee" in r.text


def test_admin_notify_create_update_toggle(as_user, monkeypatch):
    as_user["user"].role = "admin"
    calls: list = []

    async def _create(self, *, name, actor, **kw):
        calls.append(
            ("create", name, kw.get("notify_on_open"), kw.get("email"), actor.value,
             tuple(kw.get("watch_assignees") or ()))
        )
        return SimpleNamespace(id=1)

    async def _update(self, rid, *, name, actor, **kw):
        calls.append(("update", rid, name, actor.value, tuple(kw.get("watch_assignees") or ())))
        return SimpleNamespace(id=rid)

    async def _toggle(self, rid, active, *, actor):
        calls.append(("toggle", rid, active, actor.value))
        return SimpleNamespace(id=rid)

    monkeypatch.setattr(_adm.NotificationService, "create_recipient", _create)
    monkeypatch.setattr(_adm.NotificationService, "update_recipient", _update)
    monkeypatch.setattr(_adm.NotificationService, "set_recipient_active", _toggle)

    r = client.post("/admin/notify", data={
        "name": "team", "email": "t@x.com", "telegram_chat_id": "-100",
        "notify_on_open": "1", "notify_on_close": "",
        "watch_assignees": ["Sam Wu", "Sam Wu"],
    }, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/notify"

    client.post("/admin/notify/5/update", data={
        "name": "owner", "watch_assignees": ["Alan"],
    }, follow_redirects=False)
    client.post("/admin/notify/5/toggle", data={"is_active": "0"}, follow_redirects=False)

    assert ("create", "team", True, "t@x.com", "human:jlee", ("Sam Wu", "Sam Wu")) in calls
    assert ("update", 5, "owner", "human:jlee", ("Alan",)) in calls
    assert ("toggle", 5, False, "human:jlee") in calls


# ---- /admin/owners:批次指定負責人(asset.owner,0029)----

def test_admin_owners_forbids_engineer(as_user):
    r = client.get("/admin/owners")                        # 預設 engineer
    assert r.status_code == 403


def test_admin_owners_lists_ownerless(as_user, monkeypatch):
    as_user["user"].role = "admin"

    async def _list(self, *, search=None, only_missing=True, limit=300):
        return [SimpleNamespace(
            asset_id="EID-70003", description="De-Ion Air Gun", line="ASSY", owner=None,
        )]

    async def _pm(self, ids):
        return {"EID-70003": 4}

    monkeypatch.setattr(_adm.AssetService, "list_for_owner_admin", _list)
    monkeypatch.setattr(_adm.AssetService, "pm_counts", _pm)
    r = client.get("/admin/owners")
    assert r.status_code == 200
    assert "EID-70003" in r.text and "De-Ion Air Gun" in r.text
    assert 'name="asset_ids"' in r.text                     # 逐列勾選框
    assert 'id="owner-all"' in r.text                       # 全選框
    assert "4" in r.text                                    # PM 數


def test_admin_owners_missing_zero_shows_owned(as_user, monkeypatch):
    as_user["user"].role = "admin"
    captured: dict = {}

    async def _list(self, *, search=None, only_missing=True, limit=300):
        captured["only_missing"] = only_missing
        return [SimpleNamespace(
            asset_id="EID-001", description="Has owner", line="ASSY", owners=["Ben Yeh"],
        )]

    async def _pm(self, ids):
        return {}

    monkeypatch.setattr(_adm.AssetService, "list_for_owner_admin", _list)
    monkeypatch.setattr(_adm.AssetService, "pm_counts", _pm)
    r = client.get("/admin/owners?missing=0")
    assert r.status_code == 200
    assert captured["only_missing"] is False               # 全部(含已有 owner)
    assert "Ben Yeh" in r.text


def test_admin_owners_apply_assigns(as_user, monkeypatch):
    as_user["user"].role = "admin"
    captured: dict = {}

    async def _bulk(self, *, asset_ids, owners, actor):
        captured["asset_ids"] = asset_ids
        captured["owners"] = owners
        captured["actor"] = actor.value
        return 2

    monkeypatch.setattr(_adm.AssetService, "set_owner_bulk", _bulk)
    r = client.post("/admin/owners", data={
        "asset_ids": ["EID-001", "EID-002"], "owners": ["Alice Fang", "Ben Yeh"],
        "q": "", "missing": "1",
    }, follow_redirects=False)
    assert r.status_code == 303
    assert "ok=2" in r.headers["location"]
    assert captured["asset_ids"] == ["EID-001", "EID-002"]
    assert captured["owners"] == ["Alice Fang", "Ben Yeh"]
    assert captured["actor"] == "human:jlee"             # 操作者 = 登入 admin


def test_admin_owners_apply_no_selection_errs(as_user):
    as_user["user"].role = "admin"
    # 無勾選 → route 先擋(不呼叫 service),err redirect
    r = client.post("/admin/owners", data={
        "owner": "Alice Fang", "q": "", "missing": "1",
    }, follow_redirects=False)
    assert r.status_code == 303 and "err=" in r.headers["location"]
