"""管理台 web 路由(ADR-019 雙表面 + ADR-022 RBAC)。掛 `/admin`,全數 require_admin。

RBAC 兩層(縱深):web `require_admin`(擋非 admin 進頁)+ domain service `_assert_admin`
(寫入路徑強制,非只藏按鈕,ADR-022 決策 4)。帳號管理(建 / 停用 / 復用 / 角色 / 重設密碼 /
指派名);PM 大項目 = 任務範本 + 細項步驟/用料 + 排程(串 EID / 週期 / 到期 / 負責人)全 CRUD。
"""

from __future__ import annotations

from contextlib import suppress
from datetime import date
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.audit import Actor
from cmms.domain.asset.service import AssetError, AssetService
from cmms.domain.attachment.service import AttachmentError, AttachmentService
from cmms.domain.contacts.service import ContactsService
from cmms.domain.failure_vocab.service import FailureVocabService
from cmms.domain.feedback.service import FeedbackError, FeedbackService
from cmms.domain.identity.models import UserAccount
from cmms.domain.identity.service import AuthorizationError, IdentityError, IdentityService
from cmms.domain.identity.vault import CredentialVault
from cmms.domain.inventory.service import InventoryError, InventoryService
from cmms.domain.notify.service import NotificationService, NotifyError
from cmms.domain.pm_schedule.service import PmScheduleError, PmScheduleService
from cmms.domain.task.service import TaskError, TaskService
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService
from cmms.web.routes import _LOGIN, _is_htmx, _render, get_current_user

router = APIRouter(prefix="/admin", tags=["admin"])


async def require_admin(user: UserAccount | None = Depends(get_current_user)) -> UserAccount:
    """admin 面守門:未登入 → 轉登入(307);登入非 admin → 403。"""
    if user is None:
        raise HTTPException(status_code=307, headers={"Location": _LOGIN})
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _accounts_redirect(err: str | None = None) -> RedirectResponse:
    """回帳號清單;有錯(如末位 admin 守門)→ 帶 err 供頁面 banner 顯示。"""
    url = "/admin/accounts" + (f"?err={quote(err)}" if err else "")
    return RedirectResponse(url=url, status_code=303)


@router.get("", response_class=HTMLResponse, response_model=None)
@router.get("/", response_class=HTMLResponse, response_model=None)
async def admin_home(user: UserAccount = Depends(require_admin)) -> RedirectResponse:
    return RedirectResponse(url="/admin/accounts", status_code=307)


@router.get("/pm", response_class=HTMLResponse, response_model=None)
async def admin_pm(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    q: str | None = None,
) -> HTMLResponse:
    """PM 大項目:保養任務清單(輸入即過濾:代碼/描述)→ 點進編輯;可新增大項目。"""
    tasks = await TaskService(session).list_tasks(search=q, limit=200)
    name = "partials/admin_task_list.html" if _is_htmx(request) else "admin_pm.html"
    return _render(request, name, user.ui_locale, active="pm",
                   user=user, tasks=tasks, q=q)


def _pm_task_redirect(task_no: str, err: str | None = None) -> RedirectResponse:
    url = f"/admin/pm/{task_no}" + (f"?err={quote(err)}" if err else "")
    return RedirectResponse(url=url, status_code=303)


@router.post("/pm", response_model=None)
async def admin_pm_create_task(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    task_no: str = Form(...),
    description: str = Form(...),
) -> HTMLResponse | RedirectResponse:
    """新增保養大項目(任務範本)。成功 → 直接進該任務編輯頁(接著加細項/串設備)。"""
    try:
        task = await TaskService(session).create_task(
            task_no=task_no, description=description, actor=Actor.human(user.user_id)
        )
    except TaskError as e:
        tasks = await TaskService(session).list_tasks(limit=200)
        return _render(request, "admin_pm.html", user.ui_locale, active="pm",
                       user=user, tasks=tasks, q=None, err=str(e))
    return _pm_task_redirect(task.task_no)


@router.get("/pm/{task_no}", response_class=HTMLResponse, response_model=None)
async def admin_pm_task(
    request: Request,
    task_no: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    err: str | None = None,
) -> HTMLResponse:
    """保養任務編輯頁:描述/啟停 + 細項步驟(增改刪 + 每步用料)+ 排程(串 EID/週期/到期/負責人)。"""
    svc = TaskService(session)
    task = await svc.get_task(task_no)
    if task is None:
        tasks = await svc.list_tasks(limit=200)
        return _render(request, "admin_pm.html", user.ui_locale, active="pm",
                       user=user, tasks=tasks, q=None)
    steps = await svc.get_task_steps(task_no)
    parts_by_step = await svc.get_parts_for_steps([s.id for s in steps])
    schedules = await PmScheduleService(session).list_pm_schedules(task_id=task_no, limit=200)
    return _render(request, "admin_pm_task.html", user.ui_locale, active="pm",
                   user=user, task=task, steps=steps, parts_by_step=parts_by_step,
                   schedules=schedules, err=err)


