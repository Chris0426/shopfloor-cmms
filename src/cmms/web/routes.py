"""工程師操作台 web 路由(ADR-019 thin client + ADR-022 auth)。

掛 `/app` 前綴(避免撞 `/work-orders` 等 JSON 讀 API)。除 `/app/login` 外皆需登入:
session cookie → `IdentityService.resolve_user`;未登入 → 轉 `/app/login`。
locale:已登入取 `user_account.ui_locale`(ADR-023);未登入(登入頁)協商 cookie/Accept-Language。
寫入(report submit / add-note)以登入的 `human:<id>` 為 actor —— 後續切片接。
"""

from __future__ import annotations

import calendar
import contextlib
import logging
import secrets
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.asset.models import Asset
from cmms.domain.asset.service import AssetError, AssetService
from cmms.domain.assistant.service import AssistantError, AssistantService
from cmms.domain.attachment.service import AttachmentError, AttachmentService
from cmms.domain.attachment.transform import content_type_for
from cmms.domain.contacts.schemas import PersonRead, PersonSummary
from cmms.domain.contacts.service import ContactsError, ContactsService
from cmms.domain.exports.service import ExportService
from cmms.domain.feedback.service import FeedbackError, FeedbackService
from cmms.domain.identity.models import UserAccount
from cmms.domain.identity.service import (
    AuthenticationError,
    AuthorizationError,
    IdentityError,
    IdentityService,
)
from cmms.domain.identity.vault import (
    CredentialVault,
    VaultError,
    VaultKeyInvalid,
    VaultKeyUnset,
)
from cmms.domain.inventory.models import InventoryItem
from cmms.domain.inventory.service import InventoryError, InventoryService
from cmms.domain.jira_sync.service import JiraSyncError, JiraSyncService
from cmms.domain.pm_schedule.service import PmScheduleService
from cmms.domain.pm_schedule.transform import effective_generation_date
from cmms.domain.procurement.service import ProcurementError, ProcurementService
from cmms.domain.task.service import TaskService
from cmms.domain.telegram_bridge.service import (
    TelegramBridgeError,
    TelegramBridgeService,
)
from cmms.domain.work_order.service import (
    ALLOWED_TRANSITIONS,
    BACKFILL_ACTOR,
    TERMINAL_STATUSES,
    WorkOrderError,
    WorkOrderService,
)
from cmms.domain.work_order.transform import TAIPEI
from cmms.email import EmailError, get_email_sender, smtp_configured
from cmms.web.assistant_render import render_reply
from cmms.web.export import (
    DATASETS,
    ExportFilterError,
    csv_filename,
    display_cell,
    stream_csv,
    visible_columns,
)
from cmms.web.help_docs import HELP_DOCS, get_help_doc
from cmms.web.i18n import (
    DEFAULT_LOCALE,
    LOCALE_LABELS,
    SUPPORTED_LOCALES,
    negotiate_locale,
    note_type_css,
    status_css,
    translate,
    wo_status_key,
    wtype_css,
)

_WEB_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
templates.env.globals["status_css"] = status_css
templates.env.globals["wo_status_key"] = wo_status_key
templates.env.globals["note_type_css"] = note_type_css
templates.env.globals["wtype_css"] = wtype_css
# 助理回覆安全渲染器(ADR-020):已在渲染器內 escape,模板以 |safe 輸出(見 assistant_render)。
templates.env.globals["render_reply"] = render_reply
# 匯出預覽表格的顯示格式化(HTML;Jinja autoescape 處理跳脫,故無需 formula guard)。
templates.env.globals["export_cell"] = display_cell


def _pm_due_by(due: date | None, today: date) -> bool:
    """PM 是否「已到期(含週末提前)」——供模板決定是否顯示「補開工單」鈕(#5e)。

    週末到期以其前的週五為有效日(effective_generation_date);無到期日 → 永不到期。
    """
    return due is not None and effective_generation_date(due) <= today


templates.env.globals["pm_due_by"] = _pm_due_by

# 設備分類 chips(#4d;asset_type 資料值,照原樣顯示為 chip 標籤,不 i18n 翻碼)。
# Production 預設優先;「全部」= 不過濾(atype=all)。
_EQ_TYPES: tuple[str, ...] = ("Production", "Support", "Jig", "Meter", "Computer")
_EQ_TYPE_DEFAULT = "Production"

# 工單分類標籤 → 狀態群組(canonical + legacy O/H;all=不過濾)。UI chip 與此對映。
# Jordan 2026-07-05 #3d:「我的 = 活單」→ 預設 active(OPEN/IN_PROGRESS/ON_HOLD);
# 另分「已結」(done)與「已取消」(cancelled)兩終態群組。
_WO_STATUS_TABS: dict[str, list[str]] = {
    "active": ["OPEN", "O", "IN_PROGRESS", "ON_HOLD"],
    "inprogress": ["OPEN", "O", "IN_PROGRESS"],
    "waiting": ["ON_HOLD"],
    "done": ["COMPLETED", "CLOSED", "H"],
    "cancelled": ["CANCELLED", "VOIDED"],
}
_WO_DEFAULT_TAB = "active"  # 開場即活單(取代舊「全部」不過濾預設)

router = APIRouter(prefix="/app", tags=["web"])

_LOCALE_COOKIE = "cmms_locale"
_SESSION_COOKIE = "cmms_session"
_LOGIN = "/app/login"
_HOME = "/app/work-orders"
_SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 天(對齊 IdentityService.SESSION_TTL_DAYS)

# 清單分頁(取代固定筆數上限):每頁筆數(WO 25 / 其餘 50,沿用各頁原本預設)。
# 成本取向:不對 21.5k 列跑 COUNT(*),改「多撈 1 筆」偵測是否還有下一頁(見 _paginate)。
_PER_PAGE_WO = 25
_PER_PAGE = 50

_logger = logging.getLogger("cmms.web")

# ADR-020 dock → Hermes gateway 助理:對話落 DB(assistant 切片;跨頁持久 + 多 session)。
# 送給 gateway 的歷史仍硬性裁切(對抗過長 payload / prompt 膨脹):最近 N 輪 + 總字元上限。
_ASSISTANT_MAX_TURNS = 8
_ASSISTANT_MAX_CHARS = 8000
# gateway 端 codex exec 可能久(工具往返);須大於 gateway 內部逾時
# (HERMES_CODEX_TIMEOUT=180),單次不重試。
_ASSISTANT_TIMEOUT_SECONDS = 200.0
# 當前對話 cookie(整頁導覽後 dock 自動還原 = 修「換頁對話全滅」重大缺失)。
_ASSISTANT_CONV_COOKIE = "cmms_assistant_conv"


