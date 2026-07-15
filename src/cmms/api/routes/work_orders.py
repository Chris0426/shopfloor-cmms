"""WorkOrder 讀取 API(thin client,只呼叫 WorkOrderService)。寫入留待 #4b 切片。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.config import get_settings
from cmms.domain.work_order.downtime import segment_is_downtime
from cmms.domain.work_order.onbox import (
    KeyResolver,
    OnboxVerificationError,
    make_jwks_resolver,
    make_static_jwks_resolver,
)
from cmms.domain.work_order.schemas import (
    WorkOrderActiveInResponse,
    WorkOrderActiveWindowRead,
    WorkOrderDetail,
    WorkOrderNoteRead,
    WorkOrderPartRead,
    WorkOrderRead,
    WorkOrderStatusHistoryRead,
)
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService
from cmms.domain.work_order.transform import TAIPEI

router = APIRouter(tags=["work-orders"])

# A3 視窗查詢 v1 固定上限(候選集截斷點;回應以 truncated=true 誠實標示)
_ACTIVE_IN_CAP = 2000
# 工單詳情 notes v1 固定上限:超過保留**最新** N 筆(升冪 tail),notes_truncated=true
_DETAIL_NOTES_CAP = 200
# 廠區台北時間格式(naive `YYYY-MM-DD HH:MM:SS`;與 DB timestamptz 同座標經 TAIPEI 定位)
_ACTIVE_IN_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _history_reads(
    work_type: str,
    history: Sequence[object],
    status_is_downtime: Mapping[str, bool],
    hold_is_downtime: Mapping[str, bool],
) -> list[WorkOrderStatusHistoryRead]:
    """把 status_history ORM 列轉 DTO,並依 內部規格 work_type-aware 純函式帶入計算欄 is_downtime。

    ORM 列無 is_downtime 屬性 → **顯式建構**(不能走 model_validate,否則缺欄)。lookup maps
    由呼叫端每請求載一次後傳入(detail/active-in 共用本 helper,不複製兩份判定邏輯)。
    """
    return [
        WorkOrderStatusHistoryRead(
            from_status=h.from_status,
            to_status=h.to_status,
            hold_reason=h.hold_reason,
            changed_at=h.changed_at,
            source_actor=h.source_actor,
            is_downtime=segment_is_downtime(
                work_type,
                h.to_status,
                h.hold_reason,
                status_is_downtime=status_is_downtime,
                hold_is_downtime=hold_is_downtime,
            ),
        )
        for h in history
    ]


class OnboxSubmission(BaseModel):
    """Analytics on-box relay 送來的簽章 payload(ADR-017)。"""

    jws_token: str


def _onbox_key_resolver() -> KeyResolver:
    """建 on-box JWS 的 key resolver。優先序:靜態 JWKS JSON(值交付,分析平台裁決)> JWKS URL >
    未配置 → 503(待 Analytics 部署 / 交付 JWKS,下游交付)。壞靜態 JWKS → fail-fast(非 503)。"""
    settings = get_settings()
    if settings.onbox_jwks_json:
        return make_static_jwks_resolver(settings.onbox_jwks_json)  # 值交付,優先於 URL
    if settings.onbox_jwks_url:
        return make_jwks_resolver(settings.onbox_jwks_url)
    raise HTTPException(
        status_code=503,
        detail=(
            "on-box JWKS not configured (CMMS_ONBOX_JWKS_JSON / CMMS_ONBOX_JWKS_URL); "
            "pending Analytics delivery"
        ),
    )


@router.get("/work-orders", response_model=list[WorkOrderRead])
async def list_work_orders(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    asset_id: str | None = None,
    work_type: str | None = None,
    status: str | None = None,
    assigned_vendor: str | None = None,
    opened_on_or_after: date | None = None,
    opened_on_or_before: date | None = None,
) -> list[WorkOrderRead]:
    work_orders = await WorkOrderService(session).list_work_orders(
        limit=limit,
        offset=offset,
        asset_id=asset_id,
        work_type=work_type,
        status=status,
        assigned_vendor=assigned_vendor,
        opened_on_or_after=opened_on_or_after,
        opened_on_or_before=opened_on_or_before,
    )
    return [WorkOrderRead.model_validate(w) for w in work_orders]


@router.get("/work-orders/active-in", response_model=WorkOrderActiveInResponse)
async def list_active_in_window(
    start: str = Query(..., description="窗起 naive 'YYYY-MM-DD HH:MM:SS'(廠區台北時間)"),
    end: str = Query(..., description="窗迄 naive 'YYYY-MM-DD HH:MM:SS'(廠區台北時間)"),
    asset_id: str | None = Query(None, description="選填:限單一設備 EID"),
    session: AsyncSession = Depends(get_session),
) -> WorkOrderActiveInResponse:
    """A3(Analytics 消費端需求):列「**活躍於 [start, end] 窗內**」的工單(非 opened 於窗內)。

    窗語意 = 工單活躍窗(opened_at → 首個 COMPLETED/終態;遷移單 fallback opened_at→closed_at;
    仍 open → 至今)與 [start, end] 相交。回列表 + `truncated`(v1 上限 2000 截斷旗標)。
    受既有 static bearer 保護(非豁免路徑);`start`/`end` 壞格式 → 422。
    """
    try:
        start_dt = datetime.strptime(start, _ACTIVE_IN_TS_FMT).replace(tzinfo=TAIPEI)
        end_dt = datetime.strptime(end, _ACTIVE_IN_TS_FMT).replace(tzinfo=TAIPEI)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"start/end must be naive 'YYYY-MM-DD HH:MM:SS': {e}",
        ) from e
    service = WorkOrderService(session)
    rows, truncated = await service.list_active_in_window(
        start=start_dt, end=end_dt, asset_id=asset_id, cap=_ACTIVE_IN_CAP
    )
    # 內部規格:lookup maps 每請求載一次(可回 2000 單,不得每單/每段重查);is_downtime 依各單 work_type
    status_is_downtime, hold_is_downtime = await service.downtime_lookup_maps()
    items = [
        WorkOrderActiveWindowRead(
            work_order_no=wo.work_order_no,
            asset_id=wo.asset_id,
            work_type=wo.work_type,
            opened_at=wo.opened_at,
            closed_at=wo.closed_at,
            status=wo.status,
            hold_reason=wo.hold_reason,
            status_history=_history_reads(
                wo.work_type, history, status_is_downtime, hold_is_downtime
            ),
        )
        for wo, history in rows
    ]
    return WorkOrderActiveInResponse(items=items, truncated=truncated)


@router.get("/work-orders/{work_order_no}", response_model=WorkOrderRead)
async def get_work_order(
    work_order_no: int, session: AsyncSession = Depends(get_session)
) -> WorkOrderRead:
    work_order = await WorkOrderService(session).get_work_order(work_order_no)
    if work_order is None:
        raise HTTPException(status_code=404, detail=f"work_order {work_order_no} not found")
    return WorkOrderRead.model_validate(work_order)


@router.get("/work-orders/{work_order_no}/detail", response_model=WorkOrderDetail)
async def get_work_order_detail(
    work_order_no: int, session: AsyncSession = Depends(get_session)
) -> WorkOrderDetail:
    """工單 + 狀態歷程 + 領料明細。"""
    service = WorkOrderService(session)
    work_order = await service.get_work_order(work_order_no)
    if work_order is None:
        raise HTTPException(status_code=404, detail=f"work_order {work_order_no} not found")
    history = await service.get_status_history(work_order_no)
    parts = await service.get_parts(work_order_no)
    # notes 已由 list_notes 排除軟刪、依 occurred_at,id 升冪。超過上限保留**最新** N 筆
    # (升冪 list 的 tail),仍升冪呈現、notes_truncated=true。
    notes = await service.list_notes(work_order_no)
    notes_truncated = len(notes) > _DETAIL_NOTES_CAP
    if notes_truncated:
        notes = notes[-_DETAIL_NOTES_CAP:]
    assignees = await service.get_assignees(work_order_no)  # 0031 additive(依 position)
    # 內部規格:lookup maps 每請求載一次;is_downtime 依本單 work_type 計算(PM OPEN 段不計)
    status_is_downtime, hold_is_downtime = await service.downtime_lookup_maps()
    return WorkOrderDetail(
        **WorkOrderRead.model_validate(work_order).model_dump(),
        status_history=_history_reads(
            work_order.work_type, history, status_is_downtime, hold_is_downtime
        ),
        parts=[WorkOrderPartRead.model_validate(p) for p in parts],
        notes=[WorkOrderNoteRead.model_validate(n) for n in notes],
        notes_truncated=notes_truncated,
        assignees=assignees,
    )


@router.post("/work-orders/on-box/reactive", response_model=WorkOrderRead, status_code=201)
async def onbox_open_reactive(
    body: OnboxSubmission, session: AsyncSession = Depends(get_session)
) -> WorkOrderRead:
    """設備端一鍵報修(ADR-017 Profile B):Analytics relay POST 簽章 JWS → 開 REACTIVE WO。"""
    resolver = _onbox_key_resolver()
    try:
        wo = await WorkOrderService(session).open_reactive_work_order_onbox(
            jws_token=body.jws_token, key_resolver=resolver
        )
    except OnboxVerificationError as e:
        raise HTTPException(status_code=403, detail=f"on-box signature rejected: {e}") from e
    except WorkOrderError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return WorkOrderRead.model_validate(wo)


@router.post("/work-orders/on-box/cancel", response_model=WorkOrderRead)
async def onbox_cancel(
    body: OnboxSubmission, session: AsyncSession = Depends(get_session)
) -> WorkOrderRead:
    """設備端取消報修(ADR-017 soft-cancel;限該 on-box 單仍 OPEN)。"""
    resolver = _onbox_key_resolver()
    try:
        wo = await WorkOrderService(session).cancel_reactive_report_onbox(
            jws_token=body.jws_token, key_resolver=resolver
        )
    except OnboxVerificationError as e:
        raise HTTPException(status_code=403, detail=f"on-box signature rejected: {e}") from e
    except WorkOrderError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return WorkOrderRead.model_validate(wo)