@router.post("/pm/{task_no}/update", response_model=None)
async def admin_pm_update_task(
    task_no: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    description: str = Form(...),
) -> RedirectResponse:
    try:
        await TaskService(session).update_task_description(
            task_no, description=description, actor=Actor.human(user.user_id)
        )
    except TaskError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/active", response_model=None)
async def admin_pm_task_active(
    task_no: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    active: str = Form(...),
) -> RedirectResponse:
    """啟用/停用任務範本(T3;停用 = 已淘汰,不再新排程)。"""
    try:
        await TaskService(session).set_task_active(
            task_no, active == "1", Actor.human(user.user_id)
        )
    except TaskError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/steps", response_model=None)
async def admin_pm_add_step(
    task_no: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    task_desc: str = Form(...),
) -> RedirectResponse:
    try:
        await TaskService(session).add_task_step(
            task_no, task_desc=task_desc, actor=Actor.human(user.user_id)
        )
    except TaskError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/steps/{step_id}/update", response_model=None)
async def admin_pm_update_step(
    task_no: str,
    step_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    task_desc: str = Form(...),
) -> RedirectResponse:
    try:
        await TaskService(session).update_task_step(
            step_id, task_desc=task_desc, actor=Actor.human(user.user_id), task_no=task_no
        )
    except TaskError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/steps/{step_id}/delete", response_model=None)
