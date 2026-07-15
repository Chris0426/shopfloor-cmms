"""MCP server 骨架。

各切片在此註冊領域操作工具。範例先放一個唯讀的 `ping`;真正的工具(`get_asset`
等)隨切片進來,內部一律走 domain service。

啟動(對外):Streamable HTTP transport,掛在 FastAPI `/mcp`(api/app.py;stateless,
per-user scoped token 閘門在 api/auth.py)。本地測試可走 stdio(`main()`)。
"""

from __future__ import annotations

import secrets
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from cmms import __version__
from cmms.db import get_sessionmaker
from cmms.domain.asset.schemas import (
    AssetIdentityRead,
    AssetRead,
    AssetRelationshipRead,
    AssetTreeRead,
    ExternalIdRead,
)
from cmms.domain.asset.service import AssetService
from cmms.domain.attachment.schemas import AttachmentRead
from cmms.domain.attachment.service import AttachmentService
from cmms.domain.contacts.schemas import OrganizationRead, PersonRead, PersonSummary
from cmms.domain.contacts.service import ContactsService
from cmms.domain.inventory.schemas import InventoryItemRead
from cmms.domain.inventory.service import InventoryService
from cmms.domain.pm_schedule.schemas import PmScheduleRead
from cmms.domain.pm_schedule.service import PmScheduleService
from cmms.domain.procurement.service import ProcurementService
from cmms.domain.task.schemas import TaskRead
from cmms.domain.task.service import TaskService
from cmms.domain.work_order.schemas import WorkOrderRead
from cmms.domain.work_order.service import WorkOrderService