def _cap_history(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """裁切送 gateway 的歷史:最近 8 輪 + 自最新端往回累計總字元 ≤ 8000(舊的先丟)。

    單筆 content 亦套上限截斷(避免單筆過長漏過總量守門)。輸入來自 DB(已是 [{role, content}]),
    故無需容錯解析;僅做量的裁切。
    """
    capped = [
        {"role": t["role"], "content": t["content"][:_ASSISTANT_MAX_CHARS]}
        for t in turns
        if t.get("role") in ("user", "assistant") and isinstance(t.get("content"), str)
    ][-_ASSISTANT_MAX_TURNS:]
    total = 0
    kept: list[dict[str, str]] = []
    for turn in reversed(capped):  # 自最新往回,超過字元上限即停(丟最舊)
        total += len(turn["content"])
        if total > _ASSISTANT_MAX_CHARS and kept:
            break
        kept.append(turn)
    kept.reverse()
    return kept


def _paginate(rows: list, per_page: int) -> tuple[list, bool]:
    """rows 以 `limit=per_page+1` 撈取:多出的那筆代表還有下一頁。

    回 (裁剪成 per_page 筆的清單, has_next)。探測用的第 per_page+1 筆不渲染。
    """
    has_next = len(rows) > per_page
    return rows[:per_page], has_next


def _pager_base(path: str, params: dict[str, str | None]) -> str:
    """組分頁列的基底 URL:保留除 page 外的當前查詢參數(q/tab/scope/low),故過濾與分頁可疊。

    回不含 page 的前綴('/app/x?k=v&' 或 '/app/x?'),呼叫端模板接 `page=N`。
    空值參數略過(維持乾淨 URL)。
    """
    kept = {k: v for k, v in params.items() if v}
    qs = urlencode(kept)
    return f"{path}?{qs}&" if qs else f"{path}?"


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> UserAccount | None:
    """由 session cookie 解析當前使用者(無效 / 未登入回 None)。"""
    return await IdentityService(session).resolve_user(request.cookies.get(_SESSION_COOKIE))


def _today() -> date:
    """廠區當地(台北)今天。伺服器跑 UTC,直接 date.today() 會在台灣清晨慢一天(差 8h)。"""
    return datetime.now(TAIPEI).date()


async def _generated_this_cycle(wo_svc: WorkOrderService, items: list) -> set[str]:
    """本期已生成 PM 工單的 pm_id 集合(#5e)。

    「本期已生成」= `last_work_order_no` 指向的工單仍非終態(與 _generate_pm_impl 冪等判定一致:
    已結案 → 下個週期會開新單,故視為本期未生成)。供模板決定是否顯示「補開工單」鈕。
    只對有 last_work_order_no 者查單筆,N 小(到期清單 ≤200)。
    """
    generated: set[str] = set()
    for pm in items:
        no = getattr(pm, "last_work_order_no", None)
        if no is None:
            continue
        wo = await wo_svc.get_work_order(no)
        if wo is not None and wo.status not in TERMINAL_STATUSES:
            generated.add(pm.pm_id)
    return generated


def _guest_locale(request: Request) -> str:
    return negotiate_locale(
        request.cookies.get(_LOCALE_COOKIE), request.headers.get("accept-language")
    )


def _is_htmx(request: Request) -> bool:
    """HTMX 局部請求(即時過濾)→ 只渲染結果 partial;整頁載入照常。"""
    return request.headers.get("hx-request") == "true"


def _render(
    request: Request, name: str, locale: str, *, active: str = "",
    user: UserAccount | None = None, **extra: Any,
) -> HTMLResponse:
    """組 base context(t / locale / nav / 使用者)並渲染。t 綁定當前 locale。"""
    ctx: dict[str, Any] = {
        "t": lambda key: translate(key, locale),
        "locale": locale,
        "locales": SUPPORTED_LOCALES,
        "locale_labels": LOCALE_LABELS,
        "active": active,
        "user_initial": (user.display_name[:1].upper() if user else None),
        "user_name": (user.display_name if user else None),
        "is_admin": (user.role == "admin" if user else False),
        # operator = iPad 產線共用帳號(只開報修 + 取消自己誤報)。templates 據此隱藏所有其他
        # 寫入 UI(狀態 chips / 指派 / 領料 / MRQ / 加日誌 / 作廢…);domain 縱深再擋。
        "is_operator": (user.role == "operator" if user else False),
        # ADR-020:dock / FAB 助理是否已配置(Hermes gateway url+secret 皆設)。
        # 未配置 → 前端顯示「尚未啟用」誠實狀態、灰態輸入框(不假裝有 agent)。
        "assistant_enabled": get_settings().hermes_configured,
        **extra,
    }
    return templates.TemplateResponse(request, name, ctx)


async def _upload_photos(
    session: AsyncSession,
    owner_type: str,
    owner_id: str,
    files: list[UploadFile],
    actor: Actor,
) -> int:
    """把上傳照片掛到任一 attachment owner(work_order_note / inventory_item …)。回成功數。"""
    svc = AttachmentService(session)
    n = 0
    for f in files:
        if not f.filename:
            continue
        data = await f.read()
        if not data:
            continue
        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "jpg"
        await svc.add_attachment(
            owner_type=owner_type,
            owner_id=owner_id,
            data=data,
            ext=ext,
            content_type=f.content_type or content_type_for(ext),
            actor=actor,
            original_filename=f.filename,
        )
        n += 1
    return n


async def _upload_note_photos(
    session: AsyncSession, note_id: int, files: list[UploadFile], actor: Actor
) -> int:
    """把上傳照片掛到 work_order_note(owner_type='work_order_note',§1.6)。回成功數;空檔跳過。"""
    return await _upload_photos(session, "work_order_note", str(note_id), files, actor)


async def _flush_jira_outbox_bg(actor_user_id: str) -> None:
    """背景 flush jira_outbox(ADR-020 決策 1 修訂):新增工作紀錄後立即把 note→MRQ comment 送出。

    獨立 session(請求 session 已關)、只 log 失敗(絕不讓背景任務影響已完成的頁面回應)。未配置
    jira → 直接略過(不開 session)。actor = 加註者(稽核 outbox 狀態更新的 who)。
    """
    if not get_settings().jira_forwarder_configured:
        return
    from cmms.db import get_sessionmaker
    from cmms.domain.jira_sync.service import JiraSyncService

    try:
        async with get_sessionmaker()() as session:
            await JiraSyncService(session).flush_outbox(actor=Actor.human(actor_user_id))
    except Exception as exc:  # 背景任務:誠實 log,不外洩、不中斷
        _logger.warning("jira outbox flush failed: %s: %s", type(exc).__name__, exc)


def _notify_channels_configured() -> bool:
    """任一通知通道(email / telegram)是否可送 —— 決定背景 flush 是否值得開 session(cheap guard)。"""
    s = get_settings()
    from cmms.email import smtp_configured
    from cmms.telegram import telegram_configured

    email = smtp_configured() and bool(s.notify_from or s.rfq_from)
    return email or telegram_configured()


async def _flush_notify_outbox_bg() -> None:
    """背景 flush notification_outbox(Slice B):工單開立/結案後立即送 email / telegram 通知。

    獨立 session(請求 session 已關)、只 log 失敗(絕不影響已完成的頁面回應)。無任何通道配置
    → 直接略過(不開 session;outbox 列留 pending,配置後由 CLI / 下次 flush 補送)。actor =
    scheduler(系統背景送出,稽核 outbox 狀態更新的 who)。
    """
    if not _notify_channels_configured():
        return
    from cmms.db import get_sessionmaker
    from cmms.domain.notify.service import NotificationService

    try:
        async with get_sessionmaker()() as session:
            await NotificationService(session).flush_outbox(actor=Actor.scheduler())
    except Exception as exc:  # 背景任務:誠實 log,不外洩、不中斷
        _logger.warning("notify outbox flush failed: %s: %s", type(exc).__name__, exc)


# ---- 認證(ADR-022)----

@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_form(
    request: Request, user: UserAccount | None = Depends(get_current_user)
) -> HTMLResponse | RedirectResponse:
    if user is not None:
        return RedirectResponse(url=_HOME, status_code=307)
    return _render(request, "login.html", _guest_locale(request), error=False)


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    session: AsyncSession = Depends(get_session),
    username: str = Form(...),
    password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    try:
        _uid, token = await IdentityService(session).authenticate(username, password)
    except AuthenticationError:
        return _render(request, "login.html", _guest_locale(request), error=True)
    resp = RedirectResponse(url=_HOME, status_code=303)
    resp.set_cookie(
        _SESSION_COOKIE, token,
        max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax", path="/",
    )
    return resp


@router.get("/logout")
@router.post("/logout")
async def logout(
    request: Request, session: AsyncSession = Depends(get_session)
) -> RedirectResponse:
    await IdentityService(session).logout(request.cookies.get(_SESSION_COOKIE))
    resp = RedirectResponse(url=_LOGIN, status_code=303)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


# ---- 操作台(需登入)----

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def home(user: UserAccount | None = Depends(get_current_user)) -> RedirectResponse:
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    return RedirectResponse(url=_HOME, status_code=307)


@router.get("/work-orders", response_class=HTMLResponse, response_model=None)
async def work_order_queue(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    page: int = Query(1, ge=1),
    scope: str = Query("mine"),
    tab: str = Query(_WO_DEFAULT_TAB),
    q: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """工單佇列(讀取,真資料)。`scope` = mine(指派給我)/ all;`tab` = 狀態群組;`q` = 綜合查詢。

    「我的」= assigned_person == 登入者 emaint_assignee(Slice 2)。預設 tab=active(活單)。
    無指派名(監督者)→ Mine 空,提示切 All。`page` = 分頁(1-based,取代固定筆數上限)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    active_tab = tab if tab in _WO_STATUS_TABS or tab == "all" else _WO_DEFAULT_TAB
    mine = scope != "all"  # 預設 mine;僅明確 all 才全部
    assignee = user.emaint_assignee if mine else None
    if mine and not assignee:
        work_orders: list = []  # 監督者無指派名 → Mine 空(模板提示切 All)
        has_next = False
    else:
        rows = await WorkOrderService(session).list_work_orders(
            limit=_PER_PAGE_WO + 1, offset=(page - 1) * _PER_PAGE_WO,
            statuses=_WO_STATUS_TABS.get(active_tab), search=q, assigned_person=assignee,
        )
        work_orders, has_next = _paginate(rows, _PER_PAGE_WO)
    # 清單卡標機台名(工單只帶 EID)→ 批次取資產敘述,一查免 N+1(Jordan 2026-07-07)
    asset_names = await AssetService(session).descriptions_map(
        [wo.asset_id for wo in work_orders]
    )
    # 0031:清單卡負責人顯示全部(批次取 work_order_assignee,免 N+1)
    assignee_names = await WorkOrderService(session).assignees_map(
        [wo.work_order_no for wo in work_orders]
    )
    scope_str = "mine" if mine else "all"
    pager_base = _pager_base("/app/work-orders", {"scope": scope_str, "tab": active_tab, "q": q})
    # 即時過濾(HTMX):輸入即查 → 只回結果區塊,不重載整頁
    name = "partials/wo_list.html" if _is_htmx(request) else "work_orders.html"
    return _render(
        request, name, user.ui_locale, active="orders",
        user=user, work_orders=work_orders, tab=active_tab, q=q,
        scope=scope_str, has_assignee=bool(user.emaint_assignee),
        page=page, has_next=has_next, pager_base=pager_base,
        asset_names=asset_names, assignee_names=assignee_names,
    )


@router.get("/work-orders/{work_order_no}", response_class=HTMLResponse, response_model=None)
async def work_order_detail(
    request: Request,
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    msg: str | None = Query(None),
    mrq: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """工單詳情 · 時間線(work_order_note,§1.6)+ 領料 + MRQ 連結 + 指派/取消/作廢操作。

    `msg` = 操作結果 banner(part_ok/part_dup/part_err/link_ok/link_err/voidreq/...);
    `mrq` = 轉發成功時附帶的 MRQ key(banner 顯示,如 forward_ok/forward_exists)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    svc = WorkOrderService(session)
    wo = await svc.get_work_order(work_order_no)
    if wo is None:
        return _render(
            request, "placeholder.html", user.ui_locale, active="orders",
            user=user, screen_title=f"WO-{work_order_no}",
        )
    notes = await svc.list_notes(work_order_no)
    # 每筆 note 的照片(owner_type='work_order_note')→ 批次一查免 N+1(詳情頁是最熱頁面)
    att_svc = AttachmentService(session)
    atts_map = await att_svc.attachments_map(
        "work_order_note", [str(n.id) for n in notes]
    )
    photos_by_note: dict[int, list[tuple[int, str]]] = {
        n.id: [(a.id, att_svc.presigned_url(a)[0]) for a in atts_map[str(n.id)]]
        for n in notes
        if atts_map.get(str(n.id))
    }
    parts = await svc.get_parts(work_order_no)  # 工單領料史(ui-mvp-spec §3)
    links = await svc.list_external_links(work_order_no)  # WO↔MRQ(ADR-020;落庫連結事實)
    # 是否已有待審的作廢請求(targeted 查詢;避免重複提案 + 顯示審核中)
    pending_void = (
        await svc.find_pending_proposal(
            operation="void_work_order", work_order_no=work_order_no
        )
        is not None
    )
    terminal = wo.status in TERMINAL_STATUSES
    voidable = "VOIDED" in ALLOWED_TRANSITIONS.get(wo.status, set())
    # 受控詞彙由 DB 供給(單一來源,0019 pattern)。工程師 chips 面隱藏「其他」(#2,
    # Jordan 2026-07-05):等待說明恆選填;DB 受控詞彙 / /admin/vocab / 歷史資料不動。
    hold_reasons = [
        r for r in await svc.list_hold_reasons() if r.code != "OTHER"
    ]
    # D6 確認故障真因(efc 軸)下拉候選:僅 REACTIVE 工單需要(PM 發現故障應另開報修);
    # 本設備用過的碼優先、其餘 active efc 字母序在後(見 list_confirmed_reason_options)。
    # 附 descr 人讀說明供多關鍵字搜尋;模板序列化成 JSON 給 combobox(顯示人話、存 code)。
    efc_opts = (
        await svc.list_confirmed_reason_options(wo.asset_id)
        if wo.work_type == "REACTIVE"
        else []
    )
    efc_options = [{"c": o.code, "d": o.descr, "u": o.used} for o in efc_opts]
    # 標頭機台名:工單只帶 EID,單看 EID 看不出是哪台機器 → 附資產敘述(Jordan 2026-07-07)。
    asset = await AssetService(session).get_asset(wo.asset_id)
    asset_desc = asset.description if asset else None
    assignees = await svc.get_assignees(work_order_no)  # 0031 全部負責人(顯示 + 改派預填)
    return _render(
        request, "work_order_detail.html", user.ui_locale, active="orders",
        user=user, wo=wo, notes=notes, photos_by_note=photos_by_note, assignees=assignees,
        parts=parts, links=links, pending_void=pending_void, terminal=terminal,
        voidable=voidable, hold_reasons=hold_reasons, efc_options=efc_options,
        asset_desc=asset_desc,
        backfill_source_actor=BACKFILL_ACTOR.value,
        msg=msg, mrq=mrq, me=Actor.human(user.user_id).value, nonce=secrets.token_urlsafe(8),
        back_url="/app/work-orders",
    )


# ---- 一鍵轉發工單→Jira MRQ(ADR-020 決策 1 修訂;純編碼路徑,非 agent)----

def _parse_forward_wos(primary: int, wos_raw: str | None) -> list[int]:
    """合併「當前工單 + 同批其他工單號」→ 去重保序(primary 永遠第一)。

    容錯解析逗號/分號分隔字串,接受 `WO-123` / `123` 兩種寫法;非數字 token 略過。
    """
    nos = [primary]
    for tok in (wos_raw or "").replace(";", ",").split(","):
        cleaned = tok.strip().upper().removeprefix("WO-").removeprefix("WO").strip()
        if cleaned.isdigit():
            n = int(cleaned)
            if n not in nos:
                nos.append(n)
    return nos


# UI 語言碼 → 明確語言名(給 LLM 讀,避免把語言碼誤讀為其他東西)。
_UI_LOCALE_LANGUAGE = {
    "en": "English",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "vi": "Vietnamese (Tiếng Việt)",
}


def _forward_line(eid: str, name: str, brief: str | None) -> str:
    """一列摘要基底:`EID-xxxxx <設備名> — <brief>`(name/brief 缺則自然收合,不留空 dash)。"""
    head = f"{eid} {name}".strip()
    desc = (brief or "").strip()
    return f"{head} — {desc}" if desc else head


def _forward_summary(details: list[dict]) -> str:
    """summary 預填:單 WO = 一列;多 WO = 各列以 ` / ` 相接;合理截長 ~250。

    硬規則(persona 規範,受眾=別單位/老闆):不含工單號、不含 CMMS/同步機制字眼。
    """
    parts = [_forward_line(d["eid"], d["name"], d["wo"].brief_description) for d in details]
    return " / ".join(p for p in parts if p)[:250].rstrip()


# AI 總結取樣上限(送 gateway 前裁切,防 prompt 膨脹 / 逾時)。
_AI_DESC_MAX_NOTES_PER_WO = 30    # 每工單最近 N 筆
_AI_DESC_MAX_NOTE_CHARS = 1000    # 單筆 body 截長
_AI_DESC_MAX_TOTAL_CHARS = 10000  # 全部 WO 合計 body 上限(超過自最舊丟起)


async def _collect_forward_notes(
    wo_svc: WorkOrderService, asset_svc: AssetService, nos: list[int]
) -> tuple[list[dict], bool]:
    """為 AI 總結收集各存在工單的 notes payload。回 (work_orders, truncated)。

    每工單取最近 _AI_DESC_MAX_NOTES_PER_WO 筆;單筆 body 截 _AI_DESC_MAX_NOTE_CHARS;
    全部合計 body ≤ _AI_DESC_MAX_TOTAL_CHARS(自最舊丟起)。任一裁切 → truncated=True。
    不存在的工單號略過(找不到 → 不入 payload)。
    """
    truncated = False
    wos_meta: list[dict] = []  # 各工單身分 {eid, name, brief}
    flat: list[dict] = []      # 攤平 notes(依工單序、單工單內時間升冪);total-cap 用
    for n in nos:
        wo = await wo_svc.get_work_order(n)
        if wo is None:
            continue
        asset = await asset_svc.get_asset(wo.asset_id)
        wo_idx = len(wos_meta)
        wos_meta.append(
            {"eid": wo.asset_id, "name": asset.description if asset else "",
             "brief": wo.brief_description}
        )
        notes = await wo_svc.list_notes(n)  # 時間線升冪、已排除軟刪
        if len(notes) > _AI_DESC_MAX_NOTES_PER_WO:
            notes = notes[-_AI_DESC_MAX_NOTES_PER_WO:]
            truncated = True
        for note in notes:
            body = note.body or ""
            if len(body) > _AI_DESC_MAX_NOTE_CHARS:
                body = body[:_AI_DESC_MAX_NOTE_CHARS]
                truncated = True
            flat.append({
                "wo_idx": wo_idx,
                "at": note.occurred_at.strftime("%Y-%m-%d %H:%M") if note.occurred_at else "",
                "author": note.author or "",
                "body": body,
            })
    if not wos_meta:
        return [], truncated
    # 合計 body 上限:自最舊(flat 前端)丟起
    total = sum(len(f["body"]) for f in flat)
    while total > _AI_DESC_MAX_TOTAL_CHARS and flat:
        total -= len(flat.pop(0)["body"])
        truncated = True
    by_wo: dict[int, list[dict]] = {i: [] for i in range(len(wos_meta))}
    for f in flat:
        by_wo[f["wo_idx"]].append({"at": f["at"], "author": f["author"], "body": f["body"]})
    work_orders = [
        {**meta, "notes": by_wo[i]} for i, meta in enumerate(wos_meta)
    ]
    return work_orders, truncated


async def _render_forward_form(
    request: Request,
    session: AsyncSession,
    user: UserAccount,
    work_order_no: int,
    wos_raw: str | None,
    *,
    summary: str | None = None,
    description: str | None = None,
    idem_key: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """組轉發表單 context 並渲染(GET 首載 + POST 錯誤回填共用)。

    `summary`/`description`=None → 依工單內容確定性生成模板(不走 LLM);非 None → 沿用送出值回填。
    dry-run 預覽:per-WO note/photo 數 + readiness + warnings;readiness 誠實控制送出禁用。
    """
    nos_req = _parse_forward_wos(work_order_no, wos_raw)
    wo_svc = WorkOrderService(session)
    asset_svc = AssetService(session)
    details: list[dict] = []
    missing: list[int] = []
    forwarded: dict[int, list[str]] = {}
    for n in nos_req:
        wo = await wo_svc.get_work_order(n)
        if wo is None:
            missing.append(n)
            continue
        asset = await asset_svc.get_asset(wo.asset_id)
        details.append({"wo": wo, "eid": wo.asset_id, "name": asset.description if asset else ""})
        keys = [
            link.external_key
            for link in await wo_svc.list_external_links(n)
            if link.link_type == "forwarded"
        ]
        if keys:
            forwarded[n] = keys
    valid_nos = [d["wo"].work_order_no for d in details]
    if summary is None:
        summary = _forward_summary(details)
    if description is None:
        # 反饋 1/2:description 不再帶 opened/status 模板、不重述摘要 —— 預設留空,
        # 由「AI 總結工作紀錄」按鈕生成,或使用者自行填寫。
        description = ""

    preview = None
    if valid_nos:
        try:
            preview = await JiraSyncService(session).forward_work_orders_to_mrq(
                work_order_nos=valid_nos, summary=summary, description=description,
                acting_user=user.user_id, actor=Actor.human(user.user_id), dry_run=True,
            )
        except JiraSyncError:
            preview = None  # 空 summary/description 等 → 無預覽,readiness 另算(下)
    if preview is not None:
        config_ready, pat_ready, warnings = (
            preview.config_ready, preview.pat_ready, preview.warnings
        )
    else:
        config_ready = get_settings().jira_forwarder_configured
        pat_ready = await JiraSyncService(session).pat_ready(user.user_id)
        warnings = []
    counts = {w.work_order_no: w for w in preview.work_orders} if preview else {}
    return _render(
        request, "work_order_forward.html", user.ui_locale, active="orders", user=user,
        work_order_no=work_order_no, wos_raw=(wos_raw or ""), details=details, missing=missing,
        forwarded=forwarded, preview=preview, counts=counts,
        summary=summary, description=description, ai_error=None,
        config_ready=config_ready, pat_ready=pat_ready, ready=(config_ready and pat_ready),
        warnings=warnings, idem_key=idem_key or uuid.uuid4().hex, error=error,
        back_url=f"/app/work-orders/{work_order_no}",
    )


@router.get("/work-orders/{work_order_no}/forward", response_class=HTMLResponse,
            response_model=None)
async def work_order_forward_form(
    request: Request,
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    wos: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """轉發表單頁(需登入;admin+engineer 皆可)。`?wos=123,456` = 同批其他工單號(選填)。

    確定性模板預填 summary/description(不走 LLM)+ dry-run 預覽(note/照片數 + readiness)。
    無效工單號 → 誠實 banner(不 500);config/PAT 未備 → 警語 + 送出禁用。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無 MRQ 轉發權(表單在 detail 已隱藏)
        return RedirectResponse(url=f"/app/work-orders/{work_order_no}", status_code=303)
    return await _render_forward_form(request, session, user, work_order_no, wos)


@router.post("/work-orders/{work_order_no}/forward", response_model=None)
async def work_order_forward_submit(
    request: Request,
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    summary: str = Form(""),
    description: str = Form(""),
    wos: str = Form(""),
    idem_key: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """送出轉發(dry_run=False):建 MRQ → 記 forwarded link → 排 outbox → 立即 flush。

    空 summary/description → 回表單帶錯誤。`idem_key` 防重(雙擊/重送 → domain 復用既有 MRQ、
    不重開)。JiraSyncError/VaultError → 回表單帶**安全**錯誤 banner(PAT 明文與例外細節只進 log)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無 MRQ 轉發權
        return RedirectResponse(url=f"/app/work-orders/{work_order_no}", status_code=303)
    s, d = summary.strip(), description.strip()
    if not s or not d:
        return await _render_forward_form(
            request, session, user, work_order_no, wos,
            summary=summary, description=description, idem_key=idem_key or None, error="empty",
        )
    nos = _parse_forward_wos(work_order_no, wos)
    try:
        result = await JiraSyncService(session).forward_work_orders_to_mrq(
            work_order_nos=nos, summary=s, description=d,
            acting_user=user.user_id, actor=Actor.human(user.user_id),
            dry_run=False, idempotency_key=idem_key or None,
        )
    except (JiraSyncError, VaultError) as exc:
        # 完整例外只進 log(可能含 Jira 端細節);頁面只顯示分類後的安全訊息
        _logger.warning("forward to MRQ failed (wo=%s): %s: %s",
                        work_order_no, type(exc).__name__, exc)
        reason = str(exc).lower()
        if "pat" in reason or isinstance(exc, VaultError) or "master key" in reason:
            err = "pat"
        elif "configured" in reason or "base_url" in reason or "project" in reason:
            err = "config"
        else:
            err = "jira"
        return await _render_forward_form(
            request, session, user, work_order_no, wos,
            summary=summary, description=description, idem_key=idem_key or None, error=err,
        )
    msg = "forward_exists" if result.already_forwarded else "forward_ok"
    return RedirectResponse(
        url=f"/app/work-orders/{work_order_no}?msg={msg}&mrq={result.external_key}",
        status_code=303,
    )


@router.post("/work-orders/{work_order_no}/forward/ai-description",
             response_class=HTMLResponse, response_model=None)
async def work_order_forward_ai_description(
    request: Request,
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    wos: str = Form(""),
    description: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """「AI 總結工作紀錄」(反饋 3):把各工單 work-log 送 Hermes gateway 總結成 description。

    HTMX 以 outerHTML 換掉 `#fwd-desc-wrap`。成功 → textarea 填生成文字;
    失敗/逾時/空回覆/未配置 → 保留使用者原文(`description`)+ 誠實錯誤 hint(不假造)。
    治理:gateway 只吃事實 payload(工單/notes 由此路徑先讀好),無 scoped token、不打 MCP。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無 MRQ 轉發權
        return RedirectResponse(url=f"/app/work-orders/{work_order_no}", status_code=303)
    settings = get_settings()

    def _partial(*, description: str, error: str | None = None) -> HTMLResponse:
        return _render(
            request, "partials/forward_description.html", user.ui_locale, user=user,
            work_order_no=work_order_no, wos_raw=wos, description=description, ai_error=error,
        )

    if not settings.hermes_configured:  # 防禦:按鈕不該渲染,但仍誠實回應
        return _partial(description=description, error="disabled")

    nos = _parse_forward_wos(work_order_no, wos)
    payload_wos, truncated = await _collect_forward_notes(
        WorkOrderService(session), AssetService(session), nos
    )
    if not payload_wos:  # 全部工單號查無 → 無可總結
        return _partial(description=description, error="unavailable")

    payload = {
        "work_orders": payload_wos,
        # Jordan 裁決 2026-07-07:轉發表單的 AI 摘要跟隨 UI 語言(使用者在自己的語言下
        # 檢視/編輯後才送出),此端點覆蓋 jira_output_locale;dock 助理線與 MCP 線不變。
        # 欄位名維持 jira_locale(hermes gateway 契約不動、不需 redeploy)。
        "jira_locale": _UI_LOCALE_LANGUAGE.get(user.ui_locale, "English"),
        "truncated": truncated,
    }
    try:
        async with httpx.AsyncClient(timeout=_ASSISTANT_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                settings.hermes_gateway_url.rstrip("/") + "/mrq-description",
                headers={"X-Hermes-Secret": settings.hermes_gateway_secret},
                json=payload,
            )
        resp.raise_for_status()
        generated = str((resp.json() or {}).get("description") or "").strip()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        _logger.warning("MRQ description generation failed (wo=%s): %s: %s",
                        work_order_no, type(exc).__name__, exc)
        return _partial(description=description, error="unavailable")
    if not generated:  # gateway 回空 → 暫時無法產生(保留原文、可重試)
        return _partial(description=description, error="unavailable")
    return _partial(description=generated)


@router.get("/report", response_class=HTMLResponse, response_model=None)
async def report_form(
    request: Request,
    user: UserAccount | None = Depends(get_current_user),
    eid: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """報修開單表單(screen ①)。`?eid=` 由 QR 掃描帶入(降摩擦)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    return _render(request, "report.html", user.ui_locale, active="report",
                   user=user, eid=eid, brief=None, owner_val=[], error=False)


@router.post("/report", response_model=None)
async def report_submit(
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    asset_id: str = Form(...),
    brief_description: str = Form(""),
    assignees: list[str] = Form(default=[]),
    photos: list[UploadFile] = File(default=[]),
) -> HTMLResponse | RedirectResponse:
    """開 REACTIVE 工單 + 初始報修 note(+照片),actor = 登入者(拒匿名)。

    `brief_description` **選填**(Jordan 2026-07-06:現場作業員第一時間開單常不填故障內容,
    由負責工程師事後於詳情頁補填):留空 → 工單 brief=None;有照片 → 仍建初始 report note
    掛照片(純照片紀錄,body 空字串);無照片且無簡述 → 只開單、不建 note。
    `assignees`(0031 多負責人)可多值:開單即指派全部負責人(留空 → 帶入設備負責人清單)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.strip().upper()
    asset = await session.get(Asset, eid)
    submitted = [a.strip() for a in assignees if a and a.strip()]
    if asset is None:  # EID 必須存在 asset 主檔(不靜默建殘缺)
        return _render(request, "report.html", user.ui_locale, active="report",
                       user=user, eid=asset_id, brief=brief_description,
                       owner_val=submitted, error="notfound")
    # 0031:負責人必填(低摩擦)——表單值優先,留空則帶入設備負責人清單(asset_owner);皆空 →
    # 友善錯誤重顯(保留已填值)。JS 端在選定 EID 時自動帶入負責人,此為無 JS / 空負責人的伺服端網。
    resolved = submitted or await AssetService(session).get_owners(eid)
    if not resolved:
        return _render(request, "report.html", user.ui_locale, active="report",
                       user=user, eid=asset_id, brief=brief_description,
                       owner_val=submitted, error="owner_missing")
    actor = Actor.human(user.user_id)
    wo_svc = WorkOrderService(session)
    brief = brief_description.strip()
    try:
        # 退役資產(is_active=false)開單被 domain 守門拒 → 友善 banner(#4b;非 500)
        wo = await wo_svc.open_work_order(
            asset_id=eid, work_type="REACTIVE", actor=actor,
            brief_description=brief or None, opened_by=user.user_id,
            assignees=resolved,
        )
    except WorkOrderError:
        return _render(request, "report.html", user.ui_locale, active="report",
                       user=user, eid=asset_id, brief=brief_description,
                       owner_val=submitted, error="retired")
    # Slice B:開單通知已於 open_work_order 同交易 enqueue → 立即背景 flush(送 email/telegram)。
    background.add_task(_flush_notify_outbox_bg)
    # 初始報修 = 第一筆 work_order_note(照片掛此筆 → 進時間線 + 對 Jira comment,§1.6)。
    # 簡述留空 + 無照片 → 不建空 note(工單已足);有任一 → 建(純照片時 body="" 由模板優雅渲染)。
    has_photo = any(getattr(f, "filename", None) for f in photos)
    if brief or has_photo:
        note = await wo_svc.add_note(
            wo.work_order_no, entry_type="report", body=brief, actor=actor
        )
        try:
            await _upload_note_photos(session, note.id, photos, actor)
        except Exception:  # 工單/報修文字已落庫;照片(R2)失敗只提示,不變成 500
            return _wo_redirect(wo.work_order_no, "photo_err")
    return RedirectResponse(url=f"/app/work-orders/{wo.work_order_no}", status_code=303)


@router.get("/asset-owner", response_model=None)
async def asset_owner(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    eid: str = Query(""),
) -> JSONResponse:
    """設備負責人查詢(0031;報修表單自動帶入 assignee 用)。回 {"owners": [<名字…>]}。

    登入必需(同其他 /app 路由);EID 查無 / 未設負責人 → owners=[]。純讀取,低風險。
    """
    if user is None:
        return JSONResponse({"owners": []}, status_code=401)
    e = eid.strip().upper()
    if not e:
        return JSONResponse({"owners": []})
    owners = await AssetService(session).get_owners(e)
    return JSONResponse({"owners": owners})


@router.post("/work-orders/{work_order_no}/notes", response_model=None)
async def add_work_order_note(
    work_order_no: int,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    body: str = Form(...),
    photos: list[UploadFile] = File(default=[]),
) -> RedirectResponse:
    """加一筆工作日誌(screen ② 寫入,§1.6)+ 照片,actor = 登入者。

    工單已連 forwarded/appended MRQ → add_note 於同交易 enqueue jira_outbox(domain);此處排一個
    背景任務**立即 flush**(需求 ②:新增工作紀錄即自動同步到 MRQ)。背景失敗只 log,不影響頁面。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    actor = Actor.human(user.user_id)
    wo_svc = WorkOrderService(session)
    try:
        # operator 不得加進度日誌(domain add_note 閘只放行本人 OPEN 單的 report 筆);
        # 表單在 detail 已對 operator 隱藏,此處 except 補 AuthorizationError 防 500。
        note = await wo_svc.add_note(
            work_order_no, entry_type="progress", body=body.strip(), actor=actor
        )
    except (WorkOrderError, AuthorizationError):
        return RedirectResponse(url=_HOME, status_code=303)
    try:
        await _upload_note_photos(session, note.id, photos, actor)
    except Exception:  # 日誌文字已落庫;照片(R2)失敗只提示,不變成 500
        return _wo_redirect(work_order_no, "photo_err")
    background.add_task(_flush_jira_outbox_bg, user.user_id)  # 立即同步到已連結的 MRQ
    return RedirectResponse(url=f"/app/work-orders/{work_order_no}", status_code=303)


@router.post("/work-orders/{work_order_no}/transition", response_model=None)
async def work_order_transition(
    work_order_no: int,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    action: str = Form(...),
    hold_reason: str = Form("WAITING_PARTS"),
    action_taken: str = Form(""),
    confirmed_reason_code: str = Form(""),
) -> RedirectResponse:
    """狀態機操作(screen ②;Jordan 2026-07-05 #3 生命週期簡化),actor = 登入者。

    chip 動作:`progress`(處理中:OPEN→start / ON_HOLD→resume,route 依現態分派 resume_or_start)、
    `hold`(等待:set_hold,任何活單態一鍵切換 + 換原因)、`finish`(結單:COMPLETED→CLOSED 一鍵)、
    `reopen`(COMPLETED→續修)。保留 legacy `start/resume/complete/close`(MCP/測試相容)。
    非法轉移 / 找不到 → 顯性提示(狀態不變)。downtime 由狀態機依區段精算(不手填)。
    等待延誤說明欄已移除(Jordan 2026-07-07:等料/等外包/等機台空檔等切換不需說明;原因 chip 本身
    即資訊,特殊情況工程師仍可在時間線自加一筆日誌)→ web 不再送 hold note,domain `note_body`
    選填參數保留(API/MCP 相容)。結單處置摘要欄亦移除(Jordan 2026-07-07:結單不強制總結,工作
    日誌已交代;姊妹專案皆無綁定需求)→ `finish` 不再要求 action_taken;domain 選填參數保留。
    工時欄已移除(Jordan 2026-07-05);`labor_hours` 選填參數保留(API/MCP 相容),web 層一律不傳。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋(domain 縱深再擋);operator 無狀態機操作權
        return _wo_redirect(work_order_no, "transition_err")
    actor = Actor.human(user.user_id)
    svc = WorkOrderService(session)
    try:
        if action == "progress":  # 一鍵「處理中」:依現態分派 start / resume
            await svc.resume_or_start(work_order_no, actor)
        elif action == "hold":  # 一鍵「等待」:任何活單態直接切換 / 換原因(不收延誤說明)
            await svc.set_hold(work_order_no, hold_reason, actor, note_body=None)
        elif action == "finish":  # 一鍵「結單」:COMPLETED→CLOSED(處置摘要選填,不強制)
            # D6:結單順帶確認故障真因(efc 軸;選填,僅 REACTIVE 表單顯示該欄)。壞碼/非
            # REACTIVE → domain 拒 → transition_err(整筆不結,誠實提示)。
            await svc.finish_work_order(
                work_order_no, actor,
                action_taken=action_taken.strip() or None,
                confirmed_reason_code=confirmed_reason_code.strip() or None,
            )
        elif action in ("resume", "reopen"):  # reopen = COMPLETED→續修
            await svc.resume_work(work_order_no, actor)
        elif action == "start":
            await svc.start_work(work_order_no, actor)
        elif action == "complete":
            await svc.complete_work(
                work_order_no, actor, action_taken=action_taken.strip() or None
            )
        elif action == "close":
            await svc.close_work_order(
                work_order_no, actor, action_taken=action_taken.strip() or None
            )
    except (WorkOrderError, AuthorizationError, ArithmeticError, ValueError):
        return _wo_redirect(work_order_no, "transition_err")  # 非法轉移/找不到/無權 → 顯性提示
    # Slice B:結案(finish/close)通知已於同交易 enqueue → 立即背景 flush(其餘轉移為 no-op:
    # 無 'closed' 列;flush 冪等且 cheap)。
    background.add_task(_flush_notify_outbox_bg)
    return RedirectResponse(url=f"/app/work-orders/{work_order_no}", status_code=303)


def _wo_redirect(work_order_no: int, msg: str | None = None) -> RedirectResponse:
    # 領料相關訊息(part_*)帶 #parts 錨點:領料區在頁中段,錨點讓使用者直接看到結果
    # (比照 #pat 錨點修法,免「訊息在頁頂看不到、體感=被接受」)。
    frag = "#parts" if msg and msg.startswith("part") else ""
    url = f"/app/work-orders/{work_order_no}" + (f"?msg={msg}" if msg else "") + frag
    return RedirectResponse(url=url, status_code=303)


@router.post("/work-orders/{work_order_no}/assign", response_model=None)
async def work_order_assign(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    assignees: list[str] = Form(default=[]),
) -> RedirectResponse:
    """指派/改派負責人清單(0031 多負責人;開立後隨時;空清單 = 清除)。actor = 登入者。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無指派權
        return _wo_redirect(work_order_no, "assign_err")
    try:
        await WorkOrderService(session).set_assignees(
            work_order_no, assignees=assignees, actor=Actor.human(user.user_id)
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "assign_err")
    return _wo_redirect(work_order_no, "assign_ok")


@router.post("/work-orders/{work_order_no}/brief", response_model=None)
async def work_order_edit_brief(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    brief_description: str = Form(""),
) -> RedirectResponse:
    """補填 / 更正故障簡述(Jordan 2026-07-06;作業員留空、工程師事後補)。actor = 登入者。

    非終態 = engineer/admin 皆可(web 層拒匿名);終態單由 domain 守門(限 admin)。空 → None。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無補簡述權
        return _wo_redirect(work_order_no, "brief_err")
    try:
        await WorkOrderService(session).update_brief_description(
            work_order_no,
            brief_description=brief_description.strip() or None,
            actor=Actor.human(user.user_id),
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "brief_err")
    return _wo_redirect(work_order_no, "brief_ok")


@router.post("/work-orders/{work_order_no}/confirmed-reason", response_model=None)
async def work_order_set_confirmed_reason(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    confirmed_reason_code: str = Form(""),
) -> RedirectResponse:
    """補填 / 更正 / 清除確認故障真因(D6;efc 軸)。空 → 清除。actor = 登入者。

    僅 REACTIVE 工單;非終態 = 任何登入者可設,終態(CLOSED/…)限 admin 更正 —— 全由 domain
    守門(route 藏頁不是授權)。壞碼 / 非 REACTIVE / 終態非 admin → reason_err 誠實提示。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    try:
        # operator 無設真因權(domain set_confirmed_reason 閘);表單在 detail 已隱藏,
        # except 補 AuthorizationError 防 500。
        await WorkOrderService(session).set_confirmed_reason(
            work_order_no,
            code=confirmed_reason_code.strip() or None,
            actor=Actor.human(user.user_id),
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "reason_err")
    return _wo_redirect(work_order_no, "reason_ok")


@router.post("/work-orders/{work_order_no}/notes/{note_id}/edit", response_model=None)
async def edit_work_order_note(
    work_order_no: int,
    note_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    body: str = Form(...),
    photos: list[UploadFile] = File(default=[]),
) -> RedirectResponse:
    """更正一筆日誌(限本人;admin 可代改)+ 可補照片。updated_at 記「已編輯」。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 不得更正日誌
        return _wo_redirect(work_order_no, "note_err")
    actor = Actor.human(user.user_id)
    svc = WorkOrderService(session)
    try:
        # 歸屬 / 本人-or-admin / 終態凍結 全在 domain 強制(review f14cf8d)
        await svc.update_note(note_id, body=body, actor=actor, work_order_no=work_order_no)
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "note_err")
    try:
        await _upload_note_photos(session, note_id, photos, actor)
    except Exception:  # 文字更正已落庫;照片上傳(R2)失敗只提示,不變成 500
        return _wo_redirect(work_order_no, "photo_err")
    return _wo_redirect(work_order_no)


@router.post("/work-orders/{work_order_no}/notes/{note_id}/delete", response_model=None)
async def delete_work_order_note(
    work_order_no: int,
    note_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> RedirectResponse:
    """刪一筆工作日誌(Jordan 2026-07-05 #1;軟刪,限本人或 admin;終態單限 admin)。
    照片 attachment 不動(R2 永留),隨 note 一起從時間線消失。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 不得刪日誌
        return _wo_redirect(work_order_no, "note_err")
    try:
        await WorkOrderService(session).delete_note(
            note_id, Actor.human(user.user_id), work_order_no=work_order_no
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "note_err")
    return _wo_redirect(work_order_no, "note_deleted")


@router.post(
    "/work-orders/{work_order_no}/notes/{note_id}/photos/{attachment_id}/delete",
    response_model=None,
)
async def delete_note_photo(
    work_order_no: int,
    note_id: int,
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> RedirectResponse:
    """移除日誌照片(soft delete;限該筆作者或 admin;R2 物件保留供稽核)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 不得刪日誌照片(report 後不可回頭改)
        return _wo_redirect(work_order_no, "note_err")
    svc = WorkOrderService(session)
    note = await svc.get_note(note_id)  # 單筆讀取(免撈整串 notes)
    if note is not None and note.work_order_no != work_order_no:
        note = None
    att_svc = AttachmentService(session)
    att = await att_svc.get_attachment(attachment_id)
    if (
        note is None
        or att is None
        or att.owner_type != "work_order_note"
        or att.owner_id != str(note_id)
        or (note.author != Actor.human(user.user_id).value and user.role != "admin")
    ):
        return _wo_redirect(work_order_no, "note_err")
    await att_svc.soft_delete_attachment(attachment_id, Actor.human(user.user_id))
    return _wo_redirect(work_order_no)


@router.post("/work-orders/{work_order_no}/parts", response_model=None)
async def work_order_issue_part(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    item_code: str = Form(...),
    quantity: str = Form(...),
    nonce: str = Form(...),
) -> RedirectResponse:
    """工單領料(ui-mvp-spec §3):記 work_order_part + 扣 on_hand;終態工單拒領。冪等 nonce。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無領料權
        return _wo_redirect(work_order_no, "part_err")
    code = item_code.strip().upper()
    # 未知料號分流(縱深;比照報修 EID pre-check):查無 → 明確「未領料」訊息 + #parts 錨點,
    # 不落進通用 part_err(否則訊息在頁頂、體感=被接受)。前端 strict 是第一道,這是第二道。
    if await session.get(InventoryItem, code) is None:
        return _wo_redirect(work_order_no, "part_unknown")
    try:
        ok = await WorkOrderService(session).issue_part_to_work_order(
            work_order_no=work_order_no,
            item_code=code,
            quantity=quantity,
            actor=Actor.human(user.user_id),
            # 冪等鍵綁 payload(review f14cf8d:純 nonce 會把「同頁再領不同料」誤吞為重複)
            idempotency_key=f"webwopart:v2:{nonce}:{code}:{quantity.strip()}",
        )
    except (WorkOrderError, AuthorizationError, ArithmeticError, ValueError):
        return _wo_redirect(work_order_no, "part_err")
    return _wo_redirect(work_order_no, "part_ok" if ok else "part_dup")


@router.post("/work-orders/{work_order_no}/parts/{part_id}/update", response_model=None)
async def work_order_update_part(
    work_order_no: int,
    part_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    quantity: str = Form(...),
    nonce: str = Form(...),
) -> RedirectResponse:
    """改工單領料數量(#9):差額連動庫存(增→再扣、減→回庫);終態拒改。冪等 nonce+payload。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無領料權
        return _wo_redirect(work_order_no, "part_err")
    try:
        ok = await WorkOrderService(session).update_part_issue_quantity(
            work_order_no=work_order_no, part_id=part_id, new_quantity=quantity,
            actor=Actor.human(user.user_id),
            idempotency_key=f"webpartupd:v1:{nonce}:{part_id}:{quantity.strip()}",
        )
    except (WorkOrderError, InventoryError, AuthorizationError, ArithmeticError, ValueError):
        return _wo_redirect(work_order_no, "part_err")
    return _wo_redirect(work_order_no, "part_upd" if ok else "part_dup")


@router.post("/work-orders/{work_order_no}/parts/{part_id}/cancel", response_model=None)
async def work_order_cancel_part(
    work_order_no: int,
    part_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    nonce: str = Form(...),
) -> RedirectResponse:
    """取消工單領料(#9):RETURN 全數回庫 + work_order_part 軟刪;終態拒改。冪等 nonce。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無領料權
        return _wo_redirect(work_order_no, "part_err")
    try:
        ok = await WorkOrderService(session).cancel_part_issue(
            work_order_no=work_order_no, part_id=part_id,
            actor=Actor.human(user.user_id),
            idempotency_key=f"webpartcancel:v1:{nonce}:{part_id}",
        )
    except (WorkOrderError, InventoryError, AuthorizationError, ArithmeticError, ValueError):
        return _wo_redirect(work_order_no, "part_err")
    return _wo_redirect(work_order_no, "part_cancelled" if ok else "part_dup")


@router.post("/work-orders/{work_order_no}/links", response_model=None)
async def work_order_add_link(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    external_key: str = Form(...),
    sync: str = Form(""),
) -> RedirectResponse:
    """連結既有 Jira MRQ 單(ADR-020:落庫連結事實,shape 守門 MRQ-<n>)。

    `sync` 勾選 → link_type=appended:此後新 note 自動進 jira_outbox 同步管線(既有機制)。需先存
    Jira PAT(否則不落連結、提示先存 PAT)。未勾 → referenced(現行為,不同步)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    link_type = "referenced"
    if sync.strip():  # 勾選同步 → 需 PAT ready,否則不落連結、誠實提示
        if not await JiraSyncService(session).pat_ready(user.user_id):
            return _wo_redirect(work_order_no, "link_needpat")
        link_type = "appended"
    try:
        # operator 無 MRQ 連結權(domain record_external_link 閘);表單在 detail 已隱藏。
        await WorkOrderService(session).record_external_link(
            work_order_no=work_order_no,
            external_key=external_key.strip().upper(),
            link_type=link_type,
            actor=Actor.human(user.user_id),
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "link_err")
    return _wo_redirect(work_order_no, "link_ok")


@router.post("/work-orders/{work_order_no}/links/{link_id}/remove", response_model=None)
async def work_order_remove_link(
    work_order_no: int,
    link_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> RedirectResponse:
    """移除 MRQ 連結(軟移除,留稽核;review f14cf8d:打錯的 key 要能更正)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    try:
        await WorkOrderService(session).remove_external_link(
            link_id, work_order_no=work_order_no, actor=Actor.human(user.user_id)
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "link_err")
    return _wo_redirect(work_order_no, "link_removed")


@router.post("/work-orders/{work_order_no}/cancel", response_model=None)
async def work_order_cancel(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    reason: str = Form(""),
) -> RedirectResponse:
    """取消誤開報修(僅 OPEN→CANCELLED 軟取消;非刪除 —— SoR 保留稽核軌跡)。事由落 note。
    Jordan 2026-07-05 #3c:工程師面唯一取消入口,事由必填。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if not reason.strip():
        return _wo_redirect(work_order_no, "cancel_err")  # 事由必填(#3c)
    actor = Actor.human(user.user_id)
    try:  # 事由與轉移同交易原子寫入(review f14cf8d)。/cancel 不 route 擋 operator ——
        # 「僅能取消自己開的」由 domain cancel_reactive_report 強制;operator 取消他人單 →
        # AuthorizationError → cancel_err 既有路徑(不 500)。
        await WorkOrderService(session).cancel_reactive_report(
            work_order_no, actor, reason=reason.strip()
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "cancel_err")
    return _wo_redirect(work_order_no)


@router.post("/work-orders/{work_order_no}/void", response_model=None)
async def work_order_void(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    reason: str = Form(...),
) -> RedirectResponse:
    """作廢工單(admin 直接執行;工程師走 request-void 提案)。高風險,必附事由(落 note)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":
        return _wo_redirect(work_order_no, "void_err")
    try:  # admin 授權在 domain 再驗一次;事由與轉移同交易(review f14cf8d)
        await WorkOrderService(session).void_work_order(
            work_order_no, Actor.human(user.user_id), reason=reason.strip() or None
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "void_err")
    return _wo_redirect(work_order_no)


@router.post("/work-orders/{work_order_no}/request-void", response_model=None)
async def work_order_request_void(
    work_order_no: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    reason: str = Form(...),
    nonce: str = Form(...),
) -> RedirectResponse:
    """工程師請求作廢(ADR-025 Lane 1):建 pending_proposal → admin 於 /admin/proposals 審核。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無提案(請求作廢)權
        return _wo_redirect(work_order_no, "voidreq_err")
    if not reason.strip():
        return _wo_redirect(work_order_no, "voidreq_err")
    try:
        # 同單重複請求 / 不可作廢狀態 → propose 內部擋(review f14cf8d);
        # TTL 由 proposer 身分決定(human 7 天),不再由 route 記得傳。
        await WorkOrderService(session).propose(
            operation="void_work_order",
            params={"work_order_no": work_order_no, "reason": reason.strip()},
            proposed_by=Actor.human(user.user_id),
            idempotency_key=f"webvoidreq:v2:{nonce}:{work_order_no}",
        )
    except (WorkOrderError, AuthorizationError):
        return _wo_redirect(work_order_no, "voidreq_err")
    return _wo_redirect(work_order_no, "voidreq_ok")


@router.get("/inventory", response_class=HTMLResponse, response_model=None)
async def inventory_search(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    q: str | None = Query(None),
    low: bool = Query(False),
    page: int = Query(1, ge=1),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """備品查詢(讀取,開放):q = item_code / name / description / vendor_part_no ilike。

    無查詢字串 → 預設列出最新 N 筆(可瀏覽,不再空白);有 q → 縮小到符合者。
    `low=1` = 只列低於再訂購點者(on_hand < reorder_point;補貨視角 + admin 批次詢價入口)。
    `page` = 分頁(1-based,取代固定筆數上限)。`msg` = 'adminonly' 橫幅(非 admin 嘗試建新品項)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    inv = InventoryService(session)
    rows = await inv.list_items(
        search=(q.strip() if q and q.strip() else None), below_reorder=low,
        limit=_PER_PAGE + 1, offset=(page - 1) * _PER_PAGE,
    )
    items, has_next = _paginate(rows, _PER_PAGE)
    # 縮圖(1042 張品項照片已上 R2;批次一查免 N+1)
    att_svc = AttachmentService(session)
    first = await att_svc.first_attachment_map("inventory_item", [i.item_code for i in items])
    thumbs = {oid: att_svc.presigned_url(att)[0] for oid, att in first.items()}
    pager_base = _pager_base("/app/inventory", {"q": q, "low": "1" if low else None})
    name = "partials/inv_list.html" if _is_htmx(request) else "inventory.html"
    return _render(request, name, user.ui_locale, active="inventory",
                   user=user, q=q, low=low, items=items, thumbs=thumbs,
                   page=page, has_next=has_next, pager_base=pager_base, msg=msg)


async def _storage_bin_options(session: AsyncSession) -> list[dict[str, str]]:
    """儲位受控詞彙序列化為 combobox options(active only;shape=[{"c": code}])。"""
    bins = await InventoryService(session).list_storage_bins()
    return [{"c": b.code} for b in bins]


# ★ route 順序:/inventory/new 兩路由必須宣告在 /inventory/{item_code} 之前,
#   否則 "new" 會被當成 item_code 捕獲(且無 EID 樣板可擋 → 會 200 出佔位頁)。
#   副作用:真有料號叫 "NEW" 者在 /app/inventory/new 不可達(可忽略)。
@router.get("/inventory/new", response_class=HTMLResponse, response_model=None)
async def inventory_new_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """新增備品表單(admin-only;登記 eMaint 尚未有的新料號)。

    非 admin → 回備品清單(adminonly banner)。`msg` = 'err' → 重顯錯誤橫幅。
    傳空白欄位物件(SimpleNamespace)給共用 `partials/item_fields.html`,免另建欄位模板。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url="/app/inventory?msg=adminonly", status_code=303)
    blank = SimpleNamespace(
        name=None, description=None, vendor_part_no=None, bin_location=None,
        reorder_point=None, reorder_quantity=None, lead_time_weeks=None, unit_cost=None,
        supplier=None, supplier_org_id=None, weblink=None, comment=None,
        is_stocked=True, is_obsolete=False,
    )
    return _render(request, "inventory_new.html", user.ui_locale, active="inventory",
                   user=user, item=blank, msg=msg, nonce=secrets.token_urlsafe(8),
                   bins=await _storage_bin_options(session), back_url="/app/inventory")


@router.post("/inventory/new", response_model=None)
async def inventory_create(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    item_code: str = Form(...),
    name: str = Form(""),
    description: str = Form(""),
    vendor_part_no: str = Form(""),
    bin_location: str = Form(""),
    reorder_point: str = Form(""),
    reorder_quantity: str = Form(""),
    lead_time_weeks: str = Form(""),
    unit_cost: str = Form(""),
    supplier: str = Form(""),
    supplier_org_id: str = Form(""),
    weblink: str = Form(""),
    comment: str = Form(""),
    is_stocked: bool = Form(False),
    is_obsolete: bool = Form(False),
    initial_quantity: str = Form(""),
    photos: list[UploadFile] = File(default=[]),
    nonce: str = Form(...),
) -> RedirectResponse:
    """建立新備品(admin-only;domain 亦強制)。期初在庫選填 → 另送 ADJUST 記帳(ADR-005);
    照片選填 → 品項建立後掛上(附件需 owner 已存在,故先 create 再上傳,同一請求)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url="/app/inventory?msg=adminonly", status_code=303)
    actor = Actor.human(user.user_id)
    # 供應商欄正規化為 canonical org 名(#7 續)+ 連動 org_id
    supplier, supplier_org_id = await _normalize_supplier(session, supplier, supplier_org_id)
    try:
        lead = lead_time_weeks.strip()
        item = await InventoryService(session).create_item(
            item_code,
            actor=actor,
            name=name.strip(),
            description=description.strip() or None,
            vendor_part_no=vendor_part_no.strip() or None,
            bin_location=bin_location.strip() or None,
            reorder_point=_dec_or_none(reorder_point),
            reorder_quantity=_dec_or_none(reorder_quantity),
            lead_time_weeks=int(lead) if lead else None,
            unit_cost=_dec_or_none(unit_cost),
            supplier=supplier.strip() or None,
            supplier_org_id=supplier_org_id.strip() or None,
            weblink=weblink.strip() or None,
            comment=comment.strip() or None,
            is_stocked=is_stocked,
            is_obsolete=is_obsolete,
        )
    except (InventoryError, AuthorizationError, ArithmeticError, ValueError):
        return RedirectResponse(url="/app/inventory/new?msg=err", status_code=303)
    # 照片(選填):品項已建立成功後掛上(add_attachment 需 owner 存在,故先 create 再上傳)。
    # 失敗不回滾品項(已存在=誠實部分狀態),轉明細頁 photo_err,admin 可於照片管理區重傳。
    if any(getattr(f, "filename", None) for f in photos):
        try:
            await _upload_photos(session, "inventory_item", item.item_code, photos, actor)
        except AttachmentError:
            return _inv_redirect(item.item_code, "photo_err")
    # 期初在庫(選填):品項已建立成功後另記一筆 ADJUST(從 NULL/0 起算)。失敗不回滾品項
    # (已存在=誠實部分狀態),轉明細頁顯示 editerr,admin 可於盤點區重調。
    if initial_quantity.strip():
        try:
            await InventoryService(session).adjust_on_hand(
                item.item_code,
                new_quantity=initial_quantity,
                reason="initial count at item creation",
                actor=actor,
                idempotency_key=f"webcreate:v1:{nonce}:{item.item_code}:{initial_quantity.strip()}",
            )
        except (InventoryError, ArithmeticError, ValueError):
            return _inv_redirect(item.item_code, "editerr")
    return _inv_redirect(item.item_code, "saved")


# ★ 宣告在 /inventory/{item_code} 之前(否則 "bins" 會被當成料號捕獲)。
@router.post("/inventory/bins", response_model=None)
async def inventory_add_bin(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    code: str = Form(...),
) -> JSONResponse:
    """新增儲位代號(備品編輯內 combobox quick-add;admin-only)。JSON 端點:
    成功 → {ok:true, code};驗證/授權失敗 → 400 {ok:false, error}。domain 亦強制 admin。"""
    if user is None:
        return JSONResponse({"ok": False, "error": "not authenticated"}, status_code=401)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return JSONResponse({"ok": False, "error": "admin only"}, status_code=403)
    try:
        row = await InventoryService(session).add_storage_bin(
            code, actor=Actor.human(user.user_id)
        )
    except (InventoryError, AuthorizationError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "code": row.code})


@router.get("/inventory/{item_code}", response_class=HTMLResponse, response_model=None)
async def inventory_detail(
    request: Request,
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    rfq: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """備品明細:照片(attachment)+ 替代品 / 套件 BOM / 適用機種 + RFQ 一鍵(ADR-026)。

    `rfq` = 一鍵詢價結果 banner(sent/drafted/failed/nosupplier)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    inv = InventoryService(session)
    item = await inv.get_item(code)
    if item is None:
        return _render(request, "placeholder.html", user.ui_locale, active="inventory",
                       user=user, screen_title=code)
    alternatives = await inv.get_alternatives(code)
    kit = await inv.get_kit_children(code)
    subtypes = await inv.get_applicable_subtypes(code)
    parent_kits = await inv.get_parent_kits(code)  # 反查:哪些套件含本品(#7e)
    all_subtypes = await inv.list_all_asset_subtypes() if user.role == "admin" else []
    att_svc = AttachmentService(session)
    atts = await att_svc.list_attachments("inventory_item", code)
    # (attachment_id, presigned_url):admin 逐張刪除需 id(換照片 = 刪舊 + 傳新)
    photos = [(a.id, att_svc.presigned_url(a)[0]) for a in atts]
    supplier_org = (
        await ContactsService(session).get_organization(item.supplier_org_id)
        if item.supplier_org_id else None
    )
    # engineer 主檔修改走提案(裁決 #3 後續):已有待審提案 → 顯示審核中、不重收
    pending_item_edit = (
        await WorkOrderService(session).find_pending_proposal(
            operation="update_item", item_code=code
        )
        is not None
    )
    return _render(request, "inventory_detail.html", user.ui_locale, active="inventory",
                   user=user, item=item, alternatives=alternatives, kit=kit,
                   subtypes=subtypes, parent_kits=parent_kits, all_subtypes=all_subtypes,
                   photos=photos, rfq=rfq, supplier_org=supplier_org,
                   pending_item_edit=pending_item_edit, bins=await _storage_bin_options(session),
                   nonce=secrets.token_urlsafe(8), back_url="/app/inventory")


@router.post("/inventory/{item_code}/rfq", response_model=None)
async def inventory_rfq(
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    nonce: str = Form(...),
) -> RedirectResponse:
    """一鍵 RFQ(ADR-026,human-initiated,**限 admin**,Jordan 2026-07-03):對供應商 org 送詢價。

    誠實降級:SMTP 未配置 → `dry_run=True` 只落 drafted(不讓 InMemory 假裝 sent);
    未連 supplier org → nosupplier 提示(RFQ-ineligible)。nonce → 冪等(重複點不重送)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    if user.role != "admin":  # 對外發信 = admin-only(engineer 走 agent dry-run / 找 admin)
        return _inv_redirect(code, "adminonly")
    item = await InventoryService(session).get_item(code)
    if item is None:
        return RedirectResponse(url="/app/inventory", status_code=303)
    if not item.supplier_org_id:
        return _inv_redirect(code, "nosupplier")
    try:
        r = await ProcurementService(session).create_rfq(
            supplier_org_id=item.supplier_org_id,
            item_codes=[code],
            actor=Actor.human(user.user_id),
            dry_run=not smtp_configured(),  # 未配置 SMTP → 只落 drafted(誠實降級)
            idempotency_key=f"webrfq:v2:{nonce}:{code}",
        )
    except ProcurementError:
        return _inv_redirect(code, "failed")
    return _inv_redirect(code, r.status)


def _dec_or_none(v: str) -> Decimal | None:
    """表單字串 → Decimal(空 → None)。非數字 → InvalidOperation(呼叫端轉 err banner)。"""
    v = v.strip()
    return Decimal(v) if v else None


def _inv_redirect(item_code: str, msg: str | None = None) -> RedirectResponse:
    """備品明細 redirect(結果 banner 沿用 `rfq` 參數;統一七處手寫,review f14cf8d)。"""
    url = f"/app/inventory/{item_code}" + (f"?rfq={msg}" if msg else "")
    return RedirectResponse(url=url, status_code=303)


async def _normalize_supplier(
    session: AsyncSession, supplier: str, supplier_org_id: str
) -> tuple[str, str]:
    """供應商欄儲存前正規化(#7 續:autocomplete 回 org.name,但手打全小寫直送會存成小寫)。

    非空 `supplier` 若 case-insensitive 完全等於某 org 名 → 以 canonical 大小寫取代;且
    `supplier_org_id` 空時一併帶入該 org_id(連動)。找不到 → 維持原輸入(自由文字供應商
    描述仍允許,不硬擋)。回 `(supplier, supplier_org_id)`(已 strip)。
    """
    supplier = supplier.strip()
    supplier_org_id = supplier_org_id.strip()
    if not supplier:
        return supplier, supplier_org_id
    key = supplier.lower()
    for o in await ContactsService(session).list_organizations(search=supplier, limit=20):
        if o.name.lower() == key:
            return o.name, (supplier_org_id or o.org_id)
    return supplier, supplier_org_id


@router.post("/inventory/{item_code}/update", response_model=None)
async def inventory_update(
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    name: str = Form(""),
    description: str = Form(""),
    vendor_part_no: str = Form(""),
    bin_location: str = Form(""),
    reorder_point: str = Form(""),
    reorder_quantity: str = Form(""),
    lead_time_weeks: str = Form(""),
    unit_cost: str = Form(""),
    supplier: str = Form(""),
    supplier_org_id: str = Form(""),
    weblink: str = Form(""),
    comment: str = Form(""),
    is_stocked: bool = Form(False),
    is_obsolete: bool = Form(False),
) -> RedirectResponse:
    """品項主檔編輯(admin;governed update_item)。庫存量另走 adjust(ADR-005 記帳)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    if user.role != "admin":
        return _inv_redirect(code, "adminonly")
    try:
        lead = lead_time_weeks.strip()
        # 供應商欄正規化為 canonical org 名(#7 續)+ 連動 org_id
        supplier, supplier_org_id = await _normalize_supplier(session, supplier, supplier_org_id)
        # 主檔 + 供應商連結同一交易(review f14cf8d:先前分兩段,後段失敗會顯示
        # 「儲存失敗」但主檔已寫入);空 supplier_org_id = 清除連結(先前無 unlink 路徑)。
        await InventoryService(session).update_item(
            code,
            actor=Actor.human(user.user_id),
            name=name.strip() or None,
            description=description.strip() or None,
            vendor_part_no=vendor_part_no.strip() or None,
            bin_location=bin_location.strip() or None,
            reorder_point=_dec_or_none(reorder_point),
            reorder_quantity=_dec_or_none(reorder_quantity),
            lead_time_weeks=int(lead) if lead else None,
            unit_cost=_dec_or_none(unit_cost),
            supplier=supplier.strip() or None,
            weblink=weblink.strip() or None,
            comment=comment.strip() or None,
            is_stocked=is_stocked,
            is_obsolete=is_obsolete,
            supplier_org_id=supplier_org_id.strip() or None,
        )
    except (InventoryError, ArithmeticError, ValueError):
        return _inv_redirect(code, "editerr")
    return _inv_redirect(code, "saved")


@router.post("/inventory/{item_code}/propose-update", response_model=None)
async def inventory_propose_update(
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    name: str = Form(""),
    description: str = Form(""),
    vendor_part_no: str = Form(""),
    bin_location: str = Form(""),
    reorder_point: str = Form(""),
    reorder_quantity: str = Form(""),
    lead_time_weeks: str = Form(""),
    unit_cost: str = Form(""),
    supplier: str = Form(""),
    supplier_org_id: str = Form(""),
    weblink: str = Form(""),
    comment: str = Form(""),
    is_stocked: bool = Form(False),
    is_obsolete: bool = Form(False),
) -> RedirectResponse:
    """品項修改**提案**(engineer 面;裁決 #3 後續):與作廢請求同機制 —— 建 pending_proposal,
    admin 於 /admin/proposals 審 dry-run diff 後 confirm 執行(走 update_item 單一寫入路徑)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    # 供應商欄正規化為 canonical org 名(#7 續):提案階段先正規化,存進 params 的即 canonical,
    # admin confirm 走 update_item 單一寫入路徑時已是 canonical(dry-run diff 也顯示正確值)。
    supplier, supplier_org_id = await _normalize_supplier(session, supplier, supplier_org_id)
    try:
        await WorkOrderService(session).propose(
            operation="update_item",
            params={
                "item_code": code,
                "name": name, "description": description,
                "vendor_part_no": vendor_part_no, "bin_location": bin_location,
                "reorder_point": reorder_point, "reorder_quantity": reorder_quantity,
                "lead_time_weeks": lead_time_weeks, "unit_cost": unit_cost,
                "supplier": supplier, "supplier_org_id": supplier_org_id,
                "weblink": weblink, "comment": comment,
                "is_stocked": is_stocked, "is_obsolete": is_obsolete,
            },
            proposed_by=Actor.human(user.user_id),
            idempotency_key=f"webitemprop:v1:{code}:{secrets.token_urlsafe(6)}",
        )
    except WorkOrderError:
        return _inv_redirect(code, "properr")
    return _inv_redirect(code, "proposed")


@router.post("/inventory/{item_code}/adjust", response_model=None)
async def inventory_adjust(
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    new_quantity: str = Form(...),
    reason: str = Form(...),
    nonce: str = Form(...),
) -> RedirectResponse:
    """盤點調整在庫量(admin):記 ADJUST 帳連動 on_hand(不裸改,ADR-005),必附事由。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    if user.role != "admin":
        return _inv_redirect(code, "adminonly")
    try:
        adjusted = await InventoryService(session).adjust_on_hand(
            code,
            new_quantity=new_quantity,
            reason=reason,
            actor=Actor.human(user.user_id),
            # 冪等鍵綁 payload(review f14cf8d:純 nonce 會把 back-button 後的第二次
            # 「不同數量」調整靜默丟棄還顯示 Saved)
            idempotency_key=f"webadjust:v2:{nonce}:{code}:{new_quantity.strip()}",
        )
    except (InventoryError, ArithmeticError, ValueError):
        return _inv_redirect(code, "editerr")
    return _inv_redirect(code, "saved" if adjusted else "nochange")


@router.post("/inventory/{item_code}/subtypes", response_model=None)
async def inventory_set_subtypes(
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    subtypes: list[str] = Form(default=[]),
) -> RedirectResponse:
    """設品項適用機種(#7d;admin;複選覆寫 junction,經 domain 全稽核)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    if user.role != "admin":
        return _inv_redirect(code, "adminonly")
    try:
        await InventoryService(session).set_applicable_subtypes(
            code, subtypes, actor=Actor.human(user.user_id)
        )
    except (InventoryError, AuthorizationError):
        return _inv_redirect(code, "subtypeerr")
    return _inv_redirect(code, "saved")


@router.post("/inventory/{item_code}/photos", response_model=None)
async def inventory_add_photos(
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    photos: list[UploadFile] = File(default=[]),
) -> RedirectResponse:
    """上傳備品照片(admin;掛 owner_type='inventory_item')。content-addressed 冪等,無需 nonce。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    if user.role != "admin":
        return _inv_redirect(code, "adminonly")
    if await InventoryService(session).get_item(code) is None:
        return RedirectResponse(url="/app/inventory", status_code=303)
    try:
        n = await _upload_photos(session, "inventory_item", code, photos, Actor.human(user.user_id))
    except AttachmentError:
        return _inv_redirect(code, "photo_err")
    return _inv_redirect(code, "photo_ok" if n else "photo_err")


@router.post("/inventory/{item_code}/photos/{attachment_id}/delete", response_model=None)
async def inventory_delete_photo(
    item_code: str,
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> RedirectResponse:
    """移除備品照片(admin;軟刪,R2 物件保留供稽核)。換照片 = 刪舊 + 傳新。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    code = item_code.upper()
    if user.role != "admin":
        return _inv_redirect(code, "adminonly")
    att_svc = AttachmentService(session)
    att = await att_svc.get_attachment(attachment_id)
    if att is None or att.owner_type != "inventory_item" or att.owner_id != code:
        return _inv_redirect(code, "photo_err")
    await att_svc.soft_delete_attachment(
        attachment_id, Actor.human(user.user_id), reason="admin removed item photo"
    )
    return _inv_redirect(code)


@router.get("/inventory-rfq", response_class=HTMLResponse, response_model=None)
async def inventory_rfq_batch_preview(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    sent: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """批次詢價預覽(admin):低於再訂購點品項按供應商分組(dry-run 草案),逐組確認送出。
    `sent` = 上一次送出的**真實結果**(sent/drafted/failed;review f14cf8d:不再一律報成功)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":
        return RedirectResponse(url="/app/inventory", status_code=303)
    drafts = await ProcurementService(session).draft_below_safety_stock()
    return _render(request, "inventory_rfq_batch.html", user.ui_locale, active="inventory",
                   user=user, drafts=drafts, sent=sent, smtp_ok=smtp_configured(),
                   nonce=secrets.token_urlsafe(8), back_url="/app/inventory?low=1")


@router.post("/inventory-rfq", response_model=None)
async def inventory_rfq_batch_send(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    supplier_org_id: str = Form(...),
    item_codes: str = Form(...),
    nonce: str = Form(...),
) -> RedirectResponse:
    """批次詢價:對一個供應商 org 送出其低庫存品項的 RFQ(admin;誠實降級同單品)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":
        return RedirectResponse(url="/app/inventory", status_code=303)
    codes = [c.strip().upper() for c in item_codes.split(",") if c.strip()]
    if not codes:
        return RedirectResponse(url="/app/inventory-rfq", status_code=303)
    try:
        r = await ProcurementService(session).create_rfq(
            supplier_org_id=supplier_org_id,
            item_codes=codes,
            actor=Actor.human(user.user_id),
            dry_run=not smtp_configured(),
            idempotency_key=f"webrfqbatch:v2:{nonce}:{supplier_org_id}",
        )
    except ProcurementError:
        return RedirectResponse(url="/app/inventory-rfq?sent=failed", status_code=303)
    # create_rfq 把寄信失敗吞成 status='failed' 不 raise → banner 要反映真實狀態
    return RedirectResponse(url=f"/app/inventory-rfq?sent={r.status}", status_code=303)


@router.get("/equipment", response_class=HTMLResponse, response_model=None)
async def equipment_search(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    q: str | None = Query(None),
    atype: str | None = Query(None),
    page: int = Query(1, ge=1),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """設備查詢(讀取,開放,Slice 3):q = EID / 描述 ilike;`atype` = 類別 chip(#4d)。

    無查詢字串 → 預設列出最新 N 筆(可瀏覽,不空白)+ Production 類別預設;
    **有查詢字串且使用者未顯式選類別 → 類別預設「全部」**(W2 殘留修:搜非 Production 的 EID
    不再被 Production 過濾掉空手)。`atype` 顯式帶值(chip 點選)一律尊重、與搜尋共存。
    `page` = 分頁(1-based,取代固定筆數上限)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    svc = AssetService(session)
    offset = (page - 1) * _PER_PAGE
    has_q = bool(q and q.strip())
    # 顯式 atype(chip 點選/URL 帶值)= 使用者選擇,尊重之;未顯式(None/空)→ 有搜尋則「全部」、
    # 無搜尋則 Production 預設。effective 供 chip 高亮 + pager;hidden 欄只帶「使用者顯式選擇」
    # (見 equipment.html),故打字搜尋時不會被上次的 Production 預設鎖死。
    explicit = atype if (atype is not None and atype != "") else None
    effective = explicit if explicit is not None else ("all" if has_q else _EQ_TYPE_DEFAULT)
    # 只認白名單類別碼過濾,其餘(含 'all')→ 不過濾
    type_filter = effective if effective in _EQ_TYPES else None
    rows = await svc.list_assets(
        search=(q.strip() if has_q else None),
        asset_type=type_filter, limit=_PER_PAGE + 1, offset=offset,
    )
    assets, has_next = _paginate(rows, _PER_PAGE)
    # 縮圖(有照片顯示、無則 fallback 圖示;批次一查免 N+1;presign 為本地簽名無網路 I/O)
    att_svc = AttachmentService(session)
    first = await att_svc.first_attachment_map("asset", [a.asset_id for a in assets])
    thumbs = {oid: att_svc.presigned_url(att)[0] for oid, att in first.items()}
    pager_base = _pager_base("/app/equipment", {"q": q, "atype": effective})
    name = "partials/eq_list.html" if _is_htmx(request) else "equipment.html"
    return _render(request, name, user.ui_locale, active="equipment",
                   user=user, q=q, atype=effective, atype_explicit=(explicit or ""),
                   eq_types=_EQ_TYPES, assets=assets, thumbs=thumbs, msg=msg,
                   page=page, has_next=has_next, pager_base=pager_base)


# ★ route 順序:/equipment/new 兩路由必須宣告在 /equipment/{asset_id} 之前,
#   否則 "new" 會被當成 asset_id 捕獲。
@router.get("/equipment/new", response_class=HTMLResponse, response_model=None)
async def equipment_new_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """新增設備表單(admin-only;註冊 eMaint 尚未登記的新 EID,內部規格)。

    非 admin → 回設備清單(adminonly banner)。`msg` = 'err' → 重顯錯誤橫幅。
    受控 lookup 選項(asset_type / department / line)供下拉。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url="/app/equipment?msg=adminonly", status_code=303)
    svc = AssetService(session)
    asset_types = await svc.list_asset_types()
    departments = await svc.list_departments()
    lines = await svc.list_lines()
    return _render(request, "equipment_new.html", user.ui_locale, active="equipment",
                   user=user, asset_types=asset_types, departments=departments,
                   lines=lines, msg=msg, back_url="/app/equipment")


@router.post("/equipment/new", response_model=None)
async def equipment_create(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    asset_id: str = Form(...),
    description: str = Form(...),
    asset_type: str = Form(...),
    asset_subtype: str = Form(""),
    department: str = Form(""),
    line: str = Form(""),
    site: str = Form(...),
    model_no: str = Form(""),
    serial_no: str = Form(""),
    manufacturer: str = Form(""),
    host_name: str = Form(""),
    asset_ref: str = Form(""),
    product: str = Form(""),
    weblink: str = Form(""),
    comments: str = Form(""),
    process_segment_class: str = Form(""),
    owners: list[str] = Form(default=[]),
) -> RedirectResponse:
    """建立新設備(admin-only;domain 亦強制)。成功 → 轉新設備明細頁。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url="/app/equipment?msg=adminonly", status_code=303)
    try:
        asset = await AssetService(session).create_asset(
            asset_id, actor=Actor.human(user.user_id),
            description=description, asset_type=asset_type, asset_subtype=asset_subtype,
            department=department, line=line, site=site,
            model_no=model_no, serial_no=serial_no, manufacturer=manufacturer,
            host_name=host_name, asset_ref=asset_ref, product=product, weblink=weblink,
            comments=comments, process_segment_class=process_segment_class, owners=owners,
        )
    except (AssetError, AuthorizationError):
        return RedirectResponse(url="/app/equipment/new?msg=err", status_code=303)
    return RedirectResponse(url=f"/app/equipment/{asset.asset_id}?msg=saved", status_code=303)


@router.get("/equipment/{asset_id}", response_class=HTMLResponse, response_model=None)
async def equipment_detail(
    request: Request,
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    issue: str | None = Query(None),
    msg: str | None = Query(None),
    parts: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """設備明細:主檔欄位 + 外部系統 id + 含括模組(組成,ADR-018)+ 近期工單(rollup 含後代模組)
    + 單機零件消耗(直領 ∪ 工單領料,ADR-024)+ 直領表單 + admin 啟停旗標。

    `issue` = 直領結果 banner;`msg` = 啟停旗標結果 banner(saved/err/adminonly);
    `parts` = 'all' → 展開完整用料清單(預設只顯示最近 5 筆 + 「全部(N)」連結,#4c)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.upper()
    svc = AssetService(session)
    asset = await svc.get_asset(eid)
    if asset is None:
        return _render(request, "placeholder.html", user.ui_locale, active="equipment",
                       user=user, screen_title=eid)
    owners = await svc.get_owners(eid)  # 0031 設備負責人清單(顯示 + admin 編輯預填)
    external_ids = await svc.list_external_ids(eid)
    modules = []
    for mid in await svc.get_contained_descendants(eid):  # contains_module 後代(組成子樹)
        m = await svc.get_asset(mid)
        if m is not None:
            modules.append(m)
    work_orders = await svc.rollup_work_orders(eid, limit=5)  # 近期 5 筆(全部→佇列搜尋)
    # 單機零件消耗:自身 + 後代模組(直領歸屬 ∪ 這些機台工單的領料),新到舊。
    # #4c:預設只顯示最近 5 筆 + 「全部(N)」連結;`?parts=all` 展開完整清單。
    # 一次撈上限 CAP(單機用料通常遠小於此),取 len 當 N、切 5 當預設檢視(免額外 COUNT)。
    _PART_USAGE_CAP = 200
    inv_svc = InventoryService(session)
    usage_all = await inv_svc.list_asset_part_usage(
        [eid, *(m.asset_id for m in modules)], limit=_PART_USAGE_CAP
    )
    show_all_parts = parts == "all"
    part_usage = usage_all if show_all_parts else usage_all[:5]
    part_usage_total = len(usage_all)
    # 已取消/已改量 supersede 的直領帳 → 隱藏其改量/取消按鈕(#9;ledger 列仍誠實呈現)
    cancelled_issue_ids = await inv_svc.cancelled_asset_issue_ids(
        [t.txn_id for t in part_usage if t.kind == "ISSUE" and t.work_order_no is None]
    )
    att_svc = AttachmentService(session)
    atts = await att_svc.list_attachments("asset", eid)  # 設備照片(資料源待補,顯示已接好)
    photos = [att_svc.presigned_url(a)[0] for a in atts]
    # admin 主檔編輯下拉:受控 lookup 選項(非 admin 不渲染編輯區 → 免查)
    if user.role == "admin":
        asset_types = await svc.list_asset_types()
        departments = await svc.list_departments()
        lines = await svc.list_lines()
    else:
        asset_types, departments, lines = [], [], []
    return _render(request, "equipment_detail.html", user.ui_locale, active="equipment",
                   user=user, asset=asset, owners=owners, external_ids=external_ids,
                   modules=modules, work_orders=work_orders, part_usage=part_usage,
                   part_usage_total=part_usage_total, show_all_parts=show_all_parts,
                   cancelled_issue_ids=cancelled_issue_ids,
                   backfill_source_actor=BACKFILL_ACTOR.value,
                   asset_types=asset_types, departments=departments, lines=lines,
                   photos=photos, issue=issue, msg=msg, nonce=secrets.token_urlsafe(8),
                   back_url="/app/equipment")


@router.post("/equipment/{asset_id}/flags", response_model=None)
async def equipment_set_flags(
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    flag: str = Form(...),
    value: str = Form(...),
) -> RedirectResponse:
    """啟停設備旗標(admin-only;domain 亦強制)。`flag` = active / available;`value` = 1 / 0。

    兩旗標皆資訊性(見 domain setters):is_active=在冊/退役、available_for_service=清單過濾。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.upper()
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url=f"/app/equipment/{eid}?msg=adminonly", status_code=303)
    on = value == "1"
    svc = AssetService(session)
    try:
        if flag == "active":
            await svc.set_asset_active(eid, on, actor=Actor.human(user.user_id))
        elif flag == "available":
            await svc.set_available_for_service(eid, on, actor=Actor.human(user.user_id))
        else:
            return RedirectResponse(url=f"/app/equipment/{eid}?msg=err", status_code=303)
    except (AssetError, AuthorizationError):
        return RedirectResponse(url=f"/app/equipment/{eid}?msg=err", status_code=303)
    return RedirectResponse(url=f"/app/equipment/{eid}?msg=saved", status_code=303)


@router.post("/equipment/{asset_id}/edit", response_model=None)
async def equipment_update(
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    description: str = Form(...),
    asset_type: str = Form(...),
    asset_subtype: str = Form(""),
    department: str = Form(""),
    line: str = Form(""),
    site: str = Form(...),
    model_no: str = Form(""),
    serial_no: str = Form(""),
    manufacturer: str = Form(""),
    host_name: str = Form(""),
    asset_ref: str = Form(""),
    product: str = Form(""),
    weblink: str = Form(""),
    comments: str = Form(""),
    process_segment_class: str = Form(""),
    owners: list[str] = Form(default=[]),
) -> RedirectResponse:
    """設備主檔編輯(admin-only;domain 亦強制)。EID(PK,A4)唯讀不改;旗標另走 /flags。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.upper()
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url=f"/app/equipment/{eid}?msg=adminonly", status_code=303)
    try:
        await AssetService(session).update_asset(
            eid, actor=Actor.human(user.user_id),
            description=description, asset_type=asset_type, asset_subtype=asset_subtype,
            department=department, line=line, site=site,
            model_no=model_no, serial_no=serial_no, manufacturer=manufacturer,
            host_name=host_name, asset_ref=asset_ref, product=product, weblink=weblink,
            comments=comments, process_segment_class=process_segment_class, owners=owners,
        )
    except (AssetError, AuthorizationError):
        return RedirectResponse(url=f"/app/equipment/{eid}?msg=err", status_code=303)
    return RedirectResponse(url=f"/app/equipment/{eid}?msg=saved", status_code=303)


@router.post("/equipment/{asset_id}/issue", response_model=None)
async def equipment_issue_part(
    asset_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    item_code: str = Form(...),
    quantity: str = Form(...),
    reason: str = Form(""),
    nonce: str = Form(...),
) -> RedirectResponse:
    """非工單直領(ADR-024 前端入口):領料歸屬此設備、扣 on_hand,actor = 登入者。

    nonce → idempotency_key(同表單重複送出 = 冪等跳過);未知料號/EID → banner 提示。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.upper()
    try:
        code = item_code.strip().upper()
        ok = await InventoryService(session).issue_to_asset(
            asset_id=eid,
            item_code=code,
            quantity=quantity,
            actor=Actor.human(user.user_id),
            reason=reason.strip() or None,
            # 冪等鍵綁 payload(review f14cf8d:純 nonce 會把同頁第二筆不同領料誤吞為重複)
            idempotency_key=f"webissue:v2:{nonce}:{code}:{quantity.strip()}",
        )
    except (InventoryError, AuthorizationError, ArithmeticError, ValueError):
        # operator 無領料權(domain issue_to_asset 閘);表單在設備頁已隱藏,except 防 500。
        return RedirectResponse(url=f"/app/equipment/{eid}?issue=err", status_code=303)
    return RedirectResponse(
        url=f"/app/equipment/{eid}?issue={'ok' if ok else 'dup'}", status_code=303
    )


@router.post("/equipment/{asset_id}/issue/{txn_id}/update", response_model=None)
async def equipment_update_issue(
    asset_id: str,
    txn_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    quantity: str = Form(...),
    nonce: str = Form(...),
) -> RedirectResponse:
    """改設備直領數量(#9):差額連動庫存(補償帳)。冪等 nonce+payload。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.upper()
    try:
        await InventoryService(session).update_asset_issue_quantity(
            asset_id=eid, txn_id=txn_id, new_quantity=quantity,
            actor=Actor.human(user.user_id),
            idempotency_key=f"webissueupd:v1:{nonce}:{txn_id}:{quantity.strip()}",
        )
    except (InventoryError, AuthorizationError, ArithmeticError, ValueError):
        return RedirectResponse(url=f"/app/equipment/{eid}?issue=err", status_code=303)
    return RedirectResponse(url=f"/app/equipment/{eid}?issue=upd", status_code=303)


@router.post("/equipment/{asset_id}/issue/{txn_id}/cancel", response_model=None)
async def equipment_cancel_issue(
    asset_id: str,
    txn_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    nonce: str = Form(...),
) -> RedirectResponse:
    """取消設備直領(#9):RETURN 原量回庫(ledger 留帳)。冪等 = domain 決定性取消鍵。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    eid = asset_id.upper()
    try:
        await InventoryService(session).cancel_asset_issue(
            asset_id=eid, txn_id=txn_id, actor=Actor.human(user.user_id),
        )
    except (InventoryError, AuthorizationError, ArithmeticError, ValueError):
        return RedirectResponse(url=f"/app/equipment/{eid}?issue=err", status_code=303)
    return RedirectResponse(url=f"/app/equipment/{eid}?issue=cancelled", status_code=303)


@router.get("/suppliers", response_class=HTMLResponse, response_model=None)
async def supplier_search(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    q: str | None = Query(None),
    page: int = Query(1, ge=1),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """供應商查詢(讀取,開放,Slice 4):q = 機構名稱 ilike。無 q → 預設列最新 N 筆(可瀏覽)。

    `page` = 分頁(1-based,取代固定筆數上限);`msg` = adminonly banner(非 admin 誤入建立入口)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    svc = ContactsService(session)
    offset = (page - 1) * _PER_PAGE
    if q and q.strip():
        rows = await svc.list_organizations(search=q.strip(), limit=_PER_PAGE + 1, offset=offset)
    else:
        rows = await svc.list_organizations(limit=_PER_PAGE + 1, offset=offset)
    orgs, has_next = _paginate(rows, _PER_PAGE)
    pager_base = _pager_base("/app/suppliers", {"q": q})
    name = "partials/sup_list.html" if _is_htmx(request) else "suppliers.html"
    return _render(request, name, user.ui_locale, active="suppliers",
                   user=user, q=q, orgs=orgs, msg=msg,
                   page=page, has_next=has_next, pager_base=pager_base)


# ★ route 順序:/suppliers/new 兩路由必須宣告在 /suppliers/{org_id} 之前,
#   否則 "new" 會被當成 org_id 捕獲(比照 /equipment/new、/inventory/new)。
@router.get("/suppliers/new", response_class=HTMLResponse, response_model=None)
async def supplier_new_form(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """新增供應商表單(admin-only;org_id 由名稱自動導出)。

    非 admin → 回供應商清單(adminonly banner)。`msg` = 'err' → 重顯錯誤橫幅。
    org_type 受控 lookup 供下拉(預設 Supplier)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url="/app/suppliers?msg=adminonly", status_code=303)
    org_types = await ContactsService(session).list_org_types()
    return _render(request, "supplier_new.html", user.ui_locale, active="suppliers",
                   user=user, org_types=org_types, msg=msg, back_url="/app/suppliers")


@router.post("/suppliers/new", response_model=None)
async def supplier_create(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    name: str = Form(...),
    org_type: str = Form(""),
    website: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
) -> RedirectResponse:
    """建立新供應商(admin-only;domain 亦強制)。成功 → 轉新機構明細頁。

    無 nonce:org_id 由名稱決定性導出(單一 PK 冪等寫入),重複送出 → 命中重複檢查回 err(可接受)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url="/app/suppliers?msg=adminonly", status_code=303)
    try:
        org = await ContactsService(session).create_organization(
            actor=Actor.human(user.user_id),
            name=name, org_type=org_type.strip() or None,
            website=website, address=address, phone=phone,
        )
    except (ContactsError, AuthorizationError):
        return RedirectResponse(url="/app/suppliers/new?msg=err", status_code=303)
    return _sup_redirect(org.org_id, "saved")


@router.get("/suppliers/{org_id}", response_class=HTMLResponse, response_model=None)
async def supplier_detail(
    request: Request,
    org_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    msg: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """供應商明細:機構聯絡欄 + 人員清單 + admin 啟停。人員 PII(email/phone/mobile/address)僅
    admin 可見(06-contacts §3):engineer 走 PersonSummary(PII 不入 context),admin 走 PersonRead。

    `msg` = 啟停結果 banner(saved/err/adminonly)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    svc = ContactsService(session)
    org = await svc.get_organization(org_id)
    if org is None:
        return _render(request, "placeholder.html", user.ui_locale, active="suppliers",
                       user=user, screen_title=org_id)
    persons = await svc.list_org_persons(org_id)
    # PII gate:非 admin 只拿非 PII 摘要 → 聯絡資料連進不了模板 context(defense in depth)
    if user.role == "admin":
        people = [PersonRead.model_validate(p) for p in persons]
        org_types = await svc.list_org_types()
        categories = await svc.list_contact_categories()
    else:
        people = [PersonSummary.model_validate(p) for p in persons]
        org_types = []
        categories = []
    return _render(request, "supplier_detail.html", user.ui_locale, active="suppliers",
                   user=user, org=org, people=people, msg=msg, back_url="/app/suppliers",
                   org_types=org_types, categories=categories)


@router.post("/suppliers/{org_id}/active", response_model=None)
async def supplier_set_active(
    org_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    active: str = Form(...),
) -> RedirectResponse:
    """啟停供應商機構(admin-only;domain 亦強制)。`active` = 1 / 0。

    停用 = 資訊性標記(合約終止),**不阻擋 RFQ**(見 ContactsService.set_organization_active)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":  # route 先擋(domain assert_active_admin 縱深再擋)
        return RedirectResponse(url=f"/app/suppliers/{org_id}?msg=adminonly", status_code=303)
    try:
        await ContactsService(session).set_organization_active(
            org_id, active == "1", actor=Actor.human(user.user_id)
        )
    except (ContactsError, AuthorizationError):
        return RedirectResponse(url=f"/app/suppliers/{org_id}?msg=err", status_code=303)
    return RedirectResponse(url=f"/app/suppliers/{org_id}?msg=saved", status_code=303)


def _sup_redirect(org_id: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/app/suppliers/{org_id}?msg={msg}", status_code=303)


@router.post("/suppliers/{org_id}/update", response_model=None)
async def supplier_update(
    org_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    name: str = Form(...),
    org_type: str = Form(""),
    website: str = Form(""),
    address: str = Form(""),
    phone: str = Form(""),
) -> RedirectResponse:
    """供應商機構主檔編輯(#6a;admin-only,domain 亦強制)。org_id(代理鍵)唯讀不改。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":
        return _sup_redirect(org_id, "adminonly")
    try:
        await ContactsService(session).update_organization(
            org_id, actor=Actor.human(user.user_id),
            name=name, org_type=org_type, website=website, address=address, phone=phone,
        )
    except (ContactsError, AuthorizationError):
        return _sup_redirect(org_id, "err")
    return _sup_redirect(org_id, "saved")


@router.post("/suppliers/{org_id}/persons", response_model=None)
async def supplier_add_person(
    org_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    full_name: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    category: str = Form(""),
    email: str = Form(""),
    work_phone: str = Form(""),
    mobile: str = Form(""),
    extension: str = Form(""),
    work_address: str = Form(""),
    is_main: bool = Form(False),
) -> RedirectResponse:
    """admin 新增聯絡人(#6b;掛此 org)。PII 治理不變(明細限 admin)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":
        return _sup_redirect(org_id, "adminonly")
    try:
        await ContactsService(session).create_person(
            org_id=org_id, actor=Actor.human(user.user_id),
            full_name=full_name, first_name=first_name, last_name=last_name,
            category=category, email=email, work_phone=work_phone, mobile=mobile,
            extension=extension, work_address=work_address, is_main=is_main,
        )
    except (ContactsError, AuthorizationError):
        return _sup_redirect(org_id, "err")
    return _sup_redirect(org_id, "saved")


@router.post("/suppliers/{org_id}/persons/{person_id}/update", response_model=None)
async def supplier_update_person(
    org_id: str,
    person_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    full_name: str = Form(""),
    first_name: str = Form(""),
    last_name: str = Form(""),
    category: str = Form(""),
    email: str = Form(""),
    work_phone: str = Form(""),
    mobile: str = Form(""),
    extension: str = Form(""),
    work_address: str = Form(""),
    is_main: bool = Form(False),
) -> RedirectResponse:
    """admin 編輯聯絡人欄位(#6b)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role != "admin":
        return _sup_redirect(org_id, "adminonly")
    try:
        await ContactsService(session).update_person(
            person_id, actor=Actor.human(user.user_id),
            full_name=full_name, first_name=first_name, last_name=last_name,
            category=category, email=email, work_phone=work_phone, mobile=mobile,
            extension=extension, work_address=work_address, is_main=is_main,
        )
    except (ContactsError, AuthorizationError):
        return _sup_redirect(org_id, "err")
    return _sup_redirect(org_id, "saved")


@router.get("/pm", response_class=HTMLResponse, response_model=None)
async def pm_due(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    scope: str = Query("mine"),
    focus: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """保養排程:到期 / 逾期 PM(未 suppress),逾期在前。

    到期判定含**週末提前**(#5d):next_due 落週六/日 → 以其前的週五為有效日,故週五起就進清單。
    `scope` = mine(指派給我)/ all,同工單佇列;無指派名 → Mine 空,提示切 All。
    `focus` = 只看某一筆 PM(月曆/清單點入的詳情;含保養細項 + 補開工單鈕 + 「看全部」退路,#5c)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    today = _today()  # 台北今天(伺服器 UTC 直接 date.today() 會慢一天)
    mine = scope != "all"
    assignee = user.emaint_assignee if mine else None
    pm_svc = PmScheduleService(session)
    focused = False
    if focus:
        pm = await pm_svc.get_pm_schedule(focus)
        items = [pm] if pm is not None else []
        focused = pm is not None
    elif mine and not assignee:
        items = []
    else:
        # 週末提前:寬放到 today+2 撈候選,再以有效到期日(effective_generation_date)精篩
        candidates = await pm_svc.list_pm_schedules(
            due_on_or_before=today + timedelta(days=2), is_suppressed=False,
            assigned_person=assignee, limit=200,
        )
        items = [p for p in candidates if _pm_due_by(p.next_due_date, today)]
    items.sort(key=lambda p: p.next_due_date or today)  # 逾期(最舊到期日)在前
    task_svc = TaskService(session)
    task_names: dict[str, str] = {}
    for it in items:
        if it.task_id and it.task_id not in task_names:
            task = await task_svc.get_task(it.task_id)
            if task is not None:
                task_names[it.task_id] = task.description
    # 本期是否已生成(#5e:補開工單鈕只在「已到期且本期未生成」顯示)——last WO 非終態 = 已生成本期
    generated = await _generated_this_cycle(WorkOrderService(session), items)
    # 保養細項(#5c):focus 詳情才載入該 task 的步驟 + 每步用料 + 工項層級彙總(唯讀)
    steps: list = []
    step_parts: dict[int, list] = {}
    part_summary: dict[str, Decimal] = {}
    if focused and items and items[0].task_id:
        steps = await task_svc.get_task_steps(items[0].task_id)
        step_parts = await task_svc.get_parts_for_steps([s.id for s in steps])
        for plist in step_parts.values():
            for p in plist:
                part_summary[p.item_code] = part_summary.get(p.item_code, Decimal(0)) + (
                    p.replace_qty or Decimal(0)
                )
    return _render(request, "pm.html", user.ui_locale, active="pm",
                   user=user, items=items, task_names=task_names, today=today,
                   scope=("mine" if mine else "all"), has_assignee=bool(user.emaint_assignee),
                   focused=focused, generated=generated,
                   steps=steps, step_parts=step_parts, part_summary=part_summary)


@router.get("/pm/calendar", response_class=HTMLResponse, response_model=None)
async def pm_calendar(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    view: str = Query("month"),
    d: str | None = Query(None),
    scope: str = Query("mine"),
) -> HTMLResponse | RedirectResponse:
    """PM 行事曆(讀取):到期 PM(next_due_date)+ 已生成 PM 工單(opened_date)。

    `view` = month / week / day(格子越大顯示越多內容);`d` = 錨定日(ISO,預設台北今天);
    `scope` = mine/all(同清單)。點到期 PM → `/app/pm?focus=<pm_id>`(單項聚焦,非全清單)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    today = _today()  # 台北今天(伺服器 UTC 直接 date.today() 會慢一天)
    v = view if view in ("month", "week", "day") else "month"
    try:
        anchor = date.fromisoformat(d) if d else today
    except ValueError:
        anchor = today
    if v == "month":
        win_start = anchor.replace(day=1)
        win_end = date(
            anchor.year, anchor.month, calendar.monthrange(anchor.year, anchor.month)[1]
        )
        prev_d = (win_start - timedelta(days=1)).replace(day=1)
        next_d = win_end + timedelta(days=1)
        title = f"{anchor.year}-{anchor.month:02d}"
    elif v == "week":
        win_start = anchor - timedelta(days=(anchor.weekday() + 1) % 7)  # 週日起始(同表頭)
        win_end = win_start + timedelta(days=6)
        prev_d = win_start - timedelta(days=7)
        next_d = win_start + timedelta(days=7)
        title = f"{win_start.isoformat()} – {win_end.isoformat()}"
    else:  # day
        win_start = win_end = anchor
        prev_d = anchor - timedelta(days=1)
        next_d = anchor + timedelta(days=1)
        title = anchor.isoformat()

    mine = scope != "all"
    assignee = user.emaint_assignee if mine else None
    if mine and not assignee:
        due, generated = [], []
    else:
        due = await PmScheduleService(session).list_pm_schedules(
            due_on_or_after=win_start, due_on_or_before=win_end,
            is_suppressed=False, assigned_person=assignee, limit=500,
        )
        generated = await WorkOrderService(session).list_work_orders(
            work_type="PM", opened_on_or_after=win_start, opened_on_or_before=win_end,
            assigned_person=assignee, limit=500,
        )
    due_by_date: dict[date, list] = {}
    for pm in due:
        if pm.next_due_date:
            due_by_date.setdefault(pm.next_due_date, []).append(pm)
    gen_by_date: dict[date, list] = {}
    for wo in generated:
        if wo.opened_date:
            gen_by_date.setdefault(wo.opened_date, []).append(wo)
    task_svc = TaskService(session)
    task_names: dict[str, str] = {}
    for pm in due:
        if pm.task_id and pm.task_id not in task_names:
            task = await task_svc.get_task(pm.task_id)
            if task is not None:
                task_names[pm.task_id] = task.description
    # month:整月格網(date 物件,含鄰月補格);week:7 天;day:單日
    month_weeks = (
        calendar.Calendar(firstweekday=6).monthdatescalendar(anchor.year, anchor.month)
        if v == "month" else []
    )
    week_days = [win_start + timedelta(days=i) for i in range(7)] if v == "week" else []
    # 本期已生成集合(#5e:日視圖「補開工單」鈕只在已到期〔含週末提前〕且本期未生成時顯示)
    generated = await _generated_this_cycle(WorkOrderService(session), due)
    return _render(
        request, "pm_calendar.html", user.ui_locale, active="pm", user=user,
        view=v, anchor=anchor, title=title, prev_d=prev_d, next_d=next_d,
        month_weeks=month_weeks, week_days=week_days,
        due_by_date=due_by_date, gen_by_date=gen_by_date, generated=generated,
        task_names=task_names, today=today, scope=("mine" if mine else "all"),
        has_assignee=bool(user.emaint_assignee), back_url="/app/pm",
    )


@router.get("/pm/{pm_id}/backfill", response_class=HTMLResponse, response_model=None)
async def pm_backfill_confirm(
    request: Request,
    pm_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> HTMLResponse | RedirectResponse:
    """補開工單確認頁(#5b;GET 零寫入):顯示 PM 資訊 + 可編輯 owner 欄(預填 assigned_person)
    + 取消 / 確認送出。送出 POST 到 /generate(帶 assigned_person override)。

    若該 PM 已非「到期且本期未生成」(重複視窗)→ redirect 回 /app/pm(照現有錯誤處理風格)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無 PM 補開權
        return RedirectResponse(url="/app/pm", status_code=303)
    pm_svc = PmScheduleService(session)
    pm = await pm_svc.get_pm_schedule(pm_id)
    if pm is None:
        return RedirectResponse(url="/app/pm", status_code=303)
    today = _today()
    # 本期已生成(冪等命中)→ 不再顯示補開確認頁,直接回清單
    generated = await _generated_this_cycle(WorkOrderService(session), [pm])
    if pm.pm_id in generated or not _pm_due_by(pm.next_due_date, today):
        return RedirectResponse(url="/app/pm", status_code=303)
    task_name = None
    if pm.task_id:
        task = await TaskService(session).get_task(pm.task_id)
        task_name = task.description if task is not None else None
    return _render(request, "pm_backfill.html", user.ui_locale, active="pm",
                   user=user, pm=pm, task_name=task_name, today=today,
                   back_url="/app/pm")


@router.post("/pm/{pm_id}/generate", response_model=None)
async def pm_generate(
    pm_id: str,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    assigned_person: str = Form(""),
) -> RedirectResponse:
    """對到期 PM 按「執行」→ 按需生成 PM 工單(ADR-021),actor = 登入者,轉進其詳情。

    `assigned_person`(補開工單確認頁送入)= owner override;空 → None(沿用 pm.assigned_person)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if user.role == "operator":  # route 先擋;operator 無 PM 生成權
        return RedirectResponse(url="/app/pm", status_code=303)
    actor = Actor.human(user.user_id)
    try:
        wo = await WorkOrderService(session).generate_pm_work_order(
            pm_id=pm_id, actor=actor, assigned_person=assigned_person.strip() or None
        )
    except (WorkOrderError, AuthorizationError):
        return RedirectResponse(url="/app/pm", status_code=303)
    # Slice B:PM 開單通知已於同交易 enqueue → 立即背景 flush(送 email/telegram)。
    background.add_task(_flush_notify_outbox_bg)
    return RedirectResponse(url=f"/app/work-orders/{wo.work_order_no}", status_code=303)


# ---- 自動完成(全站 data-suggest 欄位的資料源;讀取,開放)----

@router.get("/suggest", response_class=HTMLResponse, response_model=None)
async def suggest(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    kind: str = Query(...),
    q: str = Query(""),
) -> HTMLResponse:
    """輸入即建議(static/app.js 的 data-suggest 元件呼叫):回 <button> 清單 fragment。

    kind = asset(EID+名稱)/ part(料號+品名)/ person(歷史指派名)/ task(保養任務)
    / org(供應商機構,value=org_id)/ supplier(供應商名稱,value=name + extra=org_id)。
    輸入 "0038" 即建議 "EID-70004"(substring ilike)。未登入回空。
    """
    q = q.strip()
    if user is None or not q:
        return HTMLResponse("")
    options: list[dict[str, str]] = []  # value=填入值;label/hint=顯示
    if kind == "asset":
        for a in await AssetService(session).list_assets(search=q, limit=8):
            options.append({"value": a.asset_id, "label": a.asset_id, "hint": a.description})
    elif kind == "part":
        for i in await InventoryService(session).list_items(search=q, limit=8):
            options.append(
                {"value": i.item_code, "label": i.item_code, "hint": i.name or i.description or ""}
            )
    elif kind == "person":
        for n in await WorkOrderService(session).list_assignee_suggestions(q):
            options.append({"value": n, "label": n, "hint": ""})
    elif kind == "task":
        for tk in await TaskService(session).list_tasks(search=q, limit=8):
            options.append({"value": tk.task_no, "label": tk.task_no, "hint": tk.description})
    elif kind == "org":
        for o in await ContactsService(session).list_organizations(search=q, limit=8):
            options.append({"value": o.org_id, "label": o.name, "hint": o.org_id})
    elif kind == "supplier":
        # 供應商欄自動完成(#7b/#7g):填入的 value = 機構名稱(display);extra = org_id 帶入
        # 唯讀的「供應商機構代碼」欄(app.js data-fill),使用者不需手打代碼。
        for o in await ContactsService(session).list_organizations(search=q, limit=8):
            options.append(
                {"value": o.name, "label": o.name, "hint": o.org_id, "extra": o.org_id}
            )
    return _render(request, "partials/suggest.html", user.ui_locale, user=user, options=options)


# ---- 助理(ADR-020:dock / FAB → Hermes gateway 實接;對話落 DB,跨頁持久 + 多 session)----

async def _render_assistant_panel(
    request: Request,
    session: AsyncSession,
    user: UserAccount,
    current_id: int | None,
) -> HTMLResponse:
    """渲染 dock 內容 partial(切換列 + 當前對話訊息 + 表單 + 結束鈕),並同步當前對話 cookie。

    `current_id` 有值 → set cookie(整頁導覽後自動還原);None(新對話 / 全結束)→ 刪 cookie。
    訊息經安全渲染器(assistant_render;user 訊息維持純 escape)。
    """
    asvc = AssistantService(session)
    conversations = await asvc.list_open_conversations(user.user_id)
    messages = await asvc.get_messages(user.user_id, current_id) if current_id else []
    resp = _render(
        request, "partials/assistant_panel.html", user.ui_locale, user=user,
        conversations=conversations, messages=messages, current_id=current_id or "",
    )
    if current_id:
        resp.set_cookie(
            _ASSISTANT_CONV_COOKIE, str(current_id),
            max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax", path="/",
        )
    else:
        resp.delete_cookie(_ASSISTANT_CONV_COOKIE, path="/")
    return resp


async def _resolve_current_conversation(
    asvc: AssistantService, user_id: str, cookie_val: str | None
) -> int | None:
    """決定當前對話:cookie(有效 + 開啟中 + 本人)否則最近活動的開啟中對話,否則 None。"""
    if cookie_val and cookie_val.isdigit():
        conv = await asvc.get_conversation(user_id, int(cookie_val))
        if conv is not None and conv.closed_at is None:
            return conv.id
    open_convs = await asvc.list_open_conversations(user_id)
    return open_convs[0].id if open_convs else None


@router.get("/assistant/panel", response_class=HTMLResponse, response_model=None)
async def assistant_panel(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    new: bool = Query(False),
) -> HTMLResponse:
    """dock 內容 lazy-load 端點(base.html hx-get load 觸發)。

    未登入 → 空殼(不炸);`new=1` → 空白新對話態(不落 DB、刪 cookie)。
    否則還原 cookie 指的當前對話(或最近開啟中對話)—— 修「整頁導覽對話全滅」重大缺失。
    """
    if user is None:
        return HTMLResponse("")  # 未登入:空殼(hx-get 不做轉址)
    if new:
        return await _render_assistant_panel(request, session, user, None)
    current_id = await _resolve_current_conversation(
        AssistantService(session), user.user_id, request.cookies.get(_ASSISTANT_CONV_COOKIE)
    )
    return await _render_assistant_panel(request, session, user, current_id)


@router.get("/assistant/{conversation_id}", response_class=HTMLResponse, response_model=None)
async def assistant_switch(
    conversation_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> HTMLResponse:
    """切換到某對話(本人 + 開啟中);無效 / 已結束 → 退回最近開啟中對話或空白態。"""
    if user is None:
        return HTMLResponse("")
    asvc = AssistantService(session)
    conv = await asvc.get_conversation(user.user_id, conversation_id)
    if conv is not None and conv.closed_at is None:
        current_id: int | None = conv.id
    else:  # 無效 / 非本人 / 已結束 → 退回最近開啟中
        open_convs = await asvc.list_open_conversations(user.user_id)
        current_id = open_convs[0].id if open_convs else None
    return await _render_assistant_panel(request, session, user, current_id)


@router.post("/assistant/{conversation_id}/close", response_class=HTMLResponse, response_model=None)
async def assistant_close(
    conversation_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> HTMLResponse:
    """結束對話(closed_at;本人才動)→ 回 panel(切到下一個開啟中或空白態)。"""
    if user is None:
        return HTMLResponse("")
    asvc = AssistantService(session)
    # 非本人 / 不存在 → 忽略,單純重渲染(擁有權在 domain 強制,route 不需再判)
    with contextlib.suppress(AssistantError):
        await asvc.close_conversation(user.user_id, conversation_id, Actor.human(user.user_id))
    open_convs = await asvc.list_open_conversations(user.user_id)
    current_id = open_convs[0].id if open_convs else None
    return await _render_assistant_panel(request, session, user, current_id)


@router.post("/assistant", response_class=HTMLResponse, response_model=None)
async def assistant_chat(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    message: str = Form(...),
    conversation_id: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    """兩段式送出 · phase 1(快,不打 gateway):落 user 訊息 → 回觸發器 partial。

    `conversation_id` 空 = 首則(惰性建會話)。**先落 DB + set cookie**(修「送出後、
    Hermes 回覆前轉跳 = 整輪蒸發」:in-flight 窗內伺服器上已有會話 + user 訊息)。回
    `assistant_turn.html`:user 泡泡 + pending 觸發器(hx-trigger=load → phase 2)+ OOB
    重渲染切換列(修「新會話標籤不出現」)+ OOB 更新 hidden conversation_id。
    誠實降級:未配置 → 「尚未啟用」;開啟中對話超限 → 友善提示(皆不建 user 訊息)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    msg = message.strip()
    conv_id: int | None = (
        int(conversation_id) if conversation_id.strip().isdigit() else None
    )
    settings = get_settings()
    asvc = AssistantService(session)

    def _sys(error: str | None) -> HTMLResponse:
        # phase 1 誠實系統泡泡(不建 user 訊息、不打 gateway):disabled / limit / 空訊息
        return _render(
            request, "partials/assistant_reply.html", user.ui_locale, user=user,
            reply_html=None, error=error, retry_id=None, conversation_id="",
        )

    if not settings.hermes_configured:  # 未配置 → 誠實「尚未啟用」
        return _sys("disabled")
    if not msg:  # 空訊息:不做事
        return _sys(None)

    # 既有 conversation_id 驗擁有權 + 開啟中;無效 → 當新對話(conv_id=None)
    if conv_id is not None:
        conv = await asvc.get_conversation(user.user_id, conv_id)
        if conv is None or conv.closed_at is not None:
            conv_id = None

    # 新對話先套開啟中上限(fail fast → 友善提示;add_user_message 亦守門)
    if conv_id is None and (
        await asvc.count_open_conversations(user.user_id)
        >= AssistantService.MAX_OPEN_CONVERSATIONS
    ):
        return _sys("limit")

    try:  # phase 1:落 user 訊息(惰性建會話);上限競態 → 友善提示
        conv, umsg = await asvc.add_user_message(
            user_id=user.user_id, conversation_id=conv_id,
            content=msg, actor=Actor.human(user.user_id),
        )
    except AssistantError:
        return _sys("limit")

    conversations = await asvc.list_open_conversations(user.user_id)
    resp = _render(
        request, "partials/assistant_turn.html", user.ui_locale, user=user,
        user_content=msg, message_id=umsg.id, conversation_id=conv.id,
        conversations=conversations, current_id=conv.id, oob=True,
    )
    resp.set_cookie(  # 即刻記住當前對話 → 轉跳回來停在此會話(不落舊會話)
        _ASSISTANT_CONV_COOKIE, str(conv.id),
        max_age=_SESSION_MAX_AGE, httponly=True, samesite="lax", path="/",
    )
    return resp


@router.post(
    "/assistant/{conversation_id}/reply", response_class=HTMLResponse, response_model=None
)
async def assistant_reply(
    conversation_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    message_id: int = Form(...),
) -> HTMLResponse | RedirectResponse:
    """兩段式送出 · phase 2(慢,打 gateway):由 phase 1 的 pending 元素 hx-trigger=load 觸發。

    取 message_id 指的 user 訊息(驗屬此會話、role=user)→ gateway 回覆 → 落 assistant 訊息。
    冪等/重放守門:該 user 訊息之後若已有 assistant 訊息(重送 / back-forward)→ 直接回渲染
    既存回覆,**不重打 gateway**。失敗 → 錯誤泡泡 + 重試鈕(user 訊息保留、不刪、可重送)。
    治理(ADR-019 沙箱):scoped token 只進 gateway body,**絕不入回應 HTML / server log**。
    以 hx-swap=outerHTML 換掉 pending 元素(回應即單一泡泡)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    settings = get_settings()
    asvc = AssistantService(session)

    def _bubble(
        *, reply_html: str | None = None, error: str | None = None, retry_id: int | None = None
    ) -> HTMLResponse:
        return _render(
            request, "partials/assistant_reply.html", user.ui_locale, user=user,
            reply_html=reply_html, error=error, retry_id=retry_id,
            conversation_id=conversation_id,
        )

    if not settings.hermes_configured:  # 防禦:panel 未啟用即不渲染,但仍誠實回應
        return _bubble(error="disabled")

    # 會話 / user 訊息擁有權 + 開啟中(非本人 / 已結束 / 找不到 → 錯誤泡泡,不洩漏、不重試)
    conv = await asvc.get_conversation(user.user_id, conversation_id)
    if conv is None or conv.closed_at is not None:
        return _bubble(error="unavailable")
    umsg = await asvc.get_message(user.user_id, message_id)
    if umsg is None or umsg.conversation_id != conversation_id or umsg.role != "user":
        return _bubble(error="unavailable")

    # 冪等/重放守門:該 user 訊息之後第一則已是 assistant → 回既存回覆,不重打 gateway
    nxt = await asvc.next_message_after(user.user_id, conversation_id, message_id)
    if nxt is not None and nxt.role == "assistant":
        return _bubble(reply_html=render_reply(nxt.content))

    history = _cap_history(
        await asvc.recent_history(
            user.user_id, conversation_id, before_message_id=message_id
        )
    )

    # 由現有 web session 現鑄短命 scoped token(身分=登入者,ADR-020 決策 5)
    try:
        token = await IdentityService(session).mint_scoped_token(
            session_token=request.cookies.get(_SESSION_COOKIE),
            agent="hermes", scope="pilot",
        )
    except IdentityError:  # session 失效 → 回登入(不外洩細節)
        return RedirectResponse(url=_LOGIN, status_code=307)

    payload = {
        "message": umsg.content,
        "scoped_token": token,  # ★ 只進 gateway body,永不回前端 / log
        "history": history,
        "locale": user.ui_locale,
        # 需求 ③(ADR-020/023):Hermes 生成寫進 Jira MRQ 的內容(summary/description)用此語言,
        # 與 UI 回覆語言(locale)分離、各自持久化(user_account.jira_output_locale)。
        "jira_locale": user.jira_output_locale,
    }
    try:
        async with httpx.AsyncClient(timeout=_ASSISTANT_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                settings.hermes_gateway_url.rstrip("/") + "/chat",
                headers={"X-Hermes-Secret": settings.hermes_gateway_secret},
                json=payload,
            )
        resp.raise_for_status()
        reply = str((resp.json() or {}).get("reply") or "").strip()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        # 錯誤細節進 log(token 不在 exc:它在 body,httpx 例外不回放 request body)
        _logger.warning("assistant gateway call failed: %s: %s", type(exc).__name__, exc)
        return _bubble(error="unavailable", retry_id=message_id)  # user 訊息保留,可重送
    if not reply:  # gateway 回空 → 暫時無法回應(不落 DB、可重送)
        return _bubble(error="unavailable", retry_id=message_id)

    try:  # 成功:落 assistant 訊息(會話競態關閉 → 當暫時無法回應)
        await asvc.add_assistant_message(
            user_id=user.user_id, conversation_id=conversation_id,
            content=reply, actor=Actor.human(user.user_id),
        )
    except AssistantError:
        return _bubble(error="unavailable", retry_id=message_id)

    return _bubble(reply_html=render_reply(reply))


@router.get("/set-locale")
async def set_locale(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    locale: str = Query(...),
    next: str = Query(_HOME),
) -> RedirectResponse:
    """切換 UI 語言(ADR-023)。已登入 → 寫回 user_account.ui_locale;未登入 → cookie 暫存。"""
    chosen = locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE
    target = next if next.startswith("/app") else _HOME  # 防 open-redirect
    resp = RedirectResponse(url=target, status_code=303)
    if user is not None:
        await IdentityService(session).set_locale(
            user.user_id, actor=Actor.human(user.user_id), ui_locale=chosen
        )
    else:
        resp.set_cookie(
            _LOCALE_COOKIE, chosen,
            max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax", path="/",
        )
    return resp


# ---- 個人設定(所有登入者;非 admin-gated)----

@router.get("/settings", response_class=HTMLResponse, response_model=None)
async def settings_page(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    changed: bool = Query(False),
    pat: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """個人設定:改密碼 / 語言(走 /app/set-locale)/ 自己的指派名 / Jira PAT(ADR-022 §5 vault)。

    PAT 區只讀 metadata(system/label/last_used_at;list_credentials 不需主鑰、不外洩密文)。
    `pat` = 存/撤結果 banner(saved/revoked/keyunset/keyinvalid/empty/error)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    return await _render_settings(request, session, user, changed=changed, pat=pat)


async def _render_settings(
    request: Request,
    session: AsyncSession,
    user: UserAccount,
    *,
    changed: bool = False,
    pat: str | None = None,
    tg_code: str | None = None,
) -> HTMLResponse:
    """組設定頁 context(PAT metadata + Telegram 綁定狀態 + bot 名)並渲染。

    `tg_code` 有值 = 剛產生的綁定碼明文,直接內嵌顯示一次(絕不進 URL / redirect / log)。
    """
    pat_creds = await CredentialVault(session).list_credentials(user.user_id)
    tg_link = await TelegramBridgeService(session).get_link(user.user_id)
    return _render(
        request, "settings.html", user.ui_locale, active="settings",
        user=user, pw_error=None, changed=changed, assignee=user.emaint_assignee,
        pat_creds=pat_creds, pat=pat, back_url=_HOME,
        tg_link=tg_link, tg_code=tg_code,
        bot_username=get_settings().telegram_bot_username,
    )


@router.post("/settings/telegram/code", response_class=HTMLResponse, response_model=None)
async def settings_telegram_code(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> HTMLResponse | RedirectResponse:
    """產生一次性 Telegram 綁定碼(TTL 10 分)。**直接渲染設定頁**顯示明文碼 —— 碼只顯示一次,
    絕不進 URL query / redirect / log(比照 PAT 明文永不回顯的紅線)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    try:
        code = await TelegramBridgeService(session).create_link_code(
            user.user_id, Actor.human(user.user_id)
        )
    except TelegramBridgeError:  # 帳號 inactive 等(理論上登入者不會遇到)→ 誠實回設定頁
        return await _render_settings(request, session, user)
    return await _render_settings(request, session, user, tg_code=code)


@router.post("/settings/telegram/unlink", response_model=None)
async def settings_telegram_unlink(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> RedirectResponse:
    """解除本人 Telegram 綁定(冪等)。之後需重新產生綁定碼才能再綁。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    await TelegramBridgeService(session).unlink(user.user_id, Actor.human(user.user_id))
    return RedirectResponse(url="/app/settings", status_code=303)


@router.get("/proposals", response_class=HTMLResponse, response_model=None)
async def my_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    page: int = Query(1, ge=1),
) -> HTMLResponse | RedirectResponse:
    """「我的提案」(唯讀):列登入者本人提出的提案(所有狀態),新到舊、分頁。

    提案人透過此頁得知 admin 審核進度(PENDING / CONFIRMED / REJECTED / EXPIRED)——
    先前提案只有 admin 在 /admin/proposals 看得到,提案人無從得知結果。零寫入:逾期仍
    PENDING 者以 `expires_at` 對現在時間顯示「已逾期」,不改 DB(sweep 屬 admin 頁職責)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    proposed_by = Actor.human(user.user_id).value  # human:<user_id>
    rows = await WorkOrderService(session).list_proposals_by_proposer(
        proposed_by=proposed_by, limit=_PER_PAGE + 1, offset=(page - 1) * _PER_PAGE
    )
    proposals, has_next = _paginate(rows, _PER_PAGE)
    pager_base = _pager_base("/app/proposals", {})
    return _render(
        request, "proposals.html", user.ui_locale, active="",
        user=user, proposals=proposals, now=datetime.now(UTC),
        page=page, has_next=has_next, pager_base=pager_base, back_url="/app/settings",
    )


@router.post("/settings/pat", response_model=None)
async def settings_store_pat(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    pat_secret: str = Form(...),
) -> RedirectResponse:
    """存/換發本人 Jira PAT(ADR-022 §5:封套加密、明文不入庫/log、限本人)。

    一人一支(#8;Jordan 2026-07-05):`store_credential` 已在**同一交易**先撤既有現行者、再存新的
    → 存第二支即「更換」(舊撤新立原子)。標籤欄移除(system 固定 'jira')。
    錯誤分流(banner):主鑰未設 → keyunset;主鑰有設但格式無效(誤用 token_urlsafe)→ keyinvalid;
    空輸入 → empty;其餘 vault 錯 → error。皆不明文暫存。redirect 一律帶 `#pat` fragment
    (PAT 區在長頁下方;無錨點時 303 後捲回頁頂,banner 落在 fold 以下第一眼看不到)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    if not pat_secret.strip():
        return RedirectResponse(url="/app/settings?pat=empty#pat", status_code=303)
    try:
        await CredentialVault(session).store_credential(
            user_id=user.user_id,
            system="jira",
            secret=pat_secret.strip(),
            actor=Actor.human(user.user_id),
            label=None,
        )
    except VaultKeyUnset:
        return RedirectResponse(url="/app/settings?pat=keyunset#pat", status_code=303)
    except VaultKeyInvalid:
        return RedirectResponse(url="/app/settings?pat=keyinvalid#pat", status_code=303)
    except VaultError:
        return RedirectResponse(url="/app/settings?pat=error#pat", status_code=303)
    return RedirectResponse(url="/app/settings?pat=saved#pat", status_code=303)


@router.post("/settings/pat/revoke", response_model=None)
async def settings_revoke_pat(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> RedirectResponse:
    """撤本人現行 Jira PAT(即時失效;冪等)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    await CredentialVault(session).revoke(
        user_id=user.user_id, system="jira", actor=Actor.human(user.user_id)
    )
    return RedirectResponse(url="/app/settings?pat=revoked#pat", status_code=303)


@router.post("/settings/password", response_model=None)
async def settings_change_password(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    current_password: str = Form(...),
    new_password: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """自助改密碼:驗舊 → 重雜湊(change_password)。錯誤以在地化訊息重渲染。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    try:
        await IdentityService(session).change_password(
            user.user_id, current_password, new_password, actor=Actor.human(user.user_id)
        )
    except AuthenticationError:
        return _render(request, "settings.html", user.ui_locale, active="settings", user=user,
                       pw_error="settings.password.wrong", changed=False,
                       assignee=user.emaint_assignee, pat_creds=[], pat=None)
    except IdentityError:
        return _render(request, "settings.html", user.ui_locale, active="settings", user=user,
                       pw_error="settings.password.short", changed=False,
                       assignee=user.emaint_assignee, pat_creds=[], pat=None)
    return RedirectResponse(url="/app/settings?changed=1", status_code=303)


@router.post("/settings/assignee", response_model=None)
async def settings_set_assignee(
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    assignee: str = Form(""),
) -> RedirectResponse:
    """設自己的 eMaint 指派名(供「我的工單/保養」過濾)。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    await IdentityService(session).set_emaint_assignee(
        user.user_id, assignee=assignee, actor=Actor.human(user.user_id)
    )
    return RedirectResponse(url="/app/settings", status_code=303)


# ---- 說明中心(/app/help;SOP 說明,需登入、不分角色)----

# SOP 回饋收件人 = 系統負責人(內部規格)。將來要配置化再抽 config;現階段常數即可。
_HELP_FEEDBACK_TO = "maintenance@example.com"


@router.get("/help", response_class=HTMLResponse, response_model=None)
async def help_index(
    request: Request,
    user: UserAccount | None = Depends(get_current_user),
    fb: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """SOP 說明清單:每份一列(標題 + 濃縮步驟 + 更新日)連到內頁 + 底部回饋表單。

    `fb` = 回饋結果 banner(ok / empty / smtp / send)。所有登入者可看(operator 亦可)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    return _render(
        request, "help.html", user.ui_locale, active="help", user=user,
        docs=HELP_DOCS, fb=fb, back_url=_HOME,
    )


@router.post("/help/feedback", response_model=None)
async def help_feedback(
    user: UserAccount | None = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    message: str = Form(""),
) -> RedirectResponse:
    """SOP 回饋:登入者留言「想要什麼 SOP」→ **落 DB(主)** + email 盡力通知(副)。

    email 曾送達延遲(內部規格)→ 改 DB 為主:`FeedbackService.create` 成功即 ok banner
    (回饋顯示於 `/admin/proposals` 同頁)。空留言 → empty。落庫成功後**盡力**寄信:SMTP
    未配置直接跳過(不再回 smtp banner);寄信失敗只 log warning、不影響 ok banner。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    body = message.strip()
    if not body:
        return RedirectResponse(url="/app/help?fb=empty", status_code=303)
    actor = Actor.human(user.user_id)
    try:
        await FeedbackService(session).create(user.user_id, body, actor)
    except FeedbackError:
        # strip 後空 / 超長:視同空留言提示(前端 rows=3 一般不會超長)
        return RedirectResponse(url="/app/help?fb=empty", status_code=303)
    # ---- 落庫成功後盡力通知(email 為副:未配置跳過、失敗只 log)----
    if smtp_configured():
        s = get_settings()
        from_addr = s.notify_from or s.rfq_from or s.smtp_username or ""
        now = datetime.now(TAIPEI).strftime("%Y-%m-%d %H:%M")
        text = (
            f"{body}\n\n"
            f"---\n"
            f"來自:{user.display_name} ({user.user_id})\n"
            f"時間:{now} (台北)\n"
        )
        try:
            await get_email_sender().send(
                to=_HELP_FEEDBACK_TO,
                subject=f"[CMMS 說明回饋] {user.display_name}",
                body=text,
                from_addr=from_addr,
                reply_to=s.rfq_reply_to,
            )
        except EmailError:
            _logger.warning("help feedback email notify failed (DB record persisted)")
    return RedirectResponse(url="/app/help?fb=ok", status_code=303)


@router.get("/help/{slug}", response_class=HTMLResponse, response_model=None)
async def help_doc_page(
    request: Request,
    slug: str,
    user: UserAccount | None = Depends(get_current_user),
) -> HTMLResponse | RedirectResponse:
    """SOP 內頁:殼 + include 對應片段模板。未知 slug → 404。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    doc = get_help_doc(slug)
    if doc is None:
        raise HTTPException(status_code=404, detail="unknown help doc")
    return _render(
        request, "help_doc.html", user.ui_locale, active="help", user=user,
        doc=doc, back_url="/app/help",
    )


# ---- 匯出中心(/app/export;需登入,admin+engineer 皆可;唯讀批次下載 CSV)----

async def _export_options_map(
    session: AsyncSession, spec, locale: str
) -> dict[str, list[tuple[str, str]]]:
    """為當前資料集的 chips 過濾器解析選項 → (value, 顯示文字)。

    動態來源(work_type/asset_type/frequency_unit)讀受控 lookup 的 label;靜態(狀態 chips)
    翻其 label_key。tristate 選項在模板端直接讀 f.options(不入此表)。
    """
    svc = ExportService(session)
    out: dict[str, list[tuple[str, str]]] = {}
    for f in spec.filters:
        if f.kind != "chips":
            continue
        if f.options_source:
            out[f.key] = await svc.lookup_options(f.options_source)
        else:
            out[f.key] = [(val, translate(lkey, locale)) for val, lkey in f.options]
    return out


@router.get("/export", response_class=HTMLResponse, response_model=None)
async def export_home(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
    ds: str | None = Query(None),
) -> HTMLResponse | RedirectResponse:
    """匯出中心:五張資料集卡片 + 當前資料集(ds)的過濾表單。無效 ds → 預設第一個。"""
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    slug = ds if ds in DATASETS else next(iter(DATASETS))
    spec = DATASETS[slug]
    options_map = await _export_options_map(session, spec, user.ui_locale)
    return _render(
        request, "export.html", user.ui_locale, active="export", user=user,
        datasets=DATASETS, spec=spec, options_map=options_map, back_url=_HOME,
    )


@router.get("/export/{slug}/preview", response_class=HTMLResponse, response_model=None)
async def export_preview(
    request: Request,
    slug: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> HTMLResponse | RedirectResponse:
    """試算(HTMX partial):符合筆數 + 前 5 列預覽(欄=依角色過濾的實際匯出欄)+ 下載連結。

    過濾參數非法(壞日期)→ 誠實 banner(不 500)。未知 slug → 404。欄位級 RBAC:
    `visible_columns` 已剔除非 admin 不可見欄,預覽與下載共用同一份欄。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    spec = DATASETS.get(slug)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown export dataset")
    columns = visible_columns(spec, user.role == "admin")
    try:
        filters = spec.parse(request.query_params)
    except ExportFilterError:
        return _render(
            request, "partials/export_preview.html", user.ui_locale, user=user,
            error="baddate", columns=columns, rows=[], count=0, download_url="",
        )
    svc = ExportService(session)
    count = await getattr(svc, f"count_{slug}")(**filters)
    rows = await getattr(svc, f"rows_{slug}")(limit=5, **filters)
    qs = request.url.query
    download_url = f"/app/export/{slug}/download" + (f"?{qs}" if qs else "")
    return _render(
        request, "partials/export_preview.html", user.ui_locale, user=user,
        error=None, columns=columns, rows=rows, count=count, download_url=download_url,
    )


@router.get("/export/{slug}/download", response_model=None)
async def export_download(
    request: Request,
    slug: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount | None = Depends(get_current_user),
) -> StreamingResponse | RedirectResponse:
    """串流 CSV(無筆數上限;utf-8-sig BOM + 台北時戳檔名)。未知 slug → 404;壞過濾 → 400。

    欄位級 RBAC:非 admin 的 CSV 不含 `admin_only` 欄(與預覽同一份 `visible_columns`)。
    """
    if user is None:
        return RedirectResponse(url=_LOGIN, status_code=307)
    spec = DATASETS.get(slug)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown export dataset")
    columns = visible_columns(spec, user.role == "admin")
    try:
        filters = spec.parse(request.query_params)
    except ExportFilterError as e:
        raise HTTPException(status_code=400, detail="invalid filter") from e
    rows = await getattr(ExportService(session), f"rows_{slug}")(limit=None, **filters)
    filename = csv_filename(slug)
    return StreamingResponse(
        stream_csv(columns, rows, user.ui_locale),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