async def admin_pm_delete_step(
    task_no: str,
    step_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    try:
        await TaskService(session).delete_task_step(
            step_id, Actor.human(user.user_id), task_no=task_no
        )
    except TaskError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/steps/{step_id}/parts", response_model=None)
async def admin_pm_add_part(
    task_no: str,
    step_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    item_code: str = Form(...),
    replace_qty: str = Form(""),
) -> RedirectResponse:
    try:
        await TaskService(session).add_task_part(
            step_id, item_code=item_code, replace_qty=replace_qty.strip() or None,
            actor=Actor.human(user.user_id), task_no=task_no,
        )
    except (TaskError, ArithmeticError, ValueError) as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/steps/{step_id}/parts/{item_code}/delete", response_model=None)
async def admin_pm_remove_part(
    task_no: str,
    step_id: int,
    item_code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    try:
        await TaskService(session).remove_task_part(
            step_id, item_code, Actor.human(user.user_id), task_no=task_no
        )
    except TaskError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


# ---- PM 排程(任務 × 設備:串 EID / 週期 / 下次到期 / 負責人 / 暫停)----

def _parse_pm_form(
    frequency_interval: str, next_due_date: str
) -> tuple[int, date | None]:
    """表單字串 → (interval, next_due)。非法 → ValueError(呼叫端轉 err banner)。"""
    interval = int(frequency_interval.strip() or "0")
    due = date.fromisoformat(next_due_date.strip()) if next_due_date.strip() else None
    return interval, due


@router.post("/pm/{task_no}/schedules", response_model=None)
async def admin_pm_create_schedule(
    task_no: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    asset_id: str = Form(...),
    frequency_interval: str = Form("0"),
    frequency_unit: str = Form(""),
    next_due_date: str = Form(""),
    assigned_person: str = Form(""),
) -> RedirectResponse:
    """此任務串一台設備(建 pm_schedule):EID + 週期 + 下次到期 + 負責人。"""
    try:
        interval, due = _parse_pm_form(frequency_interval, next_due_date)
        await PmScheduleService(session).create_pm_schedule(
            asset_id=asset_id.strip().upper(),
            task_id=task_no,
            actor=Actor.human(user.user_id),
            frequency_interval=interval,
            frequency_unit=frequency_unit.strip() or None,
            next_due_date=due,
            assigned_person=assigned_person.strip() or None,
        )
    except (PmScheduleError, ValueError) as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/schedules/{pm_id}/update", response_model=None)
async def admin_pm_update_schedule(
    task_no: str,
    pm_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    frequency_interval: str = Form("0"),
    frequency_unit: str = Form(""),
    next_due_date: str = Form(""),
    assigned_person: str = Form(""),
) -> RedirectResponse:
    try:
        interval, due = _parse_pm_form(frequency_interval, next_due_date)
        await PmScheduleService(session).update_pm_schedule(
            pm_id,
            actor=Actor.human(user.user_id),
            frequency_interval=interval,
            frequency_unit=frequency_unit.strip() or None,
            next_due_date=due,
            assigned_person=assigned_person.strip() or None,
            task_id=task_no,
        )
    except (PmScheduleError, ValueError) as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


@router.post("/pm/{task_no}/schedules/{pm_id}/suppress", response_model=None)
async def admin_pm_suppress_schedule(
    task_no: str,
    pm_id: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    suppressed: str = Form(...),
) -> RedirectResponse:
    """暫停/恢復排程(暫停 = 排程器與到期清單不出現;按需生成仍可)。"""
    try:
        await PmScheduleService(session).set_suppressed(
            pm_id, suppressed == "1", Actor.human(user.user_id), task_id=task_no
        )
    except PmScheduleError as e:
        return _pm_task_redirect(task_no, str(e))
    return _pm_task_redirect(task_no)


# ---- 提案審核(ADR-025 Lane 1:engineer/agent 提案 → admin review dry-run diff → confirm)----

@router.get("/proposals", response_class=HTMLResponse, response_model=None)
async def admin_proposals(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    err: str | None = None,
) -> HTMLResponse:
    """待確認提案清單(ADR-016 pending_proposal;admin review + confirm/reject)。

    同頁附「使用者回饋」區(內部規格 說明中心回饋落庫):開放中列表 + 近期已處理 fold。
    """
    svc = WorkOrderService(session)
    # lazy sweep:逾期 PENDING → EXPIRED(review f14cf8d:先前無任何路徑落 EXPIRED)
    await svc.expire_stale_proposals(actor=Actor.human(user.user_id))
    proposals = await svc.list_proposals(status="PENDING")
    fb_svc = FeedbackService(session)
    feedback_open = await fb_svc.list_open()
    feedback_resolved = await fb_svc.list_recent_resolved()
    return _render(request, "admin_proposals.html", user.ui_locale, active="proposals",
                   user=user, proposals=proposals, err=err,
                   feedback_open=feedback_open, feedback_resolved=feedback_resolved)


def _proposals_redirect(err: str | None = None) -> RedirectResponse:
    url = "/admin/proposals" + (f"?err={quote(err)}" if err else "")
    return RedirectResponse(url=url, status_code=303)


@router.post("/proposals/{pending_token}/confirm", response_model=None)
async def admin_confirm_proposal(
    pending_token: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    """確認提案(執行經單一寫入路徑;confirmer = 登入 admin 的 human:<id>,拒匿名,ADR-016)。"""
    try:
        await WorkOrderService(session).confirm(
            pending_token=pending_token, confirmer=Actor.human(user.user_id)
        )
    except (WorkOrderError, AuthorizationError, InventoryError) as e:
        # update_item 提案在 confirm 時走 _update_item_impl,可能拋 InventoryError
        # (負數守門 / 供應商欄限現有值 #7b)→ 回提案頁顯示,不 500
        return _proposals_redirect(str(e))
    return _proposals_redirect()


@router.post("/proposals/{pending_token}/reject", response_model=None)
async def admin_reject_proposal(
    pending_token: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    try:
        await WorkOrderService(session).reject(
            pending_token=pending_token, by=Actor.human(user.user_id)
        )
    except WorkOrderError as e:
        return _proposals_redirect(str(e))
    return _proposals_redirect()


@router.post("/feedback/{feedback_id}/resolve", response_model=None)
async def admin_resolve_feedback(
    feedback_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    """標記說明中心回饋為已處理(admin-only,domain 強制;冪等)→ 回提案頁。"""
    try:
        await FeedbackService(session).mark_resolved(
            feedback_id, Actor.human(user.user_id)
        )
    except (FeedbackError, AuthorizationError) as e:
        return _proposals_redirect(str(e))
    return _proposals_redirect()


@router.get("/accounts", response_class=HTMLResponse, response_model=None)
async def admin_accounts(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    err: str | None = None,
) -> HTMLResponse:
    users = await IdentityService(session).list_users()
    return _render(request, "admin_accounts.html", user.ui_locale, active="accounts",
                   user=user, users=users, err=err)


@router.get("/accounts/new", response_class=HTMLResponse, response_model=None)
async def admin_account_new(
    request: Request, user: UserAccount = Depends(require_admin)
) -> HTMLResponse:
    return _render(request, "admin_account_new.html", user.ui_locale, active="accounts",
                   user=user, error=None)


@router.post("/accounts", response_model=None)
async def admin_account_create(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    username: str = Form(...),
    display_name: str = Form(...),
    org: str = Form(...),
    role: str = Form("engineer"),
    password: str = Form(...),
    emaint_assignee: str = Form(""),
) -> HTMLResponse | RedirectResponse:
    # 防呆:user_id(內部識別碼)不再由 admin 自由填,一律由 username(登入帳號)導出。
    # 兩者永遠相同 → 消除「兩個 ID 欄填錯 / 瀏覽器自動填造成不一致」的坑。username 唯一約束
    # 已保證不碰撞;user_id 仍為 PK/稽核 principal(見 identity/models.py)。
    username = username.strip()
    try:
        await IdentityService(session).create_user(
            user_id=username,
            username=username,
            display_name=display_name.strip(),
            password=password,
            org=org.strip(),
            role=role,
            emaint_assignee=emaint_assignee.strip() or None,
            actor=Actor.human(user.user_id),
        )
    except IdentityError as e:
        return _render(request, "admin_account_new.html", user.ui_locale, active="accounts",
                       user=user, error=str(e))
    return RedirectResponse(url="/admin/accounts", status_code=303)


@router.post("/accounts/{uid}/deactivate", response_model=None)
async def admin_deactivate(
    uid: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    try:
        await IdentityService(session).deactivate_user(uid, actor=Actor.human(user.user_id))
    except IdentityError as e:
        return _accounts_redirect(str(e))
    return _accounts_redirect()


@router.post("/accounts/{uid}/activate", response_model=None)
async def admin_activate(
    uid: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    try:
        await IdentityService(session).reactivate_user(uid, actor=Actor.human(user.user_id))
    except IdentityError as e:
        return _accounts_redirect(str(e))
    return _accounts_redirect()


@router.post("/accounts/{uid}/role", response_model=None)
async def admin_set_role(
    uid: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    role: str = Form(...),
) -> RedirectResponse:
    try:
        await IdentityService(session).set_role(uid, role, actor=Actor.human(user.user_id))
    except IdentityError as e:
        return _accounts_redirect(str(e))
    return _accounts_redirect()


@router.post("/accounts/{uid}/assignee", response_model=None)
async def admin_set_assignee(
    uid: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    assignee: str = Form(""),
) -> RedirectResponse:
    # set_emaint_assignee 不自帶 admin 守門(bootstrap 用),故此 admin 路由先 require_admin。
    try:
        await IdentityService(session).set_emaint_assignee(
            uid, assignee=assignee, actor=Actor.human(user.user_id)
        )
    except IdentityError as e:
        return _accounts_redirect(str(e))
    return _accounts_redirect()


@router.post("/accounts/{uid}/reset-password", response_model=None)
async def admin_reset_password(
    uid: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    new_password: str = Form(...),
) -> RedirectResponse:
    try:
        await IdentityService(session).reset_password(
            uid, new_password, actor=Actor.human(user.user_id)
        )
    except IdentityError as e:
        return _accounts_redirect(str(e))
    return _accounts_redirect()


# ---- 稽核活動 feed(ADR-019;唯讀,無中央 audit_log 表 → 由各 ledger + AuditMixin 欄組裝)----

_AUDIT_SOURCES: tuple[str, ...] = ("status", "note", "stock", "proposal", "deletion")


@router.get("/audit", response_class=HTMLResponse, response_model=None)
async def admin_audit(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    actor: str | None = None,
    source: str = "all",
    limit: int = 50,
) -> HTMLResponse:
    """近期受治理活動的統一 feed(逆時序):狀態轉移 / 日誌更正 / 庫存異動 / 提案裁決 /
    範本細項軟刪。全讀取經 domain service(route 只合併排序,不下 SQL)。

    `actor` = source_actor/deleted_by 子字串過濾;`source` = 單源分頁(all=全部合併);
    `limit` = 每源與總量上限(預設 50,最大 200)。
    """
    lim = max(1, min(limit, 200))
    actor_like = actor.strip() if actor and actor.strip() else None
    src = source if source in _AUDIT_SOURCES else "all"
    wo_svc = WorkOrderService(session)
    events: list[dict] = []
    if src in ("all", "status"):
        for h in await wo_svc.list_recent_status_changes(limit=lim, actor_like=actor_like):
            events.append({"kind": "status", "when": h.changed_at, "who": h.source_actor, "o": h})
    if src in ("all", "note"):
        for n in await wo_svc.list_recent_note_edits(limit=lim, actor_like=actor_like):
            events.append({"kind": "note", "when": n.updated_at, "who": n.updated_by, "o": n})
    if src in ("all", "stock"):
        for txn in await InventoryService(session).list_recent_transactions(
            limit=lim, actor_like=actor_like
        ):
            events.append({"kind": "stock", "when": txn.occurred_at, "who": txn.source_actor,
                           "o": txn})
    if src in ("all", "proposal"):
        for p in await wo_svc.list_resolved_proposals(limit=lim, actor_like=actor_like):
            events.append({"kind": "proposal", "when": p.resolved_at,
                           "who": p.confirmed_by or p.proposed_by, "o": p})
    if src in ("all", "deletion"):
        for d in await TaskService(session).list_recent_deletions(limit=lim, actor_like=actor_like):
            events.append({"kind": "deletion", "when": d.deleted_at, "who": d.deleted_by, "o": d})
    events.sort(key=lambda e: e["when"], reverse=True)
    events = events[:lim]
    return _render(request, "admin_audit.html", user.ui_locale, active="audit",
                   user=user, events=events, actor=actor_like or "", source=src, limit=lim,
                   sources=_AUDIT_SOURCES)


# ---- 附件治理(ADR-019;統計 + 清單 + 軟刪/還原;R2 物件永不硬刪)----

def _attachments_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/attachments", status_code=303)


@router.get("/attachments", response_class=HTMLResponse, response_model=None)
async def admin_attachments(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> HTMLResponse:
    """附件治理:按 owner_type 計數 + 最近上傳 + 已軟刪清單(可還原)。全讀取。"""
    svc = AttachmentService(session)
    counts = await svc.counts_by_owner_type()
    recent = await svc.list_recent_uploads(limit=20)
    deleted = await svc.list_soft_deleted(limit=50)
    return _render(request, "admin_attachments.html", user.ui_locale, active="attachments",
                   user=user, counts=counts, recent=recent, deleted=deleted)


@router.post("/attachments/{attachment_id}/delete", response_model=None)
async def admin_attachment_delete(
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    """軟刪一張附件(治理面;R2 物件保留供稽核,不硬刪、不刪 R2)。actor = 登入 admin。"""
    with suppress(AttachmentError):  # 找不到 → 已無此列,冪等 no-op(回清單)
        await AttachmentService(session).soft_delete_attachment(
            attachment_id, Actor.human(user.user_id)
        )
    return _attachments_redirect()


@router.post("/attachments/{attachment_id}/restore", response_model=None)
async def admin_attachment_restore(
    attachment_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    """還原一張軟刪附件(is_deleted=false + 稽核)。actor = 登入 admin。"""
    with suppress(AttachmentError):
        await AttachmentService(session).restore_attachment(
            attachment_id, Actor.human(user.user_id)
        )
    return _attachments_redirect()


# ---- PAT 憑證總覽(ADR-022 §5;只讀 metadata,密文/明文永不顯示;admin 可撤)----

def _credentials_redirect() -> RedirectResponse:
    return RedirectResponse(url="/admin/credentials", status_code=303)


@router.get("/credentials", response_class=HTMLResponse, response_model=None)
async def admin_credentials(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> HTMLResponse:
    """全體使用者現行外部憑證(Jira PAT)總覽:只顯示 user/system/label/時間,不解密。"""
    creds = await CredentialVault(session).list_all_credentials()
    return _render(request, "admin_credentials.html", user.ui_locale, active="credentials",
                   user=user, creds=creds)


@router.post("/credentials/{credential_id}/revoke", response_model=None)
async def admin_credential_revoke(
    credential_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
) -> RedirectResponse:
    """撤銷任一使用者的現行憑證(即時失效;冪等)。actor = 登入 admin。"""
    await CredentialVault(session).admin_revoke(credential_id, Actor.human(user.user_id))
    return _credentials_redirect()


# ---- 受控詞彙維護(ADR-019;wo_hold_reason 可增改不刪,其餘唯讀顯示)----

def _vocab_redirect(err: str | None = None) -> RedirectResponse:
    url = "/admin/vocab" + (f"?err={quote(err)}" if err else "")
    return RedirectResponse(url=url, status_code=303)


@router.get("/vocab", response_class=HTMLResponse, response_model=None)
async def admin_vocab(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    err: str | None = None,
) -> HTMLResponse:
    """受控詞彙總覽:wo_hold_reason(可增改不刪)+ 五個唯讀 lookup(狀態/日誌型別/
    週期單位/機構類別/庫存異動類別)。全讀取經 domain service(ADR-004)。"""
    wo = WorkOrderService(session)
    hold_reasons = await wo.list_hold_reasons()
    statuses = await wo.list_statuses()
    note_types = await wo.list_note_types()
    freq_units = await PmScheduleService(session).list_freq_units()
    org_types = await ContactsService(session).list_org_types()
    inv = InventoryService(session)
    txn_kinds = await inv.list_stock_txn_kinds()
    storage_bins = await inv.list_storage_bins(include_inactive=True)
    fv = FailureVocabService(session)
    mes_failmodes = await fv.list_mes_failmodes()
    efc_codes = await fv.list_equipment_failure_codes()
    return _render(request, "admin_vocab.html", user.ui_locale, active="vocab",
                   user=user, hold_reasons=hold_reasons, statuses=statuses,
                   note_types=note_types, freq_units=freq_units, org_types=org_types,
                   txn_kinds=txn_kinds, storage_bins=storage_bins,
                   mes_failmodes=mes_failmodes, efc_codes=efc_codes, err=err)


@router.post("/vocab/hold-reasons", response_model=None)
async def admin_vocab_add_hold_reason(
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    code: str = Form(...),
    label: str = Form(...),
    is_downtime: str = Form(""),
) -> RedirectResponse:
    """新增一個等待原因(code 大寫蛇形 + label + is_downtime 旗標)。governed。"""
    try:
        await WorkOrderService(session).add_hold_reason(
            code, label, is_downtime=is_downtime == "1", actor=Actor.human(user.user_id)
        )
    except (WorkOrderError, AuthorizationError) as e:
        return _vocab_redirect(str(e))
    return _vocab_redirect()


@router.post("/vocab/hold-reasons/{code}/update", response_model=None)
async def admin_vocab_update_hold_reason(
    code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    label: str = Form(...),
    is_downtime: str = Form(""),
) -> RedirectResponse:
    """更新既有等待原因的 label / is_downtime(不刪除;is_downtime 改變不回溯已結案)。"""
    try:
        await WorkOrderService(session).update_hold_reason(
            code, label=label, is_downtime=is_downtime == "1", actor=Actor.human(user.user_id)
        )
    except (WorkOrderError, AuthorizationError) as e:
        return _vocab_redirect(str(e))
    return _vocab_redirect()


# ---- storage_bin 儲位受控詞彙(admin;增 + 啟停,不刪)----


@router.post("/vocab/storage-bins", response_model=None)
async def admin_vocab_add_bin(
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    code: str = Form(...),
) -> RedirectResponse:
    """新增一個儲位代號(格式驗證 + 大小寫不敏感查重)。governed。"""
    try:
        await InventoryService(session).add_storage_bin(code, actor=Actor.human(user.user_id))
    except (InventoryError, AuthorizationError) as e:
        return _vocab_redirect(str(e))
    return _vocab_redirect()


@router.post("/vocab/storage-bins/{code}/toggle", response_model=None)
async def admin_vocab_toggle_bin(
    code: str,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    is_active: str = Form(""),
) -> RedirectResponse:
    """啟用 / 停用一個儲位(is_active='1' → 啟用,否則停用)。停用 = 不再供選,既有品項不受影響。"""
    try:
        await InventoryService(session).set_storage_bin_active(
            code, is_active=is_active == "1", actor=Actor.human(user.user_id)
        )
    except (InventoryError, AuthorizationError) as e:
        return _vocab_redirect(str(e))
    return _vocab_redirect()


# ---- 資產關係維護(ADR-018;既有 composition service ops,route 只接線)----

def _relationships_redirect(eid: str | None = None, err: str | None = None) -> RedirectResponse:
    """回關係頁;帶回查詢 EID(續看同一台)與可選錯誤 banner。"""
    params = []
    if eid:
        params.append(f"eid={quote(eid)}")
    if err:
        params.append(f"err={quote(err)}")
    url = "/admin/relationships" + ("?" + "&".join(params) if params else "")
    return RedirectResponse(url=url, status_code=303)


@router.get("/relationships", response_class=HTMLResponse, response_model=None)
async def admin_relationships(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    eid: str | None = None,
    err: str | None = None,
) -> HTMLResponse:
    """資產組成圖維護(ADR-018)。載入即統計現行邊總數;查一台 EID → 顯示其容器/子模組/
    共用相依/單親。全讀取經既有 AssetService(get_contained_descendants / list_relationships)。"""
    svc = AssetService(session)
    all_edges = await svc.list_relationships_all()
    contains_total = sum(1 for e in all_edges if e.relationship_type == "contains_module")
    shared_total = sum(1 for e in all_edges if e.relationship_type == "shared_dependency")
    focus = (eid or "").strip().upper()
    asset = children = shared = parents = None
    if focus:
        asset = await svc.get_asset(focus)
        # 容器方向邊(from=focus):contains_module 子模組 + shared_dependency 服務對象
        outgoing = await svc.list_relationships(focus, direction="from")
        children = [e for e in outgoing if e.relationship_type == "contains_module"]
        shared = [e for e in outgoing if e.relationship_type == "shared_dependency"]
        # 反向 contains_module(to=focus):此 EID 的單親容器
        incoming = await svc.list_relationships(focus, direction="to")
        parents = [e for e in incoming if e.relationship_type == "contains_module"]
    return _render(request, "admin_relationships.html", user.ui_locale, active="relationships",
                   user=user, eid=focus, asset=asset, children=children, shared=shared,
                   parents=parents, contains_total=contains_total, shared_total=shared_total,
                   err=err)


@router.post("/relationships/contain", response_model=None)
async def admin_relationships_contain(
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    parent_eid: str = Form(...),
    child_eid: str = Form(...),
) -> RedirectResponse:
    """建 contains_module 邊(機台⊃模組)。驗證(成環/單親)由既有 service 守門。"""
    parent = parent_eid.strip().upper()
    child = child_eid.strip().upper()
    svc = AssetService(session)
    try:
        async with svc.write(Actor.human(user.user_id)):
            await svc.link_containment(parent, child, Actor.human(user.user_id))
    except AssetError as e:
        return _relationships_redirect(parent, str(e))
    return _relationships_redirect(parent)


@router.post("/relationships/shared", response_model=None)
async def admin_relationships_shared(
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    resource_eid: str = Form(...),
    machine_eid: str = Form(...),
) -> RedirectResponse:
    """建 shared_dependency 邊(共用資源→被服務機台,N:M)。"""
    resource = resource_eid.strip().upper()
    machine = machine_eid.strip().upper()
    svc = AssetService(session)
    try:
        async with svc.write(Actor.human(user.user_id)):
            await svc.link_shared_dependency(resource, machine, Actor.human(user.user_id))
    except AssetError as e:
        return _relationships_redirect(resource, str(e))
    return _relationships_redirect(resource)


@router.post("/relationships/{rel_id}/unlink", response_model=None)
async def admin_relationships_unlink(
    rel_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    eid: str = Form(""),
) -> RedirectResponse:
    """軟解一條關係邊(設 valid_to,保留歷史;冪等)。回原查詢 EID。"""
    svc = AssetService(session)
    try:
        async with svc.write(Actor.human(user.user_id)):
            await svc.unlink_relationship(rel_id, Actor.human(user.user_id))
    except AssetError as e:  # 未知邊 id 等 → banner,不 500
        return _relationships_redirect(eid.strip().upper() or None, str(e))
    return _relationships_redirect(eid.strip().upper() or None)


# ---- 批次指定負責人(asset_owner,0031;admin 一次選多台 + 整組替換負責人清單)----

_OWNER_CAP = 300  # 清單上限(超過提示 truncated;此頁聚焦補 owner,無需分頁)


def _owners_redirect(
    q: str, missing: str, *, ok: int | None = None, err: str | None = None
) -> RedirectResponse:
    """回批次指定頁,保留同一過濾檢視(q / missing);帶 ok=<變更數> 或 err。"""
    params = [f"q={quote(q)}", f"missing={quote(missing)}"]
    if ok is not None:
        params.append(f"ok={ok}")
    if err:
        params.append(f"err={quote(err)}")
    return RedirectResponse(url="/admin/owners?" + "&".join(params), status_code=303)


@router.get("/owners", response_class=HTMLResponse, response_model=None)
async def admin_owners(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    q: str | None = None,
    missing: str = "1",
    ok: int | None = None,
    err: str | None = None,
) -> HTMLResponse:
    """批次指定設備負責人(多負責人=工單 assignee 事實來源,0031)。

    `q` = EID / 描述 ilike;`missing`(預設 "1")= 僅列 owner IS NULL(聚焦待補);"0" = 全部
    在冊資產。上限 _OWNER_CAP,超過顯示 truncated 提示。全讀取經 AssetService(ADR-004)。
    """
    only_missing = missing != "0"
    svc = AssetService(session)
    rows = await svc.list_for_owner_admin(
        search=q, only_missing=only_missing, limit=_OWNER_CAP + 1
    )
    truncated = len(rows) > _OWNER_CAP
    assets = rows[:_OWNER_CAP]
    pm_counts = await svc.pm_counts([a.asset_id for a in assets])
    return _render(request, "admin_owners.html", user.ui_locale, active="owners",
                   user=user, assets=assets, pm_counts=pm_counts, q=q or "",
                   missing="1" if only_missing else "0", truncated=truncated, ok=ok, err=err)


@router.post("/owners", response_model=None)
async def admin_owners_apply(
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    asset_ids: list[str] = Form([]),
    owners: list[str] = Form([]),
    q: str = Form(""),
    missing: str = Form("1"),
) -> RedirectResponse:
    """把選定資產的負責人清單整組替換(0031 多負責人)。空清單 = 清除;無勾選 → err banner。"""
    if not [i for i in asset_ids if i.strip()]:
        return _owners_redirect(q, missing, err="admin.owners.err.noselect")
    try:
        n = await AssetService(session).set_owner_bulk(
            asset_ids=asset_ids, owners=owners, actor=Actor.human(user.user_id)
        )
    except (AssetError, AuthorizationError) as e:
        return _owners_redirect(q, missing, err=str(e))
    return _owners_redirect(q, missing, ok=n)


# ---- 通知收件人維護(Slice B;email + Telegram;收件人不綁 user_account,管理者可收)----

def _notify_redirect(err: str | None = None) -> RedirectResponse:
    url = "/admin/notify" + (f"?err={quote(err)}" if err else "")
    return RedirectResponse(url=url, status_code=303)


def _form_bool(value: str) -> bool:
    return value == "1"


@router.get("/notify", response_class=HTMLResponse, response_model=None)
async def admin_notify(
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    err: str | None = None,
) -> HTMLResponse:
    """通知收件人維護 + 通道配置狀態 + 近期 outbox(唯讀)。寫入經 NotificationService。"""
    from cmms.email import smtp_configured
    from cmms.telegram import telegram_configured

    svc = NotificationService(session)
    recipients = await svc.list_recipients()
    recent = await svc.list_recent_outbox(limit=20)
    failed_count = await svc.count_failed()
    recipient_names = {r.id: r.name for r in recipients}  # outbox 列顯示姓名而非 id
    return _render(request, "admin_notify.html", user.ui_locale, active="notify",
                   user=user, recipients=recipients, recent=recent,
                   recipient_names=recipient_names,
                   failed_count=failed_count, smtp_ok=smtp_configured(),
                   telegram_ok=telegram_configured(), err=err)


@router.post("/notify", response_model=None)
async def admin_notify_create(
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    name: str = Form(...),
    email: str = Form(""),
    telegram_chat_id: str = Form(""),
    assignee_name: str = Form(""),
    notify_on_open: str = Form(""),
    notify_on_close: str = Form(""),
    watch_assignees: list[str] = Form([]),
) -> RedirectResponse:
    """新增通知收件人(name 必填 + 至少一通道;email/telegram 各觸發該通道)。governed。"""
    try:
        await NotificationService(session).create_recipient(
            name=name, email=email or None, telegram_chat_id=telegram_chat_id or None,
            assignee_name=assignee_name or None,
            notify_on_open=_form_bool(notify_on_open),
            notify_on_close=_form_bool(notify_on_close),
            watch_assignees=watch_assignees,
            actor=Actor.human(user.user_id),
        )
    except (NotifyError, AuthorizationError) as e:
        return _notify_redirect(str(e))
    return _notify_redirect()


@router.post("/notify/{recipient_id}/update", response_model=None)
async def admin_notify_update(
    recipient_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    name: str = Form(...),
    email: str = Form(""),
    telegram_chat_id: str = Form(""),
    assignee_name: str = Form(""),
    notify_on_open: str = Form(""),
    notify_on_close: str = Form(""),
    watch_assignees: list[str] = Form([]),
) -> RedirectResponse:
    try:
        await NotificationService(session).update_recipient(
            recipient_id, name=name, email=email or None,
            telegram_chat_id=telegram_chat_id or None, assignee_name=assignee_name or None,
            notify_on_open=_form_bool(notify_on_open),
            notify_on_close=_form_bool(notify_on_close),
            watch_assignees=watch_assignees,
            actor=Actor.human(user.user_id),
        )
    except (NotifyError, AuthorizationError) as e:
        return _notify_redirect(str(e))
    return _notify_redirect()


@router.post("/notify/{recipient_id}/toggle", response_model=None)
async def admin_notify_toggle(
    recipient_id: int,
    session: AsyncSession = Depends(get_session),
    user: UserAccount = Depends(require_admin),
    is_active: str = Form(""),
) -> RedirectResponse:
    """啟用 / 停用一個收件人(停用 = 不再排入 outbox;既有列不受影響)。"""
    try:
        await NotificationService(session).set_recipient_active(
            recipient_id, is_active == "1", actor=Actor.human(user.user_id)
        )
    except (NotifyError, AuthorizationError) as e:
        return _notify_redirect(str(e))
    return _notify_redirect()