mcp = FastMCP(
    "cmms",
    # stateless:每個 HTTP 請求獨立(不留 server-side MCP session)—— 雲端 + 多 client 友善,
    # 也讓 /mcp 的 per-user token 閘門逐請求驗證(無舊 session 可劫持)。
    stateless_http=True,
    # SDK 的 DNS-rebinding Host 檢查是「本機無驗證 server」的防線;cmms /mcp 走公網 HTTPS
    # + per-user scoped token(api/auth.py),Host allowlist 反而會擋 example.com / 自訂網域。
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
def ping() -> dict[str, str]:
    """連線健檢(唯讀)。回 server 版本。"""
    return {"status": "ok", "version": __version__}


# ---- Asset 切片:讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
async def get_asset(asset_id: str) -> dict[str, Any] | None:
    """查單一設備主檔。傳入 EID(如 EID-70002)。"""
    async with get_sessionmaker()() as session:
        asset = await AssetService(session).get_asset(asset_id)
        return AssetRead.model_validate(asset).model_dump() if asset else None


@mcp.tool()
async def list_assets(
    department: str | None = None,
    asset_type: str | None = None,
    line: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列設備主檔,可依部門 / 類型 / 產線過濾。"""
    async with get_sessionmaker()() as session:
        assets = await AssetService(session).list_assets(
            department=department, asset_type=asset_type, line=line, limit=limit
        )
        return [AssetRead.model_validate(a).model_dump() for a in assets]


@mcp.tool()
async def resolve_asset_identity(namespace: str, external_id: str) -> dict[str, Any] | None:
    """用外部系統 id(namespace+external_id)反查同一台機器(ADR-015 身分服務)。"""
    async with get_sessionmaker()() as session:
        asset = await AssetService(session).resolve_by_external_id(namespace, external_id)
        return AssetRead.model_validate(asset).model_dump() if asset else None


@mcp.tool()
async def get_asset_identity(asset_id: str) -> dict[str, Any] | None:
    """回設備的 canonical 身分:asset_id + 所有外部系統 id(ADR-015)。"""
    async with get_sessionmaker()() as session:
        service = AssetService(session)
        if await service.get_asset(asset_id) is None:
            return None
        ext = await service.list_external_ids(asset_id)
        return AssetIdentityRead(
            asset_id=asset_id,
            external_ids=[ExternalIdRead.model_validate(e) for e in ext],
        ).model_dump()


# ---- 資產組成圖(ADR-018):讀取工具(ADR-004 讀取開放)----


@mcp.tool()
async def get_asset_tree(asset_id: str) -> dict[str, Any] | None:
    """回機台組成子樹(ADR-018):contains_module 後代模組 + 本機台現行關係邊。傳入機台 EID。"""
    async with get_sessionmaker()() as session:
        service = AssetService(session)
        if await service.get_asset(asset_id) is None:
            return None
        descendants = await service.get_contained_descendants(asset_id)
        rels = await service.list_relationships(asset_id, direction="both", active_only=True)
        return AssetTreeRead(
            asset_id=asset_id,
            descendant_asset_ids=descendants,
            relationships=[AssetRelationshipRead.model_validate(r) for r in rels],
        ).model_dump(mode="json")


@mcp.tool()
async def rollup_asset_work_orders(asset_id: str, limit: int = 50) -> list[dict[str, Any]] | None:
    """機台維護 rollup(ADR-018):自身 + contains_module 後代的工單(新到舊)。未知 EID 回 None。"""
    async with get_sessionmaker()() as session:
        service = AssetService(session)
        # 與 get_asset_tree / API 一致:未知 EID 回 None,而非靜默 []
        if await service.get_asset(asset_id) is None:
            return None
        wos = await service.rollup_work_orders(asset_id, limit=limit)
        return [WorkOrderRead.model_validate(w).model_dump(mode="json") for w in wos]


# ---- Task 切片:讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
async def get_task(task_no: str) -> dict[str, Any] | None:
    """查單一保養任務範本。傳入 task_no(如 CAL1DM)。"""
    async with get_sessionmaker()() as session:
        task = await TaskService(session).get_task(task_no)
        return TaskRead.model_validate(task).model_dump() if task else None


@mcp.tool()
async def list_tasks(
    search: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列保養任務範本,可依描述關鍵字(search)/ 啟用狀態(is_active)過濾。"""
    async with get_sessionmaker()() as session:
        tasks = await TaskService(session).list_tasks(
            search=search, is_active=is_active, limit=limit
        )
        return [TaskRead.model_validate(t).model_dump() for t in tasks]


@mcp.tool()
async def get_task_steps(task_no: str) -> list[dict[str, Any]] | None:
    """查保養任務的細項步驟(含每步用料;migration 0016)。傳入 task_no(如 TSK0007)。

    回步驟清單(依 proc_seq 排序,`position` 為 1..N 顯示序號);每步 `parts` 為 0..N 個
    零件(`item_code` + `replace_qty`,qty 可為 null=造冊未清點)。未知 task 回 None。
    """
    async with get_sessionmaker()() as session:
        svc = TaskService(session)
        if await svc.get_task(task_no) is None:
            return None
        steps = await svc.get_task_steps(task_no)
        parts_by_step = await svc.get_parts_for_steps([s.id for s in steps])
        return [
            {
                "position": i,  # 顯示序號(1..N 枚舉;非 eMaint 原始 10/20/30)
                "proc_seq": s.proc_seq,  # eMaint 原始序號(溯源)
                "instruction": s.task_desc,
                "parts": [
                    {
                        "item_code": p.item_code,
                        "replace_qty": float(p.replace_qty) if p.replace_qty is not None else None,
                    }
                    for p in parts_by_step.get(s.id, [])
                ],
            }
            for i, s in enumerate(steps, 1)
        ]


# ---- ScheduledActivity 切片:讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
async def get_pm_schedule(pm_id: str) -> dict[str, Any] | None:
    """查單一 PM 排程(預防保養定義)。傳入 pm_id(如 _7ZX412Q88)。"""
    async with get_sessionmaker()() as session:
        s = await PmScheduleService(session).get_pm_schedule(pm_id)
        return PmScheduleRead.model_validate(s).model_dump(mode="json") if s else None


@mcp.tool()
async def list_pm_schedules(
    asset_id: str | None = None,
    task_id: str | None = None,
    assigned_vendor: str | None = None,
    is_suppressed: bool | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列 PM 排程,可依設備 / 任務 / 承包商 / 停用狀態過濾。"""
    async with get_sessionmaker()() as session:
        schedules = await PmScheduleService(session).list_pm_schedules(
            asset_id=asset_id,
            task_id=task_id,
            assigned_vendor=assigned_vendor,
            is_suppressed=is_suppressed,
            limit=limit,
        )
        return [PmScheduleRead.model_validate(s).model_dump(mode="json") for s in schedules]


@mcp.tool()
async def list_due_pm_schedules(due_on_or_before: str, limit: int = 50) -> list[dict[str, Any]]:
    """到期清單(讀取,ADR-021):列 next_due_date <= 指定日且未停用(is_suppressed=false)的 PM。

    `due_on_or_before` 為 ISO 日期字串(YYYY-MM-DD)。供工程師挑選到期 PM 後按需生成工單。
    """
    async with get_sessionmaker()() as session:
        schedules = await PmScheduleService(session).list_pm_schedules(
            due_on_or_before=date.fromisoformat(due_on_or_before),
            is_suppressed=False,
            limit=limit,
        )
        return [PmScheduleRead.model_validate(s).model_dump(mode="json") for s in schedules]


@mcp.tool()
async def generate_pm_work_order(pm_id: str, actor_id: str = "") -> dict[str, Any]:
    """按需生成 PM 工單(ADR-021 governed write):工程師執行某到期 PM → 開一張 PM 工單。

    走單一寫入路徑 + 全稽核 + 冪等(本週期已有未結案 PM 工單 → 回該單,不重複生成);**非**
    gated-write(PM 生成是 schedule 決定論、非裁量,不需 propose/confirm)。

    歸屬:**/mcp transport 閘門驗過的身分優先**(防在稽核軌冒名);無 transport 身分(非 HTTP
    直呼)時才退回自報 `actor_id`。記 source_actor=human:<id>。回新建或既有工單摘要。
    """
    from cmms.audit import Actor
    from cmms.domain.identity.service import mcp_transport_identity
    from cmms.domain.work_order.service import WorkOrderError

    transport = mcp_transport_identity.get()
    if transport is not None:
        actor = Actor.human(transport[0])  # 驗過的身分勝過自報 actor_id
    elif actor_id:
        actor = Actor.human(actor_id)
    else:
        raise WorkOrderError(
            "generate_pm_work_order requires actor_id or an authenticated /mcp transport identity"
        )
    async with get_sessionmaker()() as session:
        wo = await WorkOrderService(session).generate_pm_work_order(pm_id=pm_id, actor=actor)
        return WorkOrderRead.model_validate(wo).model_dump(mode="json")


# ---- WorkOrder 切片:讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
async def get_work_order(work_order_no: int) -> dict[str, Any] | None:
    """查單一工單。傳入工單號(如 24172)。"""
    async with get_sessionmaker()() as session:
        wo = await WorkOrderService(session).get_work_order(work_order_no)
        return WorkOrderRead.model_validate(wo).model_dump(mode="json") if wo else None


@mcp.tool()
async def list_work_orders(
    asset_id: str | None = None,
    work_type: str | None = None,
    status: str | None = None,
    assigned_vendor: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列工單(新到舊),可依設備 / 類型 / 狀態 / 承包商過濾。"""
    async with get_sessionmaker()() as session:
        wos = await WorkOrderService(session).list_work_orders(
            asset_id=asset_id,
            work_type=work_type,
            status=status,
            assigned_vendor=assigned_vendor,
            limit=limit,
        )
        return [WorkOrderRead.model_validate(w).model_dump(mode="json") for w in wos]


# ---- Inventory 切片:讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
async def get_inventory_item(item_code: str) -> dict[str, Any] | None:
    """查單一庫存品項(零件/耗材)。傳入 item code(如 ES000701)。"""
    async with get_sessionmaker()() as session:
        item = await InventoryService(session).get_item(item_code)
        return InventoryItemRead.model_validate(item).model_dump(mode="json") if item else None


@mcp.tool()
async def list_inventory_items(
    item_category: str | None = None,
    supplier: str | None = None,
    asset_subtype: str | None = None,
    below_reorder: bool = False,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列庫存品項,可依分類 / 供應商 / 適用設備子類型 / 是否低於再訂購點過濾。"""
    async with get_sessionmaker()() as session:
        items = await InventoryService(session).list_items(
            item_category=item_category,
            supplier=supplier,
            asset_subtype=asset_subtype,
            below_reorder=below_reorder,
            limit=limit,
        )
        return [InventoryItemRead.model_validate(i).model_dump(mode="json") for i in items]


@mcp.tool()
async def draft_below_safety_stock_rfqs(limit: int = 50) -> list[dict[str, Any]]:
    """預覽低於安全庫存的 RFQ 候選(ADR-026;**dry-run / 唯讀,不發信**)。依 supplier org 分組。

    agent 面預設 draft-only(決策 a);真正發送為 human-initiated(web 一鍵 / admin confirm)。
    只納入已連 supplier_org_id 的品項;數量 = reorder_quantity 或 reorder_point−on_hand。
    """
    async with get_sessionmaker()() as session:
        drafts = await ProcurementService(session).draft_below_safety_stock(limit=limit)
        return [
            {
                "supplier_org_id": d.supplier_org_id,
                "supplier_name": d.supplier_name,
                "recipient_email": d.recipient_email,
                "lines": [{"item_code": c, "quantity": float(q)} for c, q in d.lines],
            }
            for d in drafts
        ]


# ---- WorkOrder gated write(#4b-2,ADR-016 兩階段):agent 提案、人類確認 ----
# propose 不立即執行;confirm 必須帶已驗證的 human:<id>(拒匿名)。高風險操作不在此暴露。


@mcp.tool()
async def propose_open_work_order(
    asset_id: str,
    work_type: str,
    brief_description: str | None = None,
    proposed_by: str = "analytics",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """提案開立工單(ADR-016)。回 pending_token + dry-run diff;需人類 confirm 才執行。"""
    from cmms.audit import Actor

    async with get_sessionmaker()() as session:
        p = await WorkOrderService(session).propose(
            operation="open_work_order",
            params={
                "asset_id": asset_id,
                "work_type": work_type,
                "brief_description": brief_description,
            },
            proposed_by=Actor.agent(proposed_by),
            idempotency_key=idempotency_key,
        )
        return {
            "pending_token": p.pending_token,
            "status": p.status,
            "dry_run_diff": p.dry_run_diff,
            "expires_at": p.expires_at.isoformat(),
        }


@mcp.tool()
async def propose_close_work_order(
    work_order_no: int,
    proposed_by: str = "analytics",
    idempotency_key: str | None = None,
    confirmed_reason_code: str | None = None,
) -> dict[str, Any]:
    """提案結案工單(ADR-016)。回 pending_token + dry-run diff;需人類 confirm 才執行。

    `confirmed_reason_code`(D6,選填):人工確認的故障真因碼,**efc 軸**
    (equipment_failure_code)、**僅 REACTIVE 工單有意義**(PM 發現故障應另開報修)。confirm
    時才驗(存在 + is_active + REACTIVE);None → 不設。
    """
    from cmms.audit import Actor

    params: dict[str, Any] = {"work_order_no": work_order_no}
    if confirmed_reason_code is not None:
        params["confirmed_reason_code"] = confirmed_reason_code
    async with get_sessionmaker()() as session:
        p = await WorkOrderService(session).propose(
            operation="close_work_order",
            params=params,
            proposed_by=Actor.agent(proposed_by),
            idempotency_key=idempotency_key,
        )
        return {
            "pending_token": p.pending_token,
            "status": p.status,
            "dry_run_diff": p.dry_run_diff,
            "expires_at": p.expires_at.isoformat(),
        }


@mcp.tool()
async def propose_update_item(
    item_code: str,
    name: str | None = None,
    description: str | None = None,
    vendor_part_no: str | None = None,
    bin_location: str | None = None,
    reorder_point: str | None = None,
    reorder_quantity: str | None = None,
    lead_time_weeks: str | None = None,
    unit_cost: str | None = None,
    supplier: str | None = None,
    supplier_org_id: str | None = None,
    weblink: str | None = None,
    comment: str | None = None,
    is_stocked: bool | None = None,
    is_obsolete: bool | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """提案修改備品主檔欄位(ADR-025 Lane 1;agent 只能**提案**,admin confirm 才生效)。

    與 web `inventory_propose_update` 同機制、同 params 形狀 —— confirm 走 `update_item`
    單一寫入路徑(護欄 #1)。回 pending_token + dry-run diff 供 agent 回報使用者「已提案,
    等 admin 核准」。批次 = 重複呼叫(每筆各建一提案);帶 `idempotency_key` 可讓重試冪等
    (省略則自動生成隨機鍵,比照 web:每次呼叫 = 一筆新提案)。

    **身分(比照 F-1 / record_work_order_external_link)**:proposer = `/mcp` transport 閘門
    驗過的使用者(per-user scoped token → human:<id>),**不接受自報字串**;無 transport 身分
    (非 HTTP 直呼)→ 拒(update_item 屬 HUMAN_PROPOSABLE_OPS,提案人必為已驗證 human)。

    未指定的欄位沿用現值(比照 web 表單「預填現值」語意)→ dry-run diff 只列真正的改動,
    避免 agent 漏填某欄反而把主檔清空。未知 item_code → 拒。
    """
    from cmms.audit import Actor
    from cmms.domain.identity.service import mcp_transport_identity
    from cmms.domain.inventory.service import InventoryService
    from cmms.domain.work_order.service import WorkOrderError

    transport = mcp_transport_identity.get()
    if transport is None:
        raise WorkOrderError(
            "propose_update_item requires an authenticated /mcp transport identity"
        )
    proposer = Actor.human(transport[0])  # 驗過的身分,非自報

    def _s(provided: str | None, current: Any) -> str:
        # 提供值優先;否則沿用現值(轉字串,None→"")—— params 全字串,形狀對齊 web propose
        if provided is not None:
            return provided
        return "" if current is None else str(current)

    async with get_sessionmaker()() as session:
        item = await InventoryService(session).get_item(item_code)
        if item is None:
            raise WorkOrderError(f"unknown item_code: {item_code}")
        params = {
            "item_code": item_code,
            "name": _s(name, item.name),
            "description": _s(description, item.description),
            "vendor_part_no": _s(vendor_part_no, item.vendor_part_no),
            "bin_location": _s(bin_location, item.bin_location),
            "reorder_point": _s(reorder_point, item.reorder_point),
            "reorder_quantity": _s(reorder_quantity, item.reorder_quantity),
            "lead_time_weeks": _s(lead_time_weeks, item.lead_time_weeks),
            "unit_cost": _s(unit_cost, item.unit_cost),
            "supplier": _s(supplier, item.supplier),
            "supplier_org_id": _s(supplier_org_id, item.supplier_org_id),
            "weblink": _s(weblink, item.weblink),
            "comment": _s(comment, item.comment),
            "is_stocked": is_stocked if is_stocked is not None else bool(item.is_stocked),
            "is_obsolete": is_obsolete if is_obsolete is not None else bool(item.is_obsolete),
        }
        p = await WorkOrderService(session).propose(
            operation="update_item",
            params=params,
            proposed_by=proposer,
            idempotency_key=(
                idempotency_key or f"mcpitemprop:v1:{item_code}:{secrets.token_urlsafe(6)}"
            ),
        )
        return {
            "pending_token": p.pending_token,
            "status": p.status,
            "dry_run_diff": p.dry_run_diff,
            "expires_at": p.expires_at.isoformat(),
        }


@mcp.tool()
async def confirm_work_order_proposal(pending_token: str) -> dict[str, Any]:
    """確認並執行提案。**confirmer = /mcp token 綁定的已驗證使用者,非參數自報**。

    ★ `/mcp` 上公網後(commit 8d9d811),confirmer 身分一律由 transport 閘門(api/auth.py 對
      每個 /mcp 請求 resolve scoped token)驗出,不接受自報字串 —— 否則持任一有效 token 者即可
      冒名 confirm 自己的提案。domain 再驗該 human 必須是現行 active 的 admin(user_account.role;
      所有 confirm 一律 admin,對齊 ADR-016/ADR-027「agent 不能自我確認」)。
    """
    from cmms.audit import Actor
    from cmms.domain.identity.service import mcp_transport_identity
    from cmms.domain.work_order.models import WorkOrder as _WO
    from cmms.domain.work_order.service import WorkOrderError

    transport = mcp_transport_identity.get()
    if transport is None:
        raise WorkOrderError("confirm requires an authenticated /mcp transport identity")
    async with get_sessionmaker()() as session:
        result = await WorkOrderService(session).confirm(
            pending_token=pending_token, confirmer=Actor.human(transport[0])
        )
        if isinstance(result, _WO):
            return WorkOrderRead.model_validate(result).model_dump(mode="json")
        # 非工單類提案(如 update_item)→ 回通用確認(不硬套 WO schema)
        return {"confirmed": True, "entity": type(result).__name__}


@mcp.tool()
async def reject_work_order_proposal(pending_token: str) -> dict[str, str]:
    """拒絕提案(不執行)。**rejected_by = /mcp token 綁定的使用者,非參數自報**(防冒名/agent
    自行丟棄待審提案)。"""
    from cmms.audit import Actor
    from cmms.domain.identity.service import mcp_transport_identity
    from cmms.domain.work_order.service import WorkOrderError

    transport = mcp_transport_identity.get()
    if transport is None:
        raise WorkOrderError("reject requires an authenticated /mcp transport identity")
    async with get_sessionmaker()() as session:
        await WorkOrderService(session).reject(
            pending_token=pending_token, by=Actor.human(transport[0])
        )
        return {"status": "rejected", "pending_token": pending_token}


@mcp.tool()
async def record_work_order_external_link(
    work_order_no: int,
    external_key: str,
    on_behalf_of: str = "",
    link_type: str = "forwarded",
    agent: str = "hermes",
    scoped_token: str | None = None,
) -> dict[str, Any]:
    """記工單↔Jira MRQ 連結(ADR-020 決策 3;**cmms 不呼叫 Jira**,只落庫「連了什麼」,決策 1)。

    allowlist-shape 守門(決策 8 縱深):`external_key` 必為 MRQ-<n>、`link_type` ∈
    referenced/forwarded/appended。dual attribution:`source_actor=agent:<agent>`(誰轉發)+
    `created_by=human:<id>`(代表誰)。冪等 (wo,system,key,link_type)。

    委派身分優先序(**cmms 驗過的身分勝過裸斷言,防冒名**;ADR-020 決策 5):
    - `scoped_token`:MCP scoped token → resolve 得 human:<id>(**經 cmms 驗證**;無效即拒)。
    - **/mcp transport 閘門驗過的身分**(contextvar;api/auth.py 對每請求驗過的 token 使用者)。
      transport 在場時**勝過** `on_behalf_of` 斷言 —— 持自己 token 者不得宣稱代表他人。
    - `on_behalf_of`:純斷言(信任邊界在 gateway),僅在無 scoped_token 且無 transport 身分時採用。
    三者皆無 → 拒(委派寫入不可匿名)。
    """
    from cmms.audit import Actor
    from cmms.domain.identity.service import (
        IdentityService,
        mcp_transport_identity,
    )
    from cmms.domain.work_order.service import WorkOrderError

    async with get_sessionmaker()() as session:
        if scoped_token:
            resolved = await IdentityService(session).resolve_scoped_token(scoped_token)
            if resolved is None:
                raise WorkOrderError("invalid or expired scoped token")
            behalf = f"human:{resolved[0]}"  # 驗證式委派(cmms 驗過,最高優先)
        elif (transport := mcp_transport_identity.get()) is not None:
            behalf = f"human:{transport[0]}"  # transport 層已驗;勝過 on_behalf_of 斷言(防冒名)
        elif on_behalf_of:
            behalf = f"human:{on_behalf_of}"  # 裸斷言,僅無驗過身分時採用
        else:
            raise WorkOrderError(
                "delegated write requires scoped_token, on_behalf_of, "
                "or an authenticated /mcp transport identity"
            )
        link = await WorkOrderService(session).record_external_link(
            work_order_no=work_order_no,
            external_key=external_key,
            link_type=link_type,
            actor=Actor.agent(agent),
            on_behalf_of=behalf,
        )
        return {
            "id": link.id,
            "work_order_no": link.work_order_no,
            "system": link.system,
            "external_key": link.external_key,
            "link_type": link.link_type,
        }


@mcp.tool()
async def forward_work_orders_to_mrq(
    work_order_nos: list[int],
    summary: str,
    description: str,
    dry_run: bool = True,
    idempotency_key: str | None = None,
    agent: str = "hermes",
) -> dict[str, Any]:
    """把一或多張工單綜合成**一張 Jira MRQ** + 所有工作紀錄按時間逐則 comment(ADR-020 決策 1/7)。

    ★ 用法(教 agent):**先 dry_run=true** 讀工單 + 全部 work_order_note,綜合 `summary`/`description`
    (以使用者 `jira_output_locale` 撰寫)→ 把預覽(工單清單、note 總數、readiness 警語)呈給使用者
    → **使用者口頭同意後**才 dry_run=false 實際開單。連結建立後,這些工單日後每新增一筆工作紀錄會
    **自動同步**到同一張 MRQ(無需再呼叫本工具);請一併告知使用者。

    - `work_order_nos`:只准工具真實回傳過的工單號(EID/工單不猜,ADR-027)。
    - `summary`/`description`:MRQ 標題與主旨(Hermes 綜合生成,詮釋層);comment 為 note 原文忠實
      映射(事實層,自動同步不翻譯)。**只寫 MRQ**,不得碰其他 Jira project / issue / 欄位(決策 8)。
    - `idempotency_key`:重跑防重(同 key 不重開 MRQ);建議每次真實送出帶一個穩定鍵。

    **身分(比照 F-1)**:PAT 主人 = `/mcp` transport 閘門驗過的使用者(用自己的 Jira PAT 寫,Jira
    端原生歸屬本人);拒自報、拒匿名。admin 與 engineer 皆可(PAT 是自己的)。
    """
    from cmms.audit import Actor
    from cmms.domain.identity.service import mcp_transport_identity
    from cmms.domain.jira_sync.service import JiraSyncService

    transport = mcp_transport_identity.get()
    if transport is None:
        from cmms.domain.jira_sync.service import JiraSyncError

        raise JiraSyncError(
            "forward_work_orders_to_mrq requires an authenticated /mcp transport identity"
        )
    async with get_sessionmaker()() as session:
        result = await JiraSyncService(session).forward_work_orders_to_mrq(
            work_order_nos=work_order_nos,
            summary=summary,
            description=description,
            acting_user=transport[0],  # PAT 主人 = 驗過的身分,非自報
            actor=Actor.agent(agent),
            dry_run=dry_run,
            idempotency_key=idempotency_key,
        )
        return result.to_dict()


# ---- Contacts 切片:讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----
# 人員 PII 治理(06-contacts §3):批次列舉只回非 PII 摘要;單筆查詢回完整(別名自動解析)。


@mcp.tool()
async def get_organization(org_id: str) -> dict[str, Any] | None:
    """查單一組織(供應商/承包商/客戶/內部)。傳入 org_id(如 CMA、SF、CMB)。"""
    async with get_sessionmaker()() as session:
        org = await ContactsService(session).get_organization(org_id)
        return OrganizationRead.model_validate(org).model_dump() if org else None


@mcp.tool()
async def list_organizations(
    org_type: str | None = None,
    is_active: bool | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列組織,可依類型(Supplier/Contractor/Customer/Internal)/ 啟用狀態 / 名稱關鍵字過濾。"""
    async with get_sessionmaker()() as session:
        orgs = await ContactsService(session).list_organizations(
            org_type=org_type, is_active=is_active, search=search, limit=limit
        )
        return [OrganizationRead.model_validate(o).model_dump() for o in orgs]


@mcp.tool()
async def get_person(person_id: str) -> dict[str, Any] | None:
    """查單一人員(含聯絡資料)。傳入 contactid(如 SAMWU99);別名(如 SMWU)自動解析回 canonical。"""
    async with get_sessionmaker()() as session:
        person = await ContactsService(session).resolve_person(person_id)
        return PersonRead.model_validate(person).model_dump() if person else None


@mcp.tool()
async def list_persons(
    org_id: str | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """列人員(非 PII 摘要,06-contacts §3),可依組織 / 原始分類 / 姓名關鍵字過濾。"""
    async with get_sessionmaker()() as session:
        persons = await ContactsService(session).list_persons(
            org_id=org_id, category=category, search=search, limit=limit
        )
        return [PersonSummary.model_validate(p).model_dump() for p in persons]


# ---- Attachment 切片(#7,ADR-019):讀取工具 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
async def get_attachments(
    owner_type: str, owner_id: str, limit: int = 50
) -> list[dict[str, Any]]:
    """列某 owner 的附件(照片)。owner_type=inventory_item/work_order/asset;
    回 metadata + 短期 presigned url(R2 內部座標不外洩)。"""
    async with get_sessionmaker()() as session:
        svc = AttachmentService(session)
        rows = await svc.list_attachments(owner_type, owner_id, limit=limit)
        out: list[dict[str, Any]] = []
        for a in rows:
            url, ttl = svc.presigned_url(a)
            base = AttachmentRead.model_validate(a).model_dump(mode="json")
            out.append({**base, "url": url, "url_expires_in": ttl})
        return out


# ---- Help 切片(內部規格):唯讀 SOP 目錄 = 領域操作(ADR-003);讀取直接開放(ADR-004)----


@mcp.tool()
def list_help_docs() -> list[dict[str, Any]]:
    """Returns available how-to guides (SOPs) for cmms features.

    Use when the user asks how to do or set up something in cmms (bind Telegram,
    create a Jira token, report an issue, etc.). Answer with a concise summary and
    ALWAYS attach the url (a site-relative /app path) as a markdown link.

    url 為站內相對路徑(`/app/help/<slug>`),非絕對 URL:dock 安全渲染器
    (assistant_render)白名單只放行 /app 相對路徑,絕對 URL 會退成純文字;
    Telegram 端由 absolutize_app_links 自動補 base —— 相對路徑兩個前端都對。
    """
    from cmms.web.help_docs import HELP_DOCS

    return [
        {
            "slug": doc.slug,
            "title": doc.title,
            "summary": doc.summary,
            "url": f"/app/help/{doc.slug}",
        }
        for doc in HELP_DOCS
    ]


# 寫入類須走 gated write(dry-run + 確認 / 兩階段外部確認 ADR-004/016),並記 source_actor。
# 高風險(作廢、Key Change、Mass Delete)不在 MCP 暴露。
# PM 產生、工單狀態轉移、庫存異動(receive/issue/adjust)等寫入 = 後續切片的 governed write。


def main() -> None:
    # 預設 stdio(本地);雲端部署改 Streamable HTTP。
    mcp.run()


if __name__ == "__main__":
    main()
