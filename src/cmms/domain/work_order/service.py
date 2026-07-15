"""WorkOrderService — WorkOrder 切片的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:get / list。
寫入(#4b-1):
- 載入器用:upsert lookup / work_order(idempotent,歷史 downtime 已由 transform 算好)。
- 狀態機 governed 操作:open / start / hold / resume / complete / close / void / cancel。
  每次轉移寫 `work_order_status_history`(系統自動抓時間);close 時依 history 精算 downtime
  (只計生產時段、扣非生產 00:00–09:00;ON_HOLD 是否計入依 hold_reason)。
- 領料:`issue_part_to_work_order` — 記 work_order_part + 經 InventoryService 開 stock_transaction
  扣 on_hand(ADR-005),帶 idempotency_key 防重(ADR-006)。
gated write 外部層(ADR-016 兩階段 propose/confirm + ADR-017 on-box Profile B)留 4b-2。
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, or_, select, union
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.asset.models import Asset, AssetOwner
from cmms.domain.asset.service import _clean_names
from cmms.domain.base import DomainService, clean_person_name
from cmms.domain.failure_vocab.models import EquipmentFailureCode
from cmms.domain.identity.service import (
    AuthorizationError,
    assert_active_admin,
    is_active_admin,
    is_operator,
)
from cmms.domain.inventory.models import InventoryItem
from cmms.domain.inventory.service import InventoryError, InventoryService
from cmms.domain.notify.service import enqueue_work_order_notifications
from cmms.domain.pm_schedule.models import PmSchedule
from cmms.domain.pm_schedule.transform import add_interval, effective_generation_date
from cmms.domain.task.models import Task
from cmms.domain.work_order.downtime import segment_is_downtime
from cmms.domain.work_order.models import (
    JiraOutbox,
    PendingProposal,
    WoHoldReason,
    WoNoteType,
    WorkOrder,
    WorkOrderAssignee,
    WorkOrderExternalLink,
    WorkOrderNote,
    WorkOrderPart,
    WorkOrderStatusHistory,
    WoStatus,
)
from cmms.domain.work_order.onbox import KeyResolver, OnboxVerificationError, verify_onbox_jws
from cmms.domain.work_order.transform import (
    WorkOrderImport,
    productive_minutes,
    to_taipei_naive,
)

# canonical 狀態機:允許的轉移(#4b;見 02-work-orders §3)。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"IN_PROGRESS", "CANCELLED", "VOIDED"},
    "IN_PROGRESS": {"ON_HOLD", "COMPLETED", "VOIDED"},
    "ON_HOLD": {"IN_PROGRESS", "COMPLETED", "VOIDED"},
    "COMPLETED": {"CLOSED", "IN_PROGRESS"},  # 試跑不過可從 COMPLETED 退回續修
    "CLOSED": set(),
    "CANCELLED": set(),
    "VOIDED": set(),
}
TERMINAL_STATUSES = {"CLOSED", "CANCELLED", "VOIDED"}

# ADR-016 兩階段可提案的操作(Profile A;低/中風險,不放寬高風險如 void/Key Change)。
PROPOSABLE_OPS = frozenset({"open_work_order", "close_work_order"})
# ADR-025 Lane 1 人類提案面:工程師可「請求作廢」+「品項修改提案」(admin confirm 才執行;
# Jordan 2026-07-04 裁決 #3 後續:主檔修改的 engineer 路徑 = 提案,與作廢請求同一機制)。
# 僅放寬給 human proposer —— agent(Profile A)維持 PROPOSABLE_OPS,不得提案高風險操作。
HUMAN_PROPOSABLE_OPS = PROPOSABLE_OPS | frozenset({"void_work_order", "update_item"})

# 歷史領料回填(part_issue_backfill)的發起者。回填以 adjust_on_hand=False 記帳、**從未扣 on_hand**
# (eMaint onhand snapshot 已含歷史扣減)。因此對回填帳做「取消/改量」會 RETURN 反灌 on_hand →
# 憑空灌爆庫存。domain 以此標記顯式擋下(cancel/update_part_issue;inventory 直領同理),
# 不再只靠「終態工單」推論(legacy 有 OPEN 單掛回填 part,終態守門不保證擋到)。
BACKFILL_ACTOR = Actor.human("data-migration")

# update_item 提案 params 的欄位型別(propose 先驗、confirm 統一轉型;JSONB 只存原始字串/布林)
_ITEM_STR_FIELDS = (
    "name", "description", "vendor_part_no", "bin_location", "supplier",
    "weblink", "comment", "supplier_org_id",
)
_ITEM_DEC_FIELDS = ("reorder_point", "reorder_quantity", "unit_cost")
_ITEM_BOOL_FIELDS = ("is_stocked", "is_obsolete")
DEFAULT_PROPOSAL_TTL_SECONDS = 3600  # 提案預設 1h 過期
HUMAN_PROPOSAL_TTL_SECONDS = 60 * 60 * 24 * 7  # 人類提案 7 天(等 admin 排入審核,非機器節奏)

# ADR-020 外部連結:allowlist-shape 守門(決策 8 縱深;primary enforcement 在 gateway)。
EXTERNAL_LINK_SYSTEMS = frozenset({"jira"})
EXTERNAL_LINK_TYPES = frozenset({"referenced", "forwarded", "appended"})
_HOLD_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,40}$")  # 新 hold reason code 形狀守門
_MRQ_KEY_FULL = re.compile(r"^MRQ-\d+$")  # 守門:external_key 必為 MRQ-<n>
_MRQ_KEY_FIND = re.compile(r"MRQ-\d+")  # 回填:從 external_ref 抽所有 MRQ-<n>


def _bare_user_id(actor_value: str | None) -> str | None:
    """`human:<id>` → `<id>`(PAT vault 主人);非 human 前綴 / None → None(無法歸屬 PAT)。"""
    if actor_value and actor_value.startswith("human:"):
        return actor_value.split(":", 1)[1]
    return None


def _active_window_end(
    history: list[WorkOrderStatusHistory], closed_at: datetime | None, now: datetime
) -> datetime:
    """工單「活躍窗」的結束時間(A3 視窗查詢用):**首個 COMPLETED 或終態**的 changed_at。

    語意 = 工單「處理中/等待中」的活躍區間何時結束(達成 COMPLETED 或落終態即結束)。
    `history` 須已依 `changed_at` 升冪排序。無此類轉移(遷移單無 history / 仍未完工):
    有 `closed_at` → 用之(遷移單 opened_at→closed_at fallback);否則仍 open → 至今(`now`)。
    """
    for h in history:  # 已排序:首個命中即活躍窗尾
        if h.to_status == "COMPLETED" or h.to_status in TERMINAL_STATUSES:
            return h.changed_at
    return closed_at if closed_at is not None else now


class WorkOrderError(Exception):
    """工單寫入錯誤(找不到、狀態不合法等)。"""


class InvalidTransition(WorkOrderError):
    """不合法的狀態轉移。"""


class PartIssueOutcome(StrEnum):
    """`backfill_part_issue` 的結果(loader 據此計數;FK 落空非錯誤,不 raise)。"""

    INSERTED = "inserted"  # 掛工單(work_order_part + stock_transaction)
    INSERTED_ASSET = "inserted_asset"  # 救援:WO 不存在但 compid 有效 → 掛設備(ADR-024,無 wo_part)
    DUPLICATE = "duplicate"  # idempotency_key 命中 → 跳過(work_order_part 也不重記,互鎖)
    MISSING_WORK_ORDER = "missing_wo"  # WO 不存在且無有效 compid → 不可救,log+skip
    MISSING_ITEM = "missing_item"  # FK 落空(item 不在 inventory_item)→ log+skip(不可救)


@dataclass(frozen=True, slots=True)
class PmGenerationResult:
    """自動排程器單筆到期 PM 的處理結果(ADR-021)。

    `created=True` = 新生成工單;`created=False` 且 `error is None` = 冪等命中既有未結案工單;
    `error` 非空 = 該筆失敗(已隔離、不影響其餘到期 PM)。
    """

    pm_id: str
    work_order_no: int | None
    created: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ConfirmedReasonOption:
    """結單「確認故障原因」下拉候選一列(efc 軸,唯讀)。

    `used=True` = 本設備既往工單用過此碼(排序置頂、UI 標「曾用」);`descr` = 人讀說明
    (MES 源頭英文,不翻譯 —— 詞彙軸權威不竄改;供多關鍵字搜尋 + 下拉顯示,None = 種子未附)。
    存入 DB 的仍是 `code`;descr 僅顯示層。
    """

    code: str
    descr: str | None
    used: bool


class WorkOrderService(DomainService):
    # ---- operator RBAC 縱深(白名單外的寫入一律拒;RBAC 在 domain 強制,護欄 #RBAC)----

    async def _assert_not_operator(self, actor: Actor, op: str) -> None:
        """operator(iPad 產線共用帳號)白名單外的寫入一律拒(route 藏按鈕不算授權)。

        operator 只准:① 開 REACTIVE 報修 ② 取消自己開的 OPEN 誤報 ③ 讀取 + 改自己密碼/語言。
        其餘任何 governed 寫入(狀態機 / 領料 / 指派 / 連結 / 提案 / PM 生成…)呼叫此閘擋下。
        `op` 帶入操作名,便於測試斷言與稽核。agent/scheduler/on-box 路徑一律非 operator,不受影響。"""
        if await is_operator(self.session, actor):
            raise AuthorizationError(f"operator role cannot perform {op}")

    # ---- 讀取(ADR-004:讀取低風險,直接開放)----

    async def get_work_order(self, work_order_no: int) -> WorkOrder | None:
        return await self.session.get(WorkOrder, work_order_no)

    async def list_work_orders(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        asset_id: str | None = None,
        work_type: str | None = None,
        status: str | None = None,
        assigned_vendor: str | None = None,
        assigned_person: str | None = None,
        opened_on_or_after: date | None = None,
        opened_on_or_before: date | None = None,
        statuses: list[str] | None = None,
        search: str | None = None,
    ) -> list[WorkOrder]:
        """讀工單清單(ADR-004)。`statuses` = 狀態群組(分類標籤,in-clause);
        `search` = 跨欄自由查詢(EID / owner / vendor / MRQ / 描述 ilike;純數字亦比對工單號)。
        """
        stmt = select(WorkOrder)
        if asset_id is not None:
            stmt = stmt.where(WorkOrder.asset_id == asset_id)
        if work_type is not None:
            stmt = stmt.where(WorkOrder.work_type == work_type)
        if status is not None:
            stmt = stmt.where(WorkOrder.status == status)
        if statuses:
            stmt = stmt.where(WorkOrder.status.in_(statuses))
        if assigned_vendor is not None:
            stmt = stmt.where(WorkOrder.assigned_vendor == assigned_vendor)
        if assigned_person is not None:
            # 0031「我的工單」:比對 work_order_assignee 任一負責人(非只 denormalized 首位)。
            # 回填令交叉表完整,無需 fallback 相容欄。
            stmt = stmt.where(
                select(WorkOrderAssignee.person_name)
                .where(
                    WorkOrderAssignee.work_order_no == WorkOrder.work_order_no,
                    WorkOrderAssignee.person_name == assigned_person,
                )
                .exists()
            )
        if opened_on_or_after is not None:
            stmt = stmt.where(WorkOrder.opened_date >= opened_on_or_after)
        if opened_on_or_before is not None:
            stmt = stmt.where(WorkOrder.opened_date <= opened_on_or_before)
        if search and search.strip():
            s = search.strip()
            like = f"%{s}%"
            terms = [
                WorkOrder.asset_id.ilike(like),
                WorkOrder.brief_description.ilike(like),
                WorkOrder.assigned_person.ilike(like),
                WorkOrder.assigned_vendor.ilike(like),
                WorkOrder.external_ref.ilike(like),
            ]
            if s.isdigit():
                terms.append(WorkOrder.work_order_no == int(s))
            stmt = stmt.where(or_(*terms))
        stmt = stmt.order_by(WorkOrder.work_order_no.desc()).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def get_status_history(self, work_order_no: int) -> list[WorkOrderStatusHistory]:
        stmt = (
            select(WorkOrderStatusHistory)
            .where(WorkOrderStatusHistory.work_order_no == work_order_no)
            .order_by(WorkOrderStatusHistory.changed_at, WorkOrderStatusHistory.id)
        )
        return list((await self.session.scalars(stmt)).all())

    # ---- 工單負責人(多負責人,0031;work_order_assignee 交叉表)----

    async def get_assignees(self, work_order_no: int) -> list[str]:
        """一張工單的所有負責人(依 position 排序;讀取,開放 ADR-004)。首位 == 相容欄
        `assigned_person`(denormalized)。無 → 空清單。"""
        rows = await self.session.execute(
            select(WorkOrderAssignee.person_name)
            .where(WorkOrderAssignee.work_order_no == work_order_no)
            .order_by(WorkOrderAssignee.position, WorkOrderAssignee.person_name)
        )
        return [r[0] for r in rows]

    async def assignees_map(self, work_order_nos: list[int]) -> dict[int, list[str]]:
        """批次取多張工單的負責人(work_order_no → [names],依 position),一查免 N+1(清單卡)。"""
        nos = [n for n in dict.fromkeys(work_order_nos) if n is not None]
        if not nos:
            return {}
        rows = await self.session.execute(
            select(WorkOrderAssignee.work_order_no, WorkOrderAssignee.person_name)
            .where(WorkOrderAssignee.work_order_no.in_(nos))
            .order_by(
                WorkOrderAssignee.work_order_no,
                WorkOrderAssignee.position,
                WorkOrderAssignee.person_name,
            )
        )
        out: dict[int, list[str]] = {}
        for no, name in rows:
            out.setdefault(no, []).append(name)
        return out

    async def _asset_owner_names(self, asset_id: str) -> list[str]:
        """設備負責人清單(依 position;供 REACTIVE 報修 / PM 生成未明確指派時衍生 assignee)。"""
        rows = await self.session.execute(
            select(AssetOwner.person_name)
            .where(AssetOwner.asset_id == asset_id)
            .order_by(AssetOwner.position, AssetOwner.person_name)
        )
        return [r[0] for r in rows]

    async def _replace_assignees(
        self, wo: WorkOrder, names: list[str], actor: Actor
    ) -> None:
        """在呼叫端 write() 交易內,把一張工單的負責人整組替換為 `names`(已正規化清單)。

        差異更新 work_order_assignee(刪除缺、插入新、更新 position)+ 同步相容欄
        `wo.assigned_person`=首位(或 None)。不自開交易 / 不驗權限(呼叫端負責)。
        """
        existing = {
            r.person_name: r
            for r in (
                await self.session.scalars(
                    select(WorkOrderAssignee).where(
                        WorkOrderAssignee.work_order_no == wo.work_order_no
                    )
                )
            ).all()
        }
        desired = {name: pos for pos, name in enumerate(names)}
        for name, row in list(existing.items()):
            if name not in desired:
                await self.session.delete(row)
        for name, pos in desired.items():
            row = existing.get(name)
            if row is None:
                self.session.add(
                    WorkOrderAssignee(
                        work_order_no=wo.work_order_no,
                        person_name=name,
                        position=pos,
                        created_by=actor.value,
                        source_actor=actor.value,
                    )
                )
            elif row.position != pos:
                row.position = pos
                row.updated_by = actor.value
                row.source_actor = actor.value
        wo.assigned_person = names[0] if names else None

    async def list_active_in_window(
        self,
        *,
        start: datetime,
        end: datetime,
        asset_id: str | None = None,
        cap: int = 2000,
        at: datetime | None = None,
    ) -> tuple[list[tuple[WorkOrder, list[WorkOrderStatusHistory]]], bool]:
        """A3 視窗查詢(Analytics 消費端需求;讀取開放 ADR-004):列「活躍於 [start, end] 窗內」的工單。

        **窗語意 = 活躍於窗內(非 opened 於窗內)**:工單活躍窗 = `opened_at` → **首個
        COMPLETED 或終態**(自 status_history);遷移單無 history → `opened_at`→`closed_at`;
        仍 open → 至今。活躍窗與 [start, end] **相交** 即回。

        實作(成本取向):① SQL 粗篩 `opened_at <= end AND (closed_at IS NULL OR
        closed_at >= start)`(closed_at ≥ 首個終態時間 → 粗篩為安全超集,不漏)② status_history
        以**單次 IN 查詢批載**(避免 N+1)③ Python 精修:算每單活躍窗尾、精確判相交。

        回 `(rows, truncated)`:`rows` = 通過精修的 `(工單, 該單 status_history 升冪)`;
        `truncated` = 粗篩候選 > cap(v1 固定上限,回應誠實標示被截斷)。`start`/`end` 由呼叫端
        以 tz-aware 傳入(廠區台北時間定位),與 DB timestamptz 跨時區比較正確。
        """
        stmt = select(WorkOrder).where(
            WorkOrder.opened_at.is_not(None),
            WorkOrder.opened_at <= end,
            or_(WorkOrder.closed_at.is_(None), WorkOrder.closed_at >= start),
        )
        if asset_id is not None:
            stmt = stmt.where(WorkOrder.asset_id == asset_id)
        stmt = stmt.order_by(WorkOrder.work_order_no.desc()).limit(cap + 1)
        coarse = list((await self.session.scalars(stmt)).all())
        truncated = len(coarse) > cap
        coarse = coarse[:cap]

        # status_history 一次 IN 批載(N+1 防治),再依 wo 分組(每組已依 changed_at 升冪)
        wo_nos = [w.work_order_no for w in coarse]
        hist_by_wo: dict[int, list[WorkOrderStatusHistory]] = {no: [] for no in wo_nos}
        if wo_nos:
            h_stmt = (
                select(WorkOrderStatusHistory)
                .where(WorkOrderStatusHistory.work_order_no.in_(wo_nos))
                .order_by(WorkOrderStatusHistory.changed_at, WorkOrderStatusHistory.id)
            )
            for h in (await self.session.scalars(h_stmt)).all():
                hist_by_wo[h.work_order_no].append(h)

        now = at or self._now()
        out: list[tuple[WorkOrder, list[WorkOrderStatusHistory]]] = []
        for w in coarse:
            history = hist_by_wo[w.work_order_no]
            win_end = _active_window_end(history, w.closed_at, now)
            # 活躍窗 [opened_at, win_end] 與 [start, end] 相交(opened_at 已由粗篩保證非 None)
            if w.opened_at is not None and w.opened_at <= end and win_end >= start:
                out.append((w, history))
        return out, truncated

    async def get_parts(self, work_order_no: int) -> list[WorkOrderPart]:
        """工單領料清單(未軟刪;0022 取消領料後從此清單消失)。讀取,開放。"""
        stmt = (
            select(WorkOrderPart)
            .where(
                WorkOrderPart.work_order_no == work_order_no,
                WorkOrderPart.deleted_at.is_(None),
            )
            .order_by(WorkOrderPart.id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_notes(self, work_order_no: int) -> list[WorkOrderNote]:
        """工單工作日誌(§1.6),依 occurred_at 升冪 = 時間線順序。已軟刪(0022)排除。讀取,開放。"""
        stmt = (
            select(WorkOrderNote)
            .where(
                WorkOrderNote.work_order_no == work_order_no,
                WorkOrderNote.deleted_at.is_(None),
            )
            .order_by(WorkOrderNote.occurred_at, WorkOrderNote.id)
        )
        return list((await self.session.scalars(stmt)).all())

    # ---- 工作日誌寫入(append-only,§1.6;低風險 additive,不走 gated-write)----

    async def add_note(
        self,
        work_order_no: int,
        *,
        entry_type: str,
        body: str,
        actor: Actor,
        occurred_at: datetime | None = None,
        status_history_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> WorkOrderNote:
        """追加一筆工作日誌(§1.6)。手動與 Hermes NL 輸入同走此路徑(單一寫入路徑,護欄 #1)。

        `author` = `source_actor` = actor(人/agent 分流,ADR-005);`occurred_at` 預設 now(UTC)。
        冪等(ADR-006):同 `idempotency_key` 回既有、不重記。照片另掛
        `attachment(owner_type='work_order_note', owner_id=str(note.id))` → 隨此筆時間戳。

        operator 閘:只允許在**自己開的、仍 OPEN** 的工單上加 `report` 筆(= 報修當下 /
        報修補充照片這一種情境,web report_submit 開單後立刻掛照片);其餘一律拒。
        """
        if await is_operator(self.session, actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if (
                entry_type != "report"
                or wo is None
                or wo.created_by != actor.value
                or wo.status != "OPEN"
            ):
                raise AuthorizationError(
                    "operator can only add report notes to their own open work orders"
                )
        if idempotency_key is not None:
            existing = await self.session.scalar(
                select(WorkOrderNote).where(WorkOrderNote.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing
        if await self.session.get(WorkOrder, work_order_no) is None:
            raise WorkOrderError(f"work order {work_order_no} not found")
        note = WorkOrderNote(
            work_order_no=work_order_no,
            entry_type=entry_type,
            body=body,
            author=actor.value,
            occurred_at=occurred_at or datetime.now(UTC),
            status_history_id=status_history_id,
            idempotency_key=idempotency_key,
            created_by=actor.value,
            source_actor=actor.value,
        )
        async with self.write(actor):
            self.session.add(note)
            await self.session.flush()
            note_id = note.id
            # ADR-020 決策 1 修訂:工單已連 forwarded/appended MRQ → 為本筆 note 排 outbox(同交易,
            # 單一寫入路徑)。背景 flush 立即送 comment(需求 ②「新增紀錄即自動同步」)。
            await self._enqueue_note_to_outbox(note)
        return await self.session.get(WorkOrderNote, note_id)

    async def _enqueue_note_to_outbox(self, note: WorkOrderNote) -> int:
        """為一筆 note 對其工單所有 forwarded/appended MRQ 連結各排一列 jira_outbox(pending)。

        在呼叫端 write() 交易內執行(不自開交易)。唯一鍵 (note_id, external_key) →
        on conflict do nothing(冪等,重排安全)。`on_behalf_user` = 連結建立者(PAT 主人);
        連結 created_by 非 human:<id>(無法歸屬 PAT)→ 跳過該連結。回實際新排列數。
        """
        links = (
            await self.session.scalars(
                select(WorkOrderExternalLink).where(
                    WorkOrderExternalLink.work_order_no == note.work_order_no,
                    WorkOrderExternalLink.system == "jira",
                    WorkOrderExternalLink.link_type.in_(("forwarded", "appended")),
                    WorkOrderExternalLink.removed_at.is_(None),
                )
            )
        ).all()
        enqueued = 0
        seen: set[str] = set()
        for link in links:
            if link.external_key in seen:
                continue  # 同 MRQ 多個 link_type(forwarded+appended)只排一列
            on_behalf = _bare_user_id(link.created_by)
            if on_behalf is None:
                continue  # 無法歸屬 PAT 主人 → 不排(誠實跳過,不亂記)
            seen.add(link.external_key)
            result = await self.session.execute(
                pg_insert(JiraOutbox)
                .values(
                    note_id=note.id,
                    work_order_no=note.work_order_no,
                    external_key=link.external_key,
                    on_behalf_user=on_behalf,
                    status="pending",
                    created_by=note.created_by,
                    source_actor=note.source_actor,
                )
                .on_conflict_do_nothing(index_elements=["note_id", "external_key"])
            )
            enqueued += result.rowcount or 0
        return enqueued

    async def get_note(self, note_id: int) -> WorkOrderNote | None:
        return await self.session.get(WorkOrderNote, note_id)

    async def list_hold_reasons(self) -> list[WoHoldReason]:
        """等待原因受控詞彙(讀取,開放)。UI 下拉由此供給 —— 單一來源是 DB lookup,
        新原因走 migration/seed 即自動出現(review f14cf8d:不再硬編在模板)。"""
        rows = list((await self.session.scalars(select(WoHoldReason))).all())
        # WAITING_* 排前(最常用),其餘按 code;顯示文字由 i18n holdreason.<code> 決定
        return sorted(rows, key=lambda r: (not r.code.startswith("WAITING"), r.code))

    async def list_statuses(self) -> list[WoStatus]:
        """canonical 狀態機受控詞彙(唯讀,ADR-004)。admin 詞彙頁純顯示。"""
        stmt = select(WoStatus).order_by(WoStatus.rank, WoStatus.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_note_types(self) -> list[WoNoteType]:
        """工單日誌條目型別受控詞彙(唯讀,ADR-004)。admin 詞彙頁純顯示。"""
        stmt = select(WoNoteType).order_by(WoNoteType.code)
        return list((await self.session.scalars(stmt)).all())

    # ---- 受控詞彙 governed 編輯(admin 面;僅 wo_hold_reason 開放增改,不刪)----

    async def add_hold_reason(
        self, code: str, label: str, *, is_downtime: bool, actor: Actor
    ) -> WoHoldReason:
        """新增一個等待原因(admin 面;migration 0019 之外的 UI 入口)。

        驗證:`code` 須 UPPER_SNAKE(^[A-Z][A-Z0-9_]{2,40}$)、`label` 非空、`code` 不得已存在;
        `is_downtime` 決定該段是否計入 downtime 精算(見 WoHoldReason)。admin 限定在 domain
        強制(guardrail #RBAC 縱深;route 藏頁不是授權)。governed(經單一寫入路徑)。
        """
        code = (code or "").strip().upper()
        label = (label or "").strip()
        if not _HOLD_REASON_CODE.match(code):
            raise WorkOrderError(
                "hold reason code must be UPPER_SNAKE, 3–41 chars (e.g. WAITING_TOOLING)"
            )
        if not label:
            raise WorkOrderError("hold reason label cannot be empty")
        await assert_active_admin(self.session, actor)
        if await self.session.get(WoHoldReason, code) is not None:
            raise WorkOrderError(f"hold reason {code} already exists")
        async with self.write(actor):
            self.session.add(WoHoldReason(code=code, label=label, is_downtime=is_downtime))
        return await self.session.get(WoHoldReason, code)

    async def update_hold_reason(
        self, code: str, *, label: str, is_downtime: bool, actor: Actor
    ) -> WoHoldReason:
        """更新既有等待原因的 label / is_downtime(admin 面)。**不新增、不刪除**。

        `is_downtime` 為 downtime 引擎語意:改變只影響之後結案的精算,**不回溯**已結案工單
        (歷史 downtime 於 close 時鎖定)。label 非空;code 不存在 → 錯。admin 限定在 domain。
        """
        code = (code or "").strip().upper()
        label = (label or "").strip()
        if not label:
            raise WorkOrderError("hold reason label cannot be empty")
        await assert_active_admin(self.session, actor)
        row = await self.session.get(WoHoldReason, code)
        if row is None:
            raise WorkOrderError(f"hold reason {code} not found")
        async with self.write(actor):
            row.label = label
            row.is_downtime = is_downtime
        return row

    async def update_note(
        self,
        note_id: int,
        *,
        body: str,
        actor: Actor,
        work_order_no: int | None = None,
    ) -> WorkOrderNote:
        """更正一筆既有日誌(Jordan 2026-07-03:記錯要能改)。governed、限本人(或 admin 代改)。

        就地更新 body;`updated_at`/`updated_by`(AuditMixin)記「誰、何時改過」,UI 據以顯示
        「已編輯」。同步錨(§1.6/ADR-020 決策 7)不變:note.id / idempotency_key 不動,
        gateway 端日後以 comment 更新對齊。內容未變 → no-op(不污染稽核欄)。

        review f14cf8d 加固:① admin 身分由 domain 解析(不信 caller 自報 as_admin)
        ② **終態凍結** —— 工單 CLOSED/CANCELLED/VOIDED 後本人不得再改(downtime 佐證是
        已審結紀錄),僅 admin 可更正 ③ `work_order_no` 提供時強制歸屬。
        """
        await self._assert_not_operator(actor, "update_note")
        body = body.strip()
        if not body:
            raise WorkOrderError("note body cannot be empty")
        note = await self.session.get(WorkOrderNote, note_id)
        if note is None:
            raise WorkOrderError(f"work_order_note {note_id} not found")
        if work_order_no is not None and note.work_order_no != work_order_no:
            raise WorkOrderError(f"note {note_id} does not belong to wo {work_order_no}")
        admin = await is_active_admin(self.session, actor)
        if note.author != actor.value and not admin:
            raise WorkOrderError("only the author (or an admin) can edit a note")
        wo = await self.session.get(WorkOrder, note.work_order_no)
        if wo is not None and wo.status in TERMINAL_STATUSES and not admin:
            raise WorkOrderError(
                f"wo {note.work_order_no} is terminal ({wo.status}); notes are frozen"
            )
        if note.body == body:
            return note
        async with self.write(actor):
            note.body = body
            note.updated_by = actor.value
            note.source_actor = actor.value
        return note

    async def update_brief_description(
        self,
        work_order_no: int,
        *,
        brief_description: str | None,
        actor: Actor,
    ) -> WorkOrder:
        """補填 / 更正工單「故障簡述」(Jordan 2026-07-06:現場作業員開單常留空,工程師事後補填)。

        governed(單一寫入路徑 + 全稽核)。空字串 → 存 None(語意=未填)。**終態凍結**:
        工單 CLOSED/CANCELLED/VOIDED 後限 admin 更正(比照日誌終態凍結,downtime 佐證是
        已審結紀錄);非終態 = 任何登入者(web 層已拒匿名)可編。內容未變 → no-op(不污染稽核欄)。
        """
        await self._assert_not_operator(actor, "update_brief_description")
        value = (brief_description or "").strip() or None
        wo = await self.session.get(WorkOrder, work_order_no)
        if wo is None:
            raise WorkOrderError(f"work order {work_order_no} not found")
        if wo.status in TERMINAL_STATUSES and not await is_active_admin(self.session, actor):
            raise WorkOrderError(
                f"wo {work_order_no} is terminal ({wo.status}); brief is frozen (admin only)"
            )
        if wo.brief_description == value:
            return wo
        async with self.write(actor):
            wo.brief_description = value
            wo.updated_by = actor.value
            wo.source_actor = actor.value
        return wo

    async def delete_note(
        self, note_id: int, actor: Actor, *, work_order_no: int | None = None
    ) -> int:
        """刪一筆工作日誌(Jordan 2026-07-05 裁決 #1:記錯要能刪)。回所屬 work_order_no。

        **軟刪(0022)**:填 `deleted_at`/`deleted_by`(護欄 #4:誰、何時刪可查),`list_notes`
        排除;照片 attachment **不動**(R2 永留政策),隨 note 一起從時間線消失。權限比照
        `update_note`:限本人或 admin;工單終態(CLOSED/CANCELLED/VOIDED)後**限 admin**
        (downtime 佐證是已審結紀錄)。冪等:已刪 → no-op 回其 work_order_no。
        """
        await self._assert_not_operator(actor, "delete_note")
        note = await self.session.get(WorkOrderNote, note_id)
        if note is None:
            raise WorkOrderError(f"work_order_note {note_id} not found")
        if work_order_no is not None and note.work_order_no != work_order_no:
            raise WorkOrderError(f"note {note_id} does not belong to wo {work_order_no}")
        owner_wo = note.work_order_no
        if note.deleted_at is not None:
            return owner_wo  # 冪等
        admin = await is_active_admin(self.session, actor)
        if note.author != actor.value and not admin:
            raise WorkOrderError("only the author (or an admin) can delete a note")
        wo = await self.session.get(WorkOrder, owner_wo)
        if wo is not None and wo.status in TERMINAL_STATUSES and not admin:
            raise WorkOrderError(
                f"wo {owner_wo} is terminal ({wo.status}); notes are frozen (admin only)"
            )
        async with self.write(actor):
            note.deleted_at = self._now()
            note.deleted_by = actor.value
            note.updated_by = actor.value
            note.source_actor = actor.value
        return owner_wo

    # ---- 載入器用的寫入(idempotent;在呼叫端 write() 交易內)----

    async def upsert_lookup(self, model: type, code: str, label: str) -> None:
        stmt = (
            pg_insert(model)
            .values(code=code, label=label)
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await self.session.execute(stmt)

    async def upsert_status(
        self, code: str, label: str, *, rank: int, is_terminal: bool, is_downtime: bool
    ) -> None:
        values = {
            "code": code,
            "label": label,
            "rank": rank,
            "is_terminal": is_terminal,
            "is_downtime": is_downtime,
        }
        stmt = (
            pg_insert(WoStatus)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["code"],
                set_={k: v for k, v in values.items() if k != "code"},
            )
        )
        await self.session.execute(stmt)

    async def upsert_hold_reason(self, code: str, label: str, *, is_downtime: bool) -> None:
        values = {"code": code, "label": label, "is_downtime": is_downtime}
        stmt = (
            pg_insert(WoHoldReason)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["code"], set_={"label": label, "is_downtime": is_downtime}
            )
        )
        await self.session.execute(stmt)

    async def upsert_work_order(self, data: WorkOrderImport, actor: Actor) -> None:
        """載入器用:依 work_order_no upsert(歷史)。re-run 不重複(idempotent migration)。

        ★ 0031 多負責人:legacy CSV 只有單值 `assignto`。既有 prod 資料由 migration 0031
        回填 `work_order_assignee`;**全新載入**(demo / 重建環境)沒有那次回填 → 交叉表會是空的,
        而「我的工單」過濾與負責人顯示都讀交叉表。故在此同步一次(單值 → 單元素清單),
        使 CSV 載入與 0031 模型一致。idempotent(_replace_assignees 為差異更新)。
        """
        values = {
            "work_order_no": data.work_order_no,
            "asset_id": data.asset_id,
            "work_type": data.work_type,
            "status": data.status,
            "brief_description": data.brief_description,
            "diagnosis": data.diagnosis,
            "external_ref": data.external_ref,
            "opened_date": data.opened_date,
            "scheduled_date": data.scheduled_date,
            "work_start_time": data.work_start_time,
            "work_complete_time": data.work_complete_time,
            "closed_date": data.closed_date,
            "closed_time": data.closed_time,
            "closed_by": data.closed_by,
            "assigned_vendor": data.assigned_vendor,
            "assigned_person": data.assigned_person,
            "opened_at": data.opened_at,
            "closed_at": data.closed_at,
            "downtime_minutes": data.downtime_minutes,
            "downtime_estimated": data.downtime_estimated,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("work_order_no", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(WorkOrder)
            .values(**values)
            .on_conflict_do_update(index_elements=["work_order_no"], set_=update_cols)
        )
        await self.session.execute(stmt)

        # 交叉表同步(見 docstring)。呼叫端 loader 已在 write() 交易內;不自開交易。
        name = clean_person_name(data.assigned_person)
        if name:
            wo = await self.session.get(WorkOrder, data.work_order_no)
            if wo is not None:
                await self._replace_assignees(wo, [name], actor)

    # ---- governed 領域操作(#4b-1;每個自帶 write() 交易;_now 繼承自 DomainService)----

    async def open_work_order(
        self,
        *,
        asset_id: str,
        work_type: str,
        actor: Actor,
        brief_description: str | None = None,
        opened_by: str | None = None,
        assigned_person: str | None = None,
        assignees: list[str] | None = None,
        work_order_no: int | None = None,
        at: datetime | None = None,
    ) -> WorkOrder:
        """開立工單(狀態 OPEN)。`at` 預設系統當下(系統自動抓時間;測試/回填可注入)。

        `assignees`(0031 多負責人)優先於單值 `assigned_person`(相容;若只給後者則等同單元素
        清單)。兩者皆空且 REACTIVE → 衍生自設備負責人(asset_owner)。

        operator 閘:只允許開 REACTIVE 報修;PM 等其他 work_type 一律拒(白名單縱深,route 亦擋)。
        """
        if work_type != "REACTIVE" and await is_operator(self.session, actor):
            raise AuthorizationError("operator can only open REACTIVE work orders")
        async with self.write(actor):
            wo = await self._open_impl(
                asset_id=asset_id,
                work_type=work_type,
                actor=actor,
                brief_description=brief_description,
                opened_by=opened_by,
                assigned_person=assigned_person,
                assignees=assignees,
                work_order_no=work_order_no,
                at=at,
            )
        return wo

    async def _open_impl(
        self,
        *,
        asset_id: str,
        work_type: str,
        actor: Actor,
        brief_description: str | None = None,
        opened_by: str | None = None,
        assigned_person: str | None = None,
        assignees: list[str] | None = None,
        work_order_no: int | None = None,
        at: datetime | None = None,
        origin_station: str | None = None,
        idempotency_key: str | None = None,
        evidence_ref: str | None = None,
    ) -> WorkOrder:
        """開立工單核心(無自有交易;由 open_work_order / confirm / on-box 各自 write() 內呼叫)。

        REACTIVE 開單守門(domain 層 = 所有通道〔web 報修 / on-box / confirm / 未來 MES〕一致):
        - asset 不存在 → 拒(不靜默建殘缺工單;取代原本只靠 FK 崩的隱性擋)。
        - asset 已退役(`is_active=false`)→ 拒:退役機台已不在產線,不該再產生 reactive/downtime
          工單(on-box 對退役資產開單被拒 = 正確語意)。PM 生成不套此擋(排程決定論,另循環)。

        負責人解析(0031 多負責人):`assignees` 非 None → 用之(正規化);否則單值
        `assigned_person` → 單元素清單。皆空且 REACTIVE → 衍生自設備負責人(asset_owner 全部)。
        寫入 work_order_assignee(position 0..n-1)+ 相容欄 `assigned_person`=首位。
        """
        when = at or self._now()
        # 名單優先序:assignees(list)> assigned_person(單值)> (REACTIVE)設備負責人
        if assignees is not None:
            names = _clean_names(assignees)
        else:
            ap = clean_person_name(assigned_person)
            names = [ap] if ap else []
        if work_type == "REACTIVE":
            asset = await self.session.get(Asset, asset_id)
            if asset is None:
                raise WorkOrderError(f"asset {asset_id} not in master")
            if not asset.is_active:
                raise WorkOrderError(f"asset {asset_id} is retired; cannot open a work order")
            # 未明確指派時衍生自設備負責人(全部)。web 報修表單另有人面必填檢查;domain 層不硬拒
            # 空清單(on-box Profile B 自動開單須維持可運作,設備無負責人仍能建單)。
            if not names:
                names = await self._asset_owner_names(asset_id)
        if work_order_no is None:
            maxno = await self.session.scalar(select(func.max(WorkOrder.work_order_no)))
            work_order_no = (maxno or 0) + 1
        wo = WorkOrder(
            work_order_no=work_order_no,
            asset_id=asset_id,
            work_type=work_type,
            status="OPEN",
            brief_description=brief_description,
            opened_date=to_taipei_naive(when).date(),
            opened_at=when,
            opened_by=opened_by,
            assigned_person=names[0] if names else None,  # denormalized 首位(分析平台相容)
            origin_station=origin_station,
            idempotency_key=idempotency_key,
            evidence_ref=evidence_ref,
            downtime_estimated=False,
            source_actor=actor.value,
            created_by=actor.value,
        )
        self.session.add(wo)
        self.session.add(
            WorkOrderStatusHistory(
                work_order_no=work_order_no,
                from_status=None,
                to_status="OPEN",
                changed_at=when,
                source_actor=actor.value,
                created_by=actor.value,
            )
        )
        await self.session.flush()  # work_order PK 落地後才能掛 assignee FK
        for pos, name in enumerate(names):  # 多負責人交叉表(position 0..n-1)
            self.session.add(
                WorkOrderAssignee(
                    work_order_no=work_order_no,
                    person_name=name,
                    position=pos,
                    created_by=actor.value,
                    source_actor=actor.value,
                )
            )
        await self.session.flush()
        # Slice B:開立通知(REACTIVE 報修 + PM 生成皆通知;同交易 enqueue outbox,單一寫入
        # 路徑覆蓋 web 報修 / MCP confirm / on-box / 排程器)。零收件人 → 零列(無噪音)。
        await enqueue_work_order_notifications(self.session, wo, "opened")
        return wo

    async def _transition(
        self,
        work_order_no: int,
        to_status: str,
        actor: Actor,
        *,
        hold_reason: str | None = None,
        at: datetime | None = None,
        note_body: str | None = None,
        note_entry_type: str = "note",
    ) -> WorkOrder:
        """在呼叫端 write() 交易內執行一次狀態轉移(驗證 + 記 history;CLOSED 時精算 downtime)。

        `note_body` 提供時,同交易落一筆 note 並以 `status_history_id` 連結**本次**轉移
        (直接用剛建立的 history 列,不再以 max(id) 慣例回查 —— review f14cf8d:多列同交易
        或回填情境下 max(id) 會把說明掛錯轉移)。
        """
        wo = await self.session.get(WorkOrder, work_order_no)
        if wo is None:
            raise WorkOrderError(f"work_order {work_order_no} not found")
        if to_status not in ALLOWED_TRANSITIONS.get(wo.status, set()):
            raise InvalidTransition(f"wo {work_order_no}: {wo.status} -> {to_status} not allowed")
        if to_status == "ON_HOLD" and not hold_reason:
            raise WorkOrderError(f"wo {work_order_no}: ON_HOLD requires hold_reason")
        when = at or self._now()
        prev = wo.status
        wo.status = to_status
        wo.updated_by = actor.value
        wo.source_actor = actor.value
        if to_status == "ON_HOLD":
            wo.hold_reason = hold_reason
        elif to_status == "IN_PROGRESS":
            wo.hold_reason = None  # resume 清除
        if to_status == "CLOSED":
            wo.closed_at = when
        hist = WorkOrderStatusHistory(
            work_order_no=work_order_no,
            from_status=prev,
            to_status=to_status,
            hold_reason=hold_reason if to_status == "ON_HOLD" else None,
            changed_at=when,
            source_actor=actor.value,
            created_by=actor.value,
        )
        self.session.add(hist)
        await self.session.flush()
        if note_body and note_body.strip():
            self.session.add(
                WorkOrderNote(
                    work_order_no=work_order_no,
                    entry_type=note_entry_type,
                    body=note_body.strip(),
                    author=actor.value,
                    occurred_at=when,
                    status_history_id=hist.id,
                    created_by=actor.value,
                    source_actor=actor.value,
                )
            )
            await self.session.flush()
        if to_status == "CLOSED":
            await self._recompute_downtime(wo)
            # Slice B:結案通知(兩類工單皆通知;同交易 enqueue outbox,冪等唯一鍵 →
            # reopen→re-close 不重發)。零收件人 → 零列。
            await enqueue_work_order_notifications(self.session, wo, "closed")
        return wo

    async def start_work(
        self, work_order_no: int, actor: Actor, *, at: datetime | None = None
    ) -> WorkOrder:
        await self._assert_not_operator(actor, "start_work")
        async with self.write(actor):
            wo = await self._transition(work_order_no, "IN_PROGRESS", actor, at=at)
        return wo

    async def hold_work(
        self,
        work_order_no: int,
        hold_reason: str,
        actor: Actor,
        *,
        at: datetime | None = None,
        note_body: str | None = None,
    ) -> WorkOrder:
        """轉等待(→ON_HOLD)。`note_body` 可選 = 延誤說明(零件 ETA / 廠商時間 / 等機台空檔…),
        同交易落一筆 `hold` note 並連結該次 status_history —— 供 Analytics 盯 downtime 時
        給出合理解釋(Jordan 2026-07-03)。downtime 計不計仍由 hold_reason.is_downtime 決定。"""
        await self._assert_not_operator(actor, "hold_work")
        async with self.write(actor):
            wo = await self._transition(
                work_order_no,
                "ON_HOLD",
                actor,
                hold_reason=hold_reason,
                at=at or self._now(),
                note_body=note_body,
                note_entry_type="hold",
            )
        return wo

    async def resume_work(
        self,
        work_order_no: int,
        actor: Actor,
        *,
        at: datetime | None = None,
        note_body: str | None = None,
    ) -> WorkOrder:
        await self._assert_not_operator(actor, "resume_work")
        async with self.write(actor):
            wo = await self._transition(
                work_order_no,
                "IN_PROGRESS",
                actor,
                at=at or self._now(),
                note_body=note_body,
                note_entry_type="resume",
            )
        return wo

    async def set_hold(
        self,
        work_order_no: int,
        hold_reason: str,
        actor: Actor,
        *,
        at: datetime | None = None,
        note_body: str | None = None,
    ) -> WorkOrder:
        """一鍵轉等待(Jordan 2026-07-05 #3:等待 chip 從任何活單態直接切換)。

        `hold_work` 只接受 IN_PROGRESS→ON_HOLD;本便利方法補齊 UI 需要的三種起點,
        全在**同一交易**內以 canonical 轉移完成(status_history 忠實記錄每一步、契約不變):
        - OPEN → 先 start(OPEN→IN_PROGRESS)→ hold
        - ON_HOLD → 換原因:resume(ON_HOLD→IN_PROGRESS)→ hold(兩轉移原子完成)
        - IN_PROGRESS → 直接 hold
        中間補的 IN_PROGRESS 與 ON_HOLD 用同一 `when`,零長度區段不影響 downtime 精算。
        `note_body` 落在最終 hold 轉移的 `hold` note(延誤說明,供 Analytics 稽核)。
        """
        await self._assert_not_operator(actor, "set_hold")
        async with self.write(actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            when = at or self._now()
            if wo.status in ("OPEN", "ON_HOLD"):
                # OPEN→IN_PROGRESS(開始) 或 ON_HOLD→IN_PROGRESS(換原因前先復工)
                await self._transition(work_order_no, "IN_PROGRESS", actor, at=when)
            wo = await self._transition(
                work_order_no,
                "ON_HOLD",
                actor,
                hold_reason=hold_reason,
                at=when,
                note_body=note_body,
                note_entry_type="hold",
            )
        return wo

    async def resume_or_start(
        self, work_order_no: int, actor: Actor, *, at: datetime | None = None
    ) -> WorkOrder:
        """一鍵「處理中」(Jordan 2026-07-05 #3:取代 開始/復工 兩鍵)。

        依當前狀態分派:OPEN→start(OPEN→IN_PROGRESS)、ON_HOLD/COMPLETED→resume
        (→IN_PROGRESS)。已 IN_PROGRESS → no-op(回原工單)。終態 → InvalidTransition。
        """
        await self._assert_not_operator(actor, "resume_or_start")
        async with self.write(actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            if wo.status == "IN_PROGRESS":
                return wo  # 已在處理中,冪等 no-op
            wo = await self._transition(work_order_no, "IN_PROGRESS", actor, at=at)
        return wo

    async def complete_work(
        self,
        work_order_no: int,
        actor: Actor,
        *,
        at: datetime | None = None,
        action_taken: str | None = None,
        labor_hours: Decimal | str | None = None,
    ) -> WorkOrder:
        """完工(→COMPLETED)。可順帶記處置摘要 `action_taken` 與工時(ui-mvp-spec §3 完成欄)。"""
        await self._assert_not_operator(actor, "complete_work")
        hours = self._parse_labor_hours(labor_hours)  # 先驗格式,避免壞輸入回滾整筆轉移
        async with self.write(actor):
            wo = await self._transition(work_order_no, "COMPLETED", actor, at=at)
            self._record_completion_fields(wo, action_taken, hours)
        return wo

    async def close_work_order(
        self,
        work_order_no: int,
        actor: Actor,
        *,
        at: datetime | None = None,
        action_taken: str | None = None,
        labor_hours: Decimal | str | None = None,
        confirmed_reason_code: str | None = None,
    ) -> WorkOrder:
        """結案(COMPLETED→CLOSED);依 status_history 精算並鎖定 downtime。

        若為 PM 工單(`work_type=PM` 且有 `pm_source_id`),於同一交易內回寫其 pm_schedule
        (推進 next_due_date / 記 last_pm_date,ADR-021 完成回寫),不另開交易。
        `action_taken` / `labor_hours` 可在結案時補記(未提供則不動)。`confirmed_reason_code`
        (D6,efc 軸)選填、僅 REACTIVE 有意義;交易外先驗(壞碼不回滾整筆結案)。
        """
        await self._assert_not_operator(actor, "close_work_order")
        hours = self._parse_labor_hours(labor_hours)  # 先驗格式,避免壞輸入回滾整筆結案
        reason = await self._validate_confirmed_reason(confirmed_reason_code)  # 交易外先驗 efc
        async with self.write(actor):
            wo = await self._transition(work_order_no, "CLOSED", actor, at=at)
            self._assert_reason_applicable(wo, reason)  # 真因僅 REACTIVE
            self._record_completion_fields(wo, action_taken, hours, reason)
            if wo.work_type == "PM" and wo.pm_source_id:
                # closed_on = closed_at 的廠區當地日(沿用 downtime 既有時區處理)
                closed_on = to_taipei_naive(wo.closed_at).date()
                await self._advance_pm_schedule(wo.pm_source_id, actor, closed_on)
        return wo

    async def finish_work_order(
        self,
        work_order_no: int,
        actor: Actor,
        *,
        action_taken: str | None = None,
        labor_hours: Decimal | str | None = None,
        confirmed_reason_code: str | None = None,
        at: datetime | None = None,
    ) -> WorkOrder:
        """一鍵「結單」(Jordan 2026-07-05 #3:取代 完工+結案 兩鍵)。

        同一交易内走 canonical COMPLETED→CLOSED 兩段轉移(status_history 兩筆忠實保留、
        契約不變),重用既有 complete/close 的全部守門:先驗工時格式(壞輸入不回滾整筆,
        f14cf8d)、PM 結案回寫 next_due_date。起點(Jordan #3:downtime 從開單就算、「開始」
        鍵已移除,故 OPEN 亦可直接結單):
        - OPEN → IN_PROGRESS → COMPLETED → CLOSED
        - IN_PROGRESS / ON_HOLD → COMPLETED → CLOSED
        - COMPLETED → 直接 CLOSED(略過已達的 COMPLETED 段)
        每段用同一 `when`,零長度中間態不影響 downtime 精算。`action_taken` **選填**(Jordan
        2026-07-07:結單不強制總結,工作日誌已交代;姊妹專案皆無綁定需求)—— 有填則記錄、
        空/None 不動;`labor_hours` 選填(實際人力工時,非停機時長)。`confirmed_reason_code`
        (D6,efc 軸)選填、僅 REACTIVE 有意義;交易外先驗(壞碼不回滾整筆結單)。既有分開的
        `complete_work` / `close_work_order` 保留(MCP/API 面不動)。
        """
        await self._assert_not_operator(actor, "finish_work_order")
        hours = self._parse_labor_hours(labor_hours)  # 交易外先驗格式
        reason = await self._validate_confirmed_reason(confirmed_reason_code)  # 交易外先驗 efc
        async with self.write(actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            self._assert_reason_applicable(wo, reason)  # 真因僅 REACTIVE(先擋,不做無謂轉移)
            when = at or self._now()
            if wo.status == "OPEN":  # 未點過「處理中」也可結單:先隱式開始
                await self._transition(work_order_no, "IN_PROGRESS", actor, at=when)
            if wo.status != "COMPLETED":
                await self._transition(work_order_no, "COMPLETED", actor, at=when)
            wo = await self._transition(work_order_no, "CLOSED", actor, at=when)
            self._record_completion_fields(wo, action_taken, hours, reason)
            if wo.work_type == "PM" and wo.pm_source_id:
                closed_on = to_taipei_naive(wo.closed_at).date()
                await self._advance_pm_schedule(wo.pm_source_id, actor, closed_on)
        return wo

    @staticmethod
    def _parse_labor_hours(value: Decimal | str | None) -> Decimal | None:
        """工時輸入驗證(交易外先驗:review f14cf8d,「2,5」這種壞輸入若在轉移後才爆,
        會把整筆完工/結案默默回滾)。空/None → None;不可解析 / 負數 → WorkOrderError。"""
        if value is None or not str(value).strip():
            return None
        try:
            hours = Decimal(str(value).strip())
        except ArithmeticError as e:
            raise WorkOrderError(f"invalid labor_hours: {value!r}") from e
        if hours < 0:
            raise WorkOrderError(f"labor_hours cannot be negative: {value!r}")
        return hours

    @staticmethod
    def _record_completion_fields(
        wo: WorkOrder,
        action_taken: str | None,
        labor_hours: Decimal | None,
        confirmed_reason_code: str | None = None,
    ) -> None:
        """完工欄位補記(在呼叫端交易內):只在有提供時覆寫,空字串/None 視為未提供。

        `confirmed_reason_code` 已由 `_validate_confirmed_reason` 交易外先驗(存在 + is_active);
        None = 未提供 → 不動(比照 action_taken 覆寫語意;清除走 `set_confirmed_reason`)。
        """
        if action_taken and action_taken.strip():
            wo.action_taken = action_taken.strip()
        if labor_hours is not None:
            wo.labor_hours = labor_hours
        if confirmed_reason_code is not None:
            wo.confirmed_reason_code = confirmed_reason_code

    async def _validate_confirmed_reason(self, code: str | None) -> str | None:
        """驗證 efc 確認真因碼(交易外先驗:比照 _parse_labor_hours,壞輸入不回滾整筆結案)。

        None / 空白 → None(=未確認,合法)。否則 strip 後須存在於 `equipment_failure_code`
        且 `is_active=True`,否則 WorkOrderError。**只讀 efc 單軸,永不觸 mfc。**退役碼(is_active
        =False)不得再被選為新真因(歷史工單既有引用不受影響)。
        """
        if code is None or not code.strip():
            return None
        code = code.strip()
        row = await self.session.scalar(
            select(EquipmentFailureCode).where(EquipmentFailureCode.code == code)
        )
        if row is None:
            raise WorkOrderError(f"unknown equipment failure code: {code!r}")
        if not row.is_active:
            raise WorkOrderError(f"equipment failure code is retired: {code!r}")
        return code

    @staticmethod
    def _assert_reason_applicable(wo: WorkOrder, code: str | None) -> None:
        """真因僅 REACTIVE 工單有意義:非 None 碼配非 REACTIVE 工單 → 拒(PM 發現故障應另開報修)。"""
        if code is not None and wo.work_type != "REACTIVE":
            raise WorkOrderError(
                f"wo {wo.work_order_no}: confirmed reason only applies to REACTIVE work orders"
            )

    async def set_assignees(
        self, work_order_no: int, *, assignees: list[str], actor: Actor
    ) -> WorkOrder:
        """指派/改派工單負責人清單(governed;開立後隨時可改,終態除外;0031 多負責人)。

        逐名正規化、去重保序;**空清單 → 清除**所有負責人。整組替換 work_order_assignee
        交叉表 + 同步相容欄 `assigned_person`=首位(legacy 確切字串,「我的工單」過濾鍵)。
        """
        await self._assert_not_operator(actor, "set_assignees")
        async with self.write(actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            if wo.status in TERMINAL_STATUSES:
                raise WorkOrderError(f"wo {work_order_no}: cannot reassign terminal ({wo.status})")
            await self._replace_assignees(wo, _clean_names(assignees), actor)
            wo.updated_by = actor.value
            wo.source_actor = actor.value
        return wo

    async def set_assignee(
        self, work_order_no: int, *, assigned_person: str | None, actor: Actor
    ) -> WorkOrder:
        """單值指派相容包裝(委派 set_assignees;空值 → 清除)。既有 API/CLI 呼叫面不變。"""
        ap = clean_person_name(assigned_person)
        return await self.set_assignees(
            work_order_no, assignees=[ap] if ap else [], actor=actor
        )

    async def set_confirmed_reason(
        self, work_order_no: int, *, code: str | None, actor: Actor
    ) -> WorkOrder:
        """事後補填 / 更正 / 清除工單「確認故障真因」(D6;efc 軸,人工確認)。`code=None`/空 → 清除。

        規則:① 僅 REACTIVE 工單(即使清除亦拒非 REACTIVE —— 語意單純:真因僅 REACTIVE)
        ② 非終態 = 任何登入者(web 已拒匿名)可設;CLOSED/CANCELLED/VOIDED 後**限 admin**
        更正(比照日誌 / brief 終態凍結,downtime 佐證是已審結紀錄)③ 碼須存在且 is_active
        (交易外先驗)。內容未變 → no-op(不污染稽核,比照 update_asset)。governed(單一寫入路徑)。
        """
        await self._assert_not_operator(actor, "set_confirmed_reason")
        reason = await self._validate_confirmed_reason(code)  # 交易外先驗(存在 + is_active)
        wo = await self.session.get(WorkOrder, work_order_no)
        if wo is None:
            raise WorkOrderError(f"work order {work_order_no} not found")
        if wo.work_type != "REACTIVE":
            raise WorkOrderError(
                f"wo {work_order_no}: confirmed reason only applies to REACTIVE work orders"
            )
        if wo.status in TERMINAL_STATUSES and not await is_active_admin(self.session, actor):
            raise WorkOrderError(
                f"wo {work_order_no} is terminal ({wo.status}); "
                "confirmed reason is frozen (admin only)"
            )
        if wo.confirmed_reason_code == reason:
            return wo  # 冪等 no-op(含清除已為空)
        async with self.write(actor):
            wo.confirmed_reason_code = reason
            wo.updated_by = actor.value
            wo.source_actor = actor.value
        return wo

    async def list_confirmed_reason_options(
        self, asset_id: str
    ) -> list[ConfirmedReasonOption]:
        """結單「確認故障原因」下拉候選(efc 軸,唯讀,ADR-004):本設備既往工單用過的碼優先,
        其餘 active efc 碼字母序在後。退役碼不建議(僅保留仍 active 者);附人讀說明 descr
        供多關鍵字搜尋。回 ConfirmedReasonOption(code, descr, used)。"""
        used = set(
            (
                await self.session.scalars(
                    select(WorkOrder.confirmed_reason_code)
                    .where(
                        WorkOrder.asset_id == asset_id,
                        WorkOrder.confirmed_reason_code.is_not(None),
                    )
                    .distinct()
                )
            ).all()
        )
        active = (
            await self.session.execute(
                select(EquipmentFailureCode.code, EquipmentFailureCode.descr)
                .where(EquipmentFailureCode.is_active.is_(True))
                .order_by(EquipmentFailureCode.code)
            )
        ).all()
        head = [  # 本設備用過 + 仍 active(字母序)
            ConfirmedReasonOption(c, d, True) for c, d in active if c in used
        ]
        tail = [  # 其餘 active(字母序)
            ConfirmedReasonOption(c, d, False) for c, d in active if c not in used
        ]
        return head + tail

    async def list_assignee_suggestions(self, q: str, *, limit: int = 8) -> list[str]:
        """自動完成:歷史指派名 distinct(ilike;排序/截斷推進 SQL)。讀取,開放。0031:來源 =
        工單負責人交叉表 `work_order_assignee`(全部負責人)∪ 工單 denormalized 首位
        `assigned_person`(涵蓋歷史/載入單未回填交叉表者)∪ 保養排程 `assigned_person`
        ∪ 設備負責人 `asset_owner`。union 自動去重。"""
        like = f"%{q.strip()}%"
        u = union(
            select(WorkOrderAssignee.person_name).where(
                WorkOrderAssignee.person_name.ilike(like)
            ),
            select(WorkOrder.assigned_person).where(
                WorkOrder.assigned_person.is_not(None), WorkOrder.assigned_person.ilike(like)
            ),
            select(PmSchedule.assigned_person).where(
                PmSchedule.assigned_person.is_not(None), PmSchedule.assigned_person.ilike(like)
            ),
            select(AssetOwner.person_name).where(AssetOwner.person_name.ilike(like)),
        ).subquery()
        stmt = select(u.c[0]).order_by(u.c[0]).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def void_work_order(
        self,
        work_order_no: int,
        actor: Actor,
        *,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> WorkOrder:
        """作廢(高風險終態)。**admin 限定在 domain 強制**(review f14cf8d:route 藏按鈕
        不是授權;任何未來 MCP/API/CLI 面呼叫到這裡都要過同一道門)。`reason` 提供時
        同交易落一筆 note 連結該次轉移(必填與否由呼叫端 UI 政策決定)。"""
        await assert_active_admin(self.session, actor)
        async with self.write(actor):
            wo = await self._transition(
                work_order_no, "VOIDED", actor, at=at, note_body=reason
            )
        return wo

    async def cancel_reactive_report(
        self,
        work_order_no: int,
        actor: Actor,
        *,
        at: datetime | None = None,
        reason: str | None = None,
    ) -> WorkOrder:
        """報修軟取消(OPEN→CANCELLED;保留稽核軌跡,ADR-017 Q4)。`reason` 同交易落 note
        (review f14cf8d:取消理由與轉移原子寫入,不再事後另開交易補記)。

        operator 閘:只允許取消**自己開的**單(`created_by == actor.value`);OPEN 限制由狀態機
        既有守門(OPEN→CANCELLED)保證。內部經 `_transition` 直接寫事由 note(非公開 add_note),
        故不受 operator add_note 閘影響。"""
        if await is_operator(self.session, actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            if wo.created_by != actor.value:
                raise AuthorizationError(
                    "operator can only cancel work orders they opened"
                )
        async with self.write(actor):
            wo = await self._transition(
                work_order_no, "CANCELLED", actor, at=at, note_body=reason
            )
        return wo

    # ---- PM 生成(ADR-021;按需 + 自動排程器,共用核心、皆走單一寫入路徑)----

    async def generate_pm_work_order(
        self,
        *,
        pm_id: str,
        actor: Actor,
        at: datetime | None = None,
        assigned_person: str | None = None,
    ) -> WorkOrder:
        """按需生成 PM 工單(ADR-021):工程師對某到期 PM 按「執行」→ 開一張 PM `WorkOrder`。

        - 單一 write() 交易;核心在 `_generate_pm_impl`(與自動排程器共用,避免巢狀交易)。
        - **冪等**:該 PM 本週期已有未結案的 PM 工單(`last_work_order_no` 指向且非終態)→ 回該單、
          不重複生成。結案後再生成 = 新一張(下個週期)。
        - **允許 suppress 的 PM**:按需是真人明確覆寫;`is_suppressed` 僅治理自動排程器 +
          到期清單(ADR-021 兩執行模式),不擋按需。
        - 發起者為真人(`Actor.human`);完成回寫(推進 next_due_date)於 close 時做。
        - 不走 gated-write(PM 生成是 schedule 決定論、非裁量,ADR-021)。
        - `assigned_person`:選填 override(補開工單確認頁可改負責人);None → 沿用 pm 的
          `assigned_person`(#5a:所有 PM 生成工單皆帶 owner)。override 只影響本次新開,
          冪等命中既有工單時不改既有 owner。

        operator 閘:按需 PM 生成非 operator 職責(自動排程器走 Actor.scheduler、非 operator,
        不受影響)。
        """
        await self._assert_not_operator(actor, "generate_pm_work_order")
        async with self.write(actor):
            pm = await self.session.get(PmSchedule, pm_id)
            if pm is None:
                raise WorkOrderError(f"pm_schedule {pm_id} not found")
            wo, _created = await self._generate_pm_impl(
                pm, actor, at, assigned_person=assigned_person
            )
        return wo

    async def _generate_pm_impl(
        self,
        pm: PmSchedule,
        actor: Actor,
        at: datetime | None,
        *,
        assigned_person: str | None = None,
    ) -> tuple[WorkOrder, bool]:
        """PM 工單生成核心(無自有交易;由按需 / 排程器各自 write() 內呼叫)。

        回 `(work_order, created)`:`created=False` = 冪等命中本週期既有未結案工單(未新增);
        `created=True` = 新開一張 PM `WorkOrder`(status=OPEN,回寫 pm.last_work_order_no 軟連結)。

        `assigned_person` override(None → 沿用 pm.assigned_person);冪等命中時不改既有工單 owner。
        """
        # 冪等:本週期已有未結案 PM 工單 → 回既有,不重複生成(不改既有 owner)
        if pm.last_work_order_no is not None:
            existing = await self.session.get(WorkOrder, pm.last_work_order_no)
            if (
                existing is not None
                and existing.work_type == "PM"
                and existing.status not in TERMINAL_STATUSES
            ):
                return existing, False
        # 退役資產擋 PM 生成(#1;Jordan 2026-07-05 裁決):退役機台已不在產線,不再產生任何
        # 工單(比照 _open_impl 退役擋報修)。同時覆蓋按需 + 排程器;排程器每 PM 獨立交易,
        # 此 raise 會被其 per-PM 例外隔離接住(計入 error,不炸整批)。
        asset = await self.session.get(Asset, pm.asset_id)
        if asset is None or not asset.is_active:
            raise WorkOrderError(
                f"asset {pm.asset_id} is retired; cannot generate PM work order"
            )
        task = await self.session.get(Task, pm.task_id)
        brief = task.description if task is not None else None
        # assignee 優先序(0031):明確 override(單值)→ per-PM 覆寫(單值)→ 設備負責人(全部)。
        # 回填後多數 pm.assigned_person 為 NULL,真相落在設備負責人清單(asset_owner)。
        override = clean_person_name(assigned_person)
        pm_override = clean_person_name(pm.assigned_person)
        if override:
            names = [override]
        elif pm_override:
            names = [pm_override]
        else:
            names = await self._asset_owner_names(pm.asset_id)
        wo = await self._open_impl(
            asset_id=pm.asset_id,
            work_type="PM",
            actor=actor,
            brief_description=brief,
            assignees=names,
            at=at,
        )
        wo.pm_source_id = pm.pm_id
        pm.last_work_order_no = wo.work_order_no  # soft-ref 回指本週期工單
        pm.updated_by = actor.value
        pm.source_actor = actor.value
        await self.session.flush()
        return wo, True

    async def generate_due_pm_work_orders(
        self,
        *,
        actor: Actor,
        as_of: date | None = None,
        limit: int | None = None,
        at: datetime | None = None,
    ) -> list[PmGenerationResult]:
        """自動排程器(ADR-021,unattended):為到期且週期性的 PM 批次生成工單。

        選取條件(全部成立):`next_due_date` 非空且 <= `as_of`(預設廠區當地今天)、
        `is_suppressed=false`、`frequency_interval>0` 且 `frequency_unit` 非空 —— **僅週期性**
        (非週期/單次 PM 不進排程器,留按需,否則結案不推進 next_due_date 會被每跑必中重生成)。

        - **每 PM 獨立交易**:逐筆於各自 write() 內生成;單筆失敗(如 FK 異常)隔離成 `error`、
          不影響其餘到期 PM(unattended 韌性)。先收集 pm_id 再逐筆重抓,避免跨 commit 的 ORM 陳舊。
        - **冪等**(ADR-006):本週期已有未結案 PM 工單 → `created=False`,不重複生成;重跑安全。
        - 生成 `status=OPEN`(與按需一致)。**不做 lead-window 預先 staging**:提前開單需非
          downtime 的 PLANNED 態,但 OPEN.is_downtime=True 且 7 態機無 PLANNED;ADR-021 §3 已將
          lead-window + Fixed/Floating 綁定 `calendar_freq_type` [UI] 抽取後再做(守護欄 #8)。
        - **週末提前生成**(03-scheduled-activity §3.2):`next_due_date` 落週六/日 → 以其前的
          週五為「有效生成日」(`effective_generation_date`)。SQL 先寬放到 `as_of + 2 天`(最大
          提前量),再以純函式精確篩選(週五不提前、週六 -1、週日 -2)。**只影響生成時機**,
          next_due_date 本身與 Fixed 推進鏈不動。國定假日 v1 不處理(需假日表)。
        - `actor` 由呼叫端決定;排程器入口傳 `Actor.scheduler()`(誠實標示時鐘驅動,ADR-021 §4)。
        """
        if as_of is None:
            as_of = to_taipei_naive(self._now()).date()
        # 週末提前:寬放到 as_of+2 天撈候選,再以 effective_generation_date 精篩(見 docstring)
        stmt = (
            select(PmSchedule.pm_id, PmSchedule.next_due_date)
            .where(
                PmSchedule.next_due_date.is_not(None),
                PmSchedule.next_due_date <= as_of + timedelta(days=2),
                PmSchedule.is_suppressed.is_(False),
                PmSchedule.frequency_interval > 0,
                PmSchedule.frequency_unit.is_not(None),
            )
            .order_by(PmSchedule.next_due_date, PmSchedule.pm_id)
        )
        due_ids = [
            pid
            for pid, due in (await self.session.execute(stmt)).all()
            if effective_generation_date(due) <= as_of
        ]
        if limit is not None:
            due_ids = due_ids[:limit]

        results: list[PmGenerationResult] = []
        for pm_id in due_ids:
            try:
                async with self.write(actor):
                    pm = await self.session.get(PmSchedule, pm_id)
                    if pm is None:  # 查詢與生成之間消失(理論上不會;保守處理)
                        raise WorkOrderError(f"pm_schedule {pm_id} disappeared")
                    wo, created = await self._generate_pm_impl(pm, actor, at)
                results.append(
                    PmGenerationResult(pm_id=pm_id, work_order_no=wo.work_order_no, created=created)
                )
            except Exception as e:  # 單筆隔離:記錄成 error 並續跑(unattended 韌性)
                results.append(
                    PmGenerationResult(pm_id=pm_id, work_order_no=None, created=False, error=str(e))
                )
        return results

    async def _advance_pm_schedule(self, pm_id: str, actor: Actor, closed_on: date) -> None:
        """PM 完成回寫(ADR-021):結案後推進其 pm_schedule(在呼叫端 write() 交易內,不另開交易)。

        - 不週期(`frequency_interval=0` 或 `frequency_unit` 為 None):僅記 `last_pm_date`,
          `next_due_date` 維持不動(不進入循環)。
        - 週期:`base = next_due_date`(無則 closed_on)→ +interval;並記 `last_pm_date`。
        ★ 目前一律 **Fixed**(從排定的 next_due_date 起算)。**Floating**(從實際完成日起算以防
          堆積)待 `calendar_freq_type` [UI] 抽取後再分流(S1/S4)。
        """
        pm = await self.session.get(PmSchedule, pm_id)
        if pm is None:
            return
        pm.last_pm_date = closed_on
        pm.updated_by = actor.value
        pm.source_actor = actor.value
        if pm.frequency_interval == 0 or pm.frequency_unit is None:
            return  # 不週期:不推進 next_due_date
        base = pm.next_due_date or closed_on
        pm.next_due_date = add_interval(base, pm.frequency_interval, pm.frequency_unit)
        await self.session.flush()

    async def issue_part_to_work_order(
        self,
        *,
        work_order_no: int,
        item_code: str,
        quantity: Decimal | str | int,
        actor: Actor,
        idempotency_key: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        """工單領料:記 work_order_part + 經 stock_transaction(ISSUE)扣 on_hand。

        回傳 True=已領;False=`idempotency_key` 命中 → 跳過(不重複扣帳/不重複記用料)。
        """
        await self._assert_not_operator(actor, "issue_part_to_work_order")
        try:
            qty = Decimal(str(quantity))
        except ArithmeticError as e:
            raise WorkOrderError(f"invalid quantity: {quantity!r}") from e
        if qty <= 0:
            # review f14cf8d:負數會經 qty_delta=-qty 反向「加」庫存,0 只留垃圾帳
            raise WorkOrderError(f"quantity must be positive: {quantity!r}")
        async with self.write(actor):
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            if wo.status in TERMINAL_STATUSES:
                raise WorkOrderError(
                    f"wo {work_order_no}: cannot issue parts to terminal ({wo.status})"
                )
            if await self.session.get(InventoryItem, item_code) is None:
                raise WorkOrderError(f"inventory_item {item_code} not found")
            when = at or self._now()
            posted = await InventoryService(self.session).post_stock_transaction(
                item_code=item_code,
                qty_delta=-qty,
                kind="ISSUE",
                actor=actor,
                work_order_no=work_order_no,
                occurred_at=when,
                idempotency_key=idempotency_key,
            )
            if not posted:
                return False  # idempotent 命中:不重複記 work_order_part
            self.session.add(
                WorkOrderPart(
                    work_order_no=work_order_no,
                    item_code=item_code,
                    quantity=qty,
                    source_actor=actor.value,
                    created_by=actor.value,
                )
            )
        return True

    async def update_part_issue_quantity(
        self,
        *,
        work_order_no: int,
        part_id: int,
        new_quantity: Decimal | str | int,
        actor: Actor,
        idempotency_key: str | None = None,
    ) -> bool:
        """改工單領料數量(Jordan 2026-07-05 #9):差額連動庫存(單一寫入路徑、全稽核)。

        增量 → 補一筆 ISSUE 扣庫存(庫存不足拒絕);減量 → RETURN 差額回庫存。work_order_part
        是摘要列(非 ledger),就地改 `quantity`;stock_transaction ledger 只增補償帳、不改舊帳。
        以 `part_id` 定位(work_order_no 歸屬守門)—— (wo,item) 非唯一(同料可多次領料,見
        WorkOrderPart),故用列 id 精確定位、避免歧義。回 True=已調;False=數量未變/冪等命中。

        守門(f14cf8d):`new_quantity` 必 >0(要移除走 `cancel_part_issue`);終態工單拒改;
        已軟刪列拒改。★ 只作用於 governed 領料(原 ISSUE 有扣 on_hand)—— 歷史回填領料以
        `source_actor == BACKFILL_ACTOR` **顯式擋下**(不再只靠終態工單推論;legacy 有 OPEN 單掛
        回填 part,終態守門不保證擋到),避免把「有帳無 on_hand」的回填再 RETURN 灌爆 on_hand。
        """
        await self._assert_not_operator(actor, "update_part_issue_quantity")
        try:
            new_qty = Decimal(str(new_quantity))
        except ArithmeticError as e:
            raise WorkOrderError(f"invalid quantity: {new_quantity!r}") from e
        if new_qty <= 0:
            raise WorkOrderError(
                f"quantity must be positive: {new_quantity!r} (use cancel to remove)"
            )
        async with self.write(actor):
            part = await self.session.get(WorkOrderPart, part_id)
            if part is None or part.deleted_at is not None or part.work_order_no != work_order_no:
                raise WorkOrderError(
                    f"work_order_part {part_id} not found on wo {work_order_no}"
                )
            if part.source_actor == BACKFILL_ACTOR.value:
                raise WorkOrderError(
                    "cannot amend a historical backfill issue "
                    "(never adjusted on_hand; amending would inflate stock)"
                )
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is not None and wo.status in TERMINAL_STATUSES:
                raise WorkOrderError(
                    f"wo {work_order_no}: cannot amend parts on terminal ({wo.status})"
                )
            delta = new_qty - part.quantity  # >0 需再扣;<0 回庫
            if delta == 0:
                return False
            inv = InventoryService(self.session)
            when = self._now()
            if delta > 0:
                item = await self.session.get(InventoryItem, part.item_code)
                on_hand = (item.quantity_on_hand or Decimal(0)) if item is not None else Decimal(0)
                if on_hand < delta:
                    raise WorkOrderError(
                        f"insufficient stock for {part.item_code}: have {on_hand}, need {delta}"
                    )
                posted = await inv.post_stock_transaction(
                    item_code=part.item_code, qty_delta=-delta, kind="ISSUE", actor=actor,
                    work_order_no=work_order_no, reason="amend issue qty (increase)",
                    occurred_at=when, idempotency_key=idempotency_key,
                )
            else:
                posted = await inv.post_stock_transaction(
                    item_code=part.item_code, qty_delta=-delta, kind="RETURN", actor=actor,
                    work_order_no=work_order_no, reason="amend issue qty (decrease)",
                    occurred_at=when, idempotency_key=idempotency_key,
                )
            if not posted:
                return False  # 冪等命中:摘要列不動(前次交易已改)
            part.quantity = new_qty
            part.updated_by = actor.value
            part.source_actor = actor.value
        return True

    async def cancel_part_issue(
        self,
        *,
        work_order_no: int,
        part_id: int,
        actor: Actor,
        idempotency_key: str | None = None,
    ) -> bool:
        """取消工單領料(Jordan 2026-07-05 #9):RETURN 全數回庫存 + work_order_part 軟刪(0022)。

        ledger(stock_transaction)忠實留痕(原 ISSUE + 補償 RETURN 皆在);摘要列軟刪讓它從
        領料清單消失(誰/何時取消可查)。以 `part_id` 定位 + work_order_no 歸屬守門;終態工單拒改;
        冪等:已軟刪 → no-op 回 False。★ 只作用於 governed 領料 —— 歷史回填領料以
        `source_actor == BACKFILL_ACTOR` **顯式擋下**(見 `update_part_issue_quantity`),避免對
        「有帳無 on_hand」的回填誤 RETURN 灌爆 on_hand。
        """
        await self._assert_not_operator(actor, "cancel_part_issue")
        async with self.write(actor):
            part = await self.session.get(WorkOrderPart, part_id)
            if part is None or part.work_order_no != work_order_no:
                raise WorkOrderError(
                    f"work_order_part {part_id} not found on wo {work_order_no}"
                )
            if part.deleted_at is not None:
                return False  # 冪等:已取消
            if part.source_actor == BACKFILL_ACTOR.value:
                raise WorkOrderError(
                    "cannot cancel a historical backfill issue "
                    "(never adjusted on_hand; RETURN would inflate stock)"
                )
            wo = await self.session.get(WorkOrder, work_order_no)
            if wo is not None and wo.status in TERMINAL_STATUSES:
                raise WorkOrderError(
                    f"wo {work_order_no}: cannot cancel parts on terminal ({wo.status})"
                )
            when = self._now()
            await InventoryService(self.session).post_stock_transaction(
                item_code=part.item_code, qty_delta=part.quantity, kind="RETURN", actor=actor,
                work_order_no=work_order_no, reason="cancel issue",
                occurred_at=when, idempotency_key=idempotency_key,
            )
            part.deleted_at = when
            part.deleted_by = actor.value
            part.updated_by = actor.value
            part.source_actor = actor.value
        return True

    async def backfill_part_issue(
        self,
        *,
        work_order_no: int,
        item_code: str,
        quantity: Decimal | str | int,
        occurred_at: datetime,
        actor: Actor,
        idempotency_key: str,
        reason: str | None = None,
        asset_id: str | None = None,
    ) -> PartIssueOutcome:
        """歷史領料回填:記 stock_transaction(ISSUE),**不動 on_hand**。

        與 `issue_part_to_work_order` 的關鍵差異(故另立方法,不重用):
        - on_hand:**不連動扣減**(adjust_on_hand=False)。eMaint onhand snapshot 已反映這些
          歷史扣減,再扣會雙重扣帳(規則 ②)。代價:stock_transaction 累加和 ≠ on_hand(snapshot
          起算 + 僅部分回填),屬 snapshot-初始化系統回填歷史的可接受結果(見 02-work-orders §5)。
        - 終態 WO:**允許**(歷史領料多掛在 CLOSED 單上;`issue_part_to_work_order` 會 raise)。
        - FK 缺失:**回 outcome enum、不 raise**(未登記 item 屬正常,由 loader log+skip)。

        歸屬救援(ADR-024):WO 存在 → 掛工單(INSERTED,含 work_order_part);WO 不存在但 `asset_id`
        (= 領料列 compid)為有效設備 → **掛設備救援**(INSERTED_ASSET,`charge_target_asset_id`,
        **無** work_order_part);WO 不存在且無有效設備 → MISSING_WORK_ORDER(不可救)。**item 不在
        庫存主檔恆先判 MISSING_ITEM**(item 是更基礎的阻擋,無論 WO/asset 皆救不了非存在的料號;
        承 ADR-018「不鑄 phantom id」)。

        在呼叫端的 write() 交易內執行(loader 單一交易批次;本方法不自開交易)。`idempotency_key`
        必填:同 key 命中 → DUPLICATE,work_order_part 與 stock_transaction 一起跳過(互鎖)。
        """
        qty = Decimal(str(quantity))
        # item 是最基礎的阻擋:不在庫存主檔 → 無論 WO/asset 皆不可救(FK 無對象),先判。
        if await self.session.get(InventoryItem, item_code) is None:
            return PartIssueOutcome.MISSING_ITEM
        # 歸屬解析:WO 存在→掛工單;否則 compid 有效→掛設備救援;皆無→不可救。
        wo_exists = await self.session.get(WorkOrder, work_order_no) is not None
        target_wo: int | None = work_order_no if wo_exists else None
        target_asset: str | None = None
        if not wo_exists:
            if asset_id is not None and await self.session.get(Asset, asset_id) is not None:
                target_asset = asset_id  # 掛設備救援(missing-wo + 有效 compid)
            else:
                return PartIssueOutcome.MISSING_WORK_ORDER  # 無 WO 且無有效設備 → 不可救
        posted = await InventoryService(self.session).post_stock_transaction(
            item_code=item_code,
            qty_delta=-qty,
            kind="ISSUE",
            actor=actor,
            work_order_no=target_wo,
            charge_target_asset_id=target_asset,
            reason=reason,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            adjust_on_hand=False,  # 回填不扣 on_hand(規則 ②)
        )
        if not posted:
            return PartIssueOutcome.DUPLICATE  # 與 work_order_part 互鎖:不重複記
        if target_wo is not None:
            self.session.add(
                WorkOrderPart(
                    work_order_no=target_wo,
                    item_code=item_code,
                    quantity=qty,
                    source_actor=actor.value,
                    created_by=actor.value,
                )
            )
            return PartIssueOutcome.INSERTED
        return PartIssueOutcome.INSERTED_ASSET  # 掛設備救援(無 work_order_part)

    # ---- ADR-020 工單↔外部知識庫(Jira MRQ)連結(governed;cmms 不呼叫 Jira,只落庫「連了什麼」)----

    async def record_external_link(
        self,
        *,
        work_order_no: int,
        external_key: str,
        link_type: str,
        actor: Actor,
        system: str = "jira",
        title: str | None = None,
        on_behalf_of: str | None = None,
        forward_idem_key: str | None = None,
    ) -> WorkOrderExternalLink:
        """記工單↔外部單(ADR-020 決策 3;冪等)。**allowlist-shape 守門**(決策 8 縱深):
        system ∈ {jira}、external_key 必為 `MRQ-<n>`、link_type ∈ {referenced,forwarded,appended}。

        dual attribution(ADR-005):`source_actor=actor`(agent:<name>,誰轉發)+ `created_by`
        取 `on_behalf_of`(human:<id>,代表誰;預設 actor)。冪等鍵 (wo,system,key,link_type)。
        ★ cmms 端**不呼叫 Jira**(決策 1);實際寫入由 gateway-side forwarder 做,此處只落連結事實。
        """
        await self._assert_not_operator(actor, "record_external_link")
        if system not in EXTERNAL_LINK_SYSTEMS:
            raise WorkOrderError(f"external system not allowed: {system}")
        if not _MRQ_KEY_FULL.match(external_key):
            raise WorkOrderError(f"external_key must be MRQ-<n>: {external_key}")
        if link_type not in EXTERNAL_LINK_TYPES:
            raise WorkOrderError(f"invalid link_type: {link_type}")
        async with self.write(actor):
            if await self.session.get(WorkOrder, work_order_no) is None:
                raise WorkOrderError(f"work_order {work_order_no} not found")
            existing = await self.session.scalar(
                select(WorkOrderExternalLink).where(
                    WorkOrderExternalLink.work_order_no == work_order_no,
                    WorkOrderExternalLink.system == system,
                    WorkOrderExternalLink.external_key == external_key,
                    WorkOrderExternalLink.link_type == link_type,
                )
            )
            if existing is not None:
                if existing.removed_at is not None:  # 移除後再連 = 復活(留稽核)
                    existing.removed_at = None
                    existing.removed_by = None
                    existing.updated_by = actor.value
                    existing.source_actor = actor.value
                return existing  # 冪等
            link = WorkOrderExternalLink(
                work_order_no=work_order_no,
                system=system,
                external_key=external_key,
                link_type=link_type,
                title=title,
                source_actor=actor.value,
                created_by=on_behalf_of or actor.value,
                forward_idem_key=forward_idem_key,
            )
            self.session.add(link)
            await self.session.flush()
            link_id = link.id
        return await self.session.get(WorkOrderExternalLink, link_id)

    async def list_external_links(self, work_order_no: int) -> list[WorkOrderExternalLink]:
        stmt = (
            select(WorkOrderExternalLink)
            .where(
                WorkOrderExternalLink.work_order_no == work_order_no,
                WorkOrderExternalLink.removed_at.is_(None),
            )
            .order_by(WorkOrderExternalLink.id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def remove_external_link(
        self, link_id: int, *, work_order_no: int, actor: Actor
    ) -> None:
        """移除工單↔外部單連結(軟移除,0020:打錯的 MRQ 要能更正,否則 gateway 上線後
        會同步到錯的 issue)。`work_order_no` 為歸屬守門;冪等:已移除 = no-op。"""
        await self._assert_not_operator(actor, "remove_external_link")
        link = await self.session.get(WorkOrderExternalLink, link_id)
        if link is None or link.work_order_no != work_order_no:
            raise WorkOrderError(f"external link {link_id} not found on wo {work_order_no}")
        if link.removed_at is not None:
            return
        async with self.write(actor):
            link.removed_at = self._now()
            link.removed_by = actor.value
            link.updated_by = actor.value
            link.source_actor = actor.value

    async def backfill_legacy_mrq_links(self, actor: Actor) -> int:
        """一次性:legacy `external_ref` 的 MRQ-<n> → link_type=referenced(冪等)。回新建數。"""
        wos = list(
            (
                await self.session.scalars(
                    select(WorkOrder).where(WorkOrder.external_ref.is_not(None))
                )
            ).all()
        )
        created = 0
        async with self.write(actor):
            for wo in wos:
                for key in set(_MRQ_KEY_FIND.findall(wo.external_ref or "")):
                    exists = await self.session.scalar(
                        select(WorkOrderExternalLink.id).where(
                            WorkOrderExternalLink.work_order_no == wo.work_order_no,
                            WorkOrderExternalLink.system == "jira",
                            WorkOrderExternalLink.external_key == key,
                            WorkOrderExternalLink.link_type == "referenced",
                        )
                    )
                    if exists is not None:
                        continue
                    self.session.add(
                        WorkOrderExternalLink(
                            work_order_no=wo.work_order_no,
                            system="jira",
                            external_key=key,
                            link_type="referenced",
                            source_actor=actor.value,
                            created_by=actor.value,
                        )
                    )
                    created += 1
        return created

    # ---- ADR-016 兩階段外部確認(propose / confirm / reject;Profile A)----

    async def list_proposals(
        self, *, status: str | None = "PENDING", limit: int = 100
    ) -> list[PendingProposal]:
        """列提案(ADR-025 Lane 1 管理台審核用;讀取,開放)。預設只列 PENDING,新到舊。"""
        stmt = select(PendingProposal)
        if status is not None:
            stmt = stmt.where(PendingProposal.status == status)
        stmt = stmt.order_by(PendingProposal.created_at.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def list_proposals_by_proposer(
        self, *, proposed_by: str, limit: int = 50, offset: int = 0
    ) -> list[PendingProposal]:
        """列某提案者(`proposed_by` 完全比對,如 `human:jordan.lee`)所有狀態的提案,新到舊。

        供提案人在 `/app/proposals` 追蹤自己提案的進度(唯讀,零寫入;讀取開放 ADR-004)。
        不做 lazy sweep(那是寫入,屬 admin 審核頁的職責)—— 逾期仍 PENDING 者由呼叫端以
        `expires_at` 對現在時間顯示「已逾期」,不改 DB 狀態。"""
        stmt = (
            select(PendingProposal)
            .where(PendingProposal.proposed_by == proposed_by)
            .order_by(PendingProposal.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.scalars(stmt)).all())

    # ---- 稽核 feed 讀取(ADR-019 /admin/audit;每源一個小 list 方法,route 只合併排序)----

    async def list_recent_status_changes(
        self, *, limit: int = 50, actor_like: str | None = None
    ) -> list[WorkOrderStatusHistory]:
        """近期狀態轉移(含 hold_reason;稽核 feed,讀取開放 ADR-004)。
        `actor_like` = source_actor(誰做的)子字串過濾。依 changed_at 新到舊。"""
        stmt = select(WorkOrderStatusHistory)
        if actor_like:
            stmt = stmt.where(WorkOrderStatusHistory.source_actor.ilike(f"%{actor_like}%"))
        stmt = stmt.order_by(
            WorkOrderStatusHistory.changed_at.desc(), WorkOrderStatusHistory.id.desc()
        ).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def list_recent_note_edits(
        self, *, limit: int = 50, actor_like: str | None = None
    ) -> list[WorkOrderNote]:
        """近期被更正過的工作日誌(updated_at 非空 = 事後更正;§1.6 append-only 放寬為可更正)。
        `actor_like` = updated_by(更正者)子字串過濾。依 updated_at 新到舊。"""
        stmt = select(WorkOrderNote).where(WorkOrderNote.updated_at.is_not(None))
        if actor_like:
            stmt = stmt.where(WorkOrderNote.updated_by.ilike(f"%{actor_like}%"))
        stmt = stmt.order_by(
            WorkOrderNote.updated_at.desc(), WorkOrderNote.id.desc()
        ).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def list_resolved_proposals(
        self, *, limit: int = 50, actor_like: str | None = None
    ) -> list[PendingProposal]:
        """近期已裁決提案(CONFIRMED/REJECTED/EXPIRED;稽核 feed)。`actor_like` 比對
        proposed_by 或 confirmed_by。依 resolved_at 新到舊(EXPIRED 亦落 resolved_at)。"""
        stmt = select(PendingProposal).where(
            PendingProposal.status.in_(("CONFIRMED", "REJECTED", "EXPIRED")),
            PendingProposal.resolved_at.is_not(None),
        )
        if actor_like:
            like = f"%{actor_like}%"
            stmt = stmt.where(
                or_(
                    PendingProposal.proposed_by.ilike(like),
                    PendingProposal.confirmed_by.ilike(like),
                )
            )
        stmt = stmt.order_by(PendingProposal.resolved_at.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def find_pending_proposal(
        self,
        *,
        operation: str,
        work_order_no: int | None = None,
        item_code: str | None = None,
        at: datetime | None = None,
    ) -> PendingProposal | None:
        """某目標(工單或品項)是否已有同操作的未過期 PENDING 提案(targeted 查詢;
        review f14cf8d:取代「撈全佇列再 Python 過濾」—— 佇列 >limit 時會漏、每 render 全掃)。"""
        now = at or self._now()
        stmt = select(PendingProposal).where(
            PendingProposal.status == "PENDING",
            PendingProposal.operation == operation,
            PendingProposal.expires_at > now,
        )
        if work_order_no is not None:
            stmt = stmt.where(
                PendingProposal.params["work_order_no"].as_integer() == work_order_no
            )
        if item_code is not None:
            stmt = stmt.where(PendingProposal.params["item_code"].as_string() == item_code)
        return await self.session.scalar(stmt.order_by(PendingProposal.created_at).limit(1))

    async def expire_stale_proposals(self, *, actor: Actor, at: datetime | None = None) -> int:
        """把逾期仍 PENDING 的提案標為 EXPIRED(lazy sweep;admin 審核頁載入時呼叫)。
        review f14cf8d:先前只有 confirm 失敗當下會標,且該標記隨交易回滾 → 永卡 PENDING。"""
        now = at or self._now()
        async with self.write(actor):
            stale = list(
                (
                    await self.session.scalars(
                        select(PendingProposal).where(
                            PendingProposal.status == "PENDING",
                            PendingProposal.expires_at <= now,
                        )
                    )
                ).all()
            )
            for p in stale:
                p.status = "EXPIRED"
                p.resolved_at = now
        return len(stale)

    async def propose(
        self,
        *,
        operation: str,
        params: dict,
        proposed_by: Actor,
        idempotency_key: str | None = None,
        ttl_seconds: int | None = None,
        at: datetime | None = None,
    ) -> PendingProposal:
        """建立待確認提案(不立即執行),回 pending token + dry-run diff。

        agent 僅 Profile A 操作集(`open_work_order`/`close_work_order`);human proposer
        另可提案 `void_work_order`(ADR-025 Lane 1「請求作廢」,仍須 admin confirm 才執行)。
        同 idempotency_key 重複 propose 回既有提案(ADR-006)。

        review f14cf8d 加固:① TTL 預設依 proposer 身分決定(human 7 天等 admin 排審、
        agent 1h),不再靠呼叫端記得傳 ② `void_work_order` 提案先驗「該單目前可作廢」
        (不收註定 confirm 不了的提案)+ 同單同操作已有 PENDING → 回既有(防重複請求)。
        """
        # operator 閘:提案(請求作廢 / 品項修改提案)非 operator 職責;agent proposer
        # (Profile A)非 operator,不受影響。
        await self._assert_not_operator(proposed_by, "propose")
        allowed = HUMAN_PROPOSABLE_OPS if proposed_by.is_human() else PROPOSABLE_OPS
        if operation not in allowed:
            raise WorkOrderError(f"operation not proposable (Profile A only): {operation}")
        if ttl_seconds is None:
            ttl_seconds = (
                HUMAN_PROPOSAL_TTL_SECONDS
                if proposed_by.is_human()
                else DEFAULT_PROPOSAL_TTL_SECONDS
            )
        now = at or self._now()
        if operation == "void_work_order":
            wo_no = params.get("work_order_no")
            wo = await self.session.get(WorkOrder, wo_no) if wo_no is not None else None
            if wo is None:
                raise WorkOrderError(f"work_order {wo_no} not found")
            if "VOIDED" not in ALLOWED_TRANSITIONS.get(wo.status, set()):
                raise WorkOrderError(f"wo {wo_no}: cannot void from status {wo.status}")
            existing_pending = await self.find_pending_proposal(
                operation=operation, work_order_no=wo.work_order_no, at=now
            )
            if existing_pending is not None:
                return existing_pending
        if operation == "update_item":
            item_code = params.get("item_code")
            if not item_code or await self.session.get(InventoryItem, item_code) is None:
                raise WorkOrderError(f"inventory_item {item_code} not found")
            self._coerce_item_params(params)  # 數字欄位先驗(壞輸入在提案端就擋)
            existing_pending = await self.find_pending_proposal(
                operation=operation, item_code=item_code, at=now
            )
            if existing_pending is not None:
                return existing_pending
        async with self.write(proposed_by):
            if idempotency_key is not None:
                existing = await self.session.scalar(
                    select(PendingProposal).where(
                        PendingProposal.idempotency_key == idempotency_key
                    )
                )
                if existing is not None:
                    return existing
            proposal = PendingProposal(
                pending_token=secrets.token_urlsafe(24),
                operation=operation,
                params=params,
                dry_run_diff=await self._dry_run_diff(operation, params),
                proposed_by=proposed_by.value,
                idempotency_key=idempotency_key,
                status="PENDING",
                expires_at=now + timedelta(seconds=ttl_seconds),
            )
            self.session.add(proposal)
            await self.session.flush()
        return proposal

    async def confirm(
        self, *, pending_token: str, confirmer: Actor, at: datetime | None = None
    ) -> WorkOrder | InventoryItem:
        """確認並執行提案。**授權來自已驗證的 `human:<id>`,拒匿名/agent**(ADR-016)。
        回執行結果實體(WO 操作 → WorkOrder;update_item → InventoryItem)。

        review f14cf8d 加固:① 逾期 → 先在**獨立交易**把 EXPIRED 落庫再 raise(先前標記
        跟著 raise 回滾,提案永卡 PENDING)② **所有 confirm 一律**要求 confirmer 是現行 active
        的 **admin**,在 domain 強制 —— `/mcp` 上公網後(commit 8d9d811),持 scoped token 者
        不得自行確認自己的提案;自報 human id 不等於審核權(對齊 ADR-016/ADR-027「agent 不能
        自我確認」)。峰會裁決「confirm 回家做」後,PROPOSABLE_OPS(開/結工單)亦一律 admin,
        不再豁免——無現存消費者依賴舊的「Profile A 免 admin」路徑。"""
        if not confirmer.is_human():
            raise WorkOrderError(
                "confirm requires a verified human:<id> (anonymous/agent rejected)"
            )
        proposal = await self.session.get(PendingProposal, pending_token)
        if proposal is None:
            raise WorkOrderError(f"unknown pending_token: {pending_token}")
        now = at or self._now()
        if proposal.status != "PENDING":
            raise WorkOrderError(f"proposal not pending (status={proposal.status})")
        if proposal.expires_at <= now:
            async with self.write(confirmer):  # 標記要活過 raise → 獨立交易先 commit
                proposal.status = "EXPIRED"
                proposal.resolved_at = now
            raise WorkOrderError("proposal expired")
        await assert_active_admin(self.session, confirmer)  # 所有 confirm 一律驗 admin
        async with self.write(confirmer):
            wo = await self._execute_proposed(
                proposal.operation, dict(proposal.params), confirmer, at=now
            )
            proposal.status = "CONFIRMED"
            proposal.confirmed_by = confirmer.value
            proposal.resolved_at = now
            await self.session.flush()
        return wo

    async def reject(self, *, pending_token: str, by: Actor, at: datetime | None = None) -> None:
        """丟棄提案(不執行)。低風險(只把 PENDING → REJECTED),故不強制 admin;但**拒匿名/agent**
        —— `/mcp` 上公網後 rejecter 身分綁 transport(見 mcp/server.py),不接受自報字串,且
        agent 不得自行丟棄待人審的提案。"""
        if not by.is_human():
            raise WorkOrderError(
                "reject requires a verified human:<id> (anonymous/agent rejected)"
            )
        async with self.write(by):
            proposal = await self.session.get(PendingProposal, pending_token)
            if proposal is None:
                raise WorkOrderError(f"unknown pending_token: {pending_token}")
            if proposal.status == "PENDING":
                proposal.status = "REJECTED"
                proposal.confirmed_by = by.value
                proposal.resolved_at = at or self._now()

    @staticmethod
    def _coerce_item_params(params: dict) -> dict:
        """update_item 提案 params(JSONB 原始字串)→ `_update_item_impl` 引數。

        propose 端先呼叫一次驗格式(壞數字提案端就擋);confirm 端再呼叫取執行引數。
        `supplier_org_id` 空字串 = 清除連結(與 admin 表單語意一致)。
        """
        out: dict = {}
        try:
            for f in _ITEM_STR_FIELDS:
                v = (params.get(f) or "").strip()
                out[f] = v or None
            for f in _ITEM_DEC_FIELDS:
                v = (str(params.get(f) or "")).strip()
                out[f] = Decimal(v) if v else None
            v = (str(params.get("lead_time_weeks") or "")).strip()
            out["lead_time_weeks"] = int(v) if v else None
            for f in _ITEM_BOOL_FIELDS:
                out[f] = bool(params.get(f))
        except (ArithmeticError, ValueError) as e:
            raise WorkOrderError(f"invalid item field value: {e}") from e
        return out

    async def _dry_run_diff(self, operation: str, params: dict) -> dict:
        if operation == "update_item":
            # 提案審核要看得懂:只列「會變的欄位」old → new(admin 據此判斷)
            item = await self.session.get(InventoryItem, params.get("item_code"))
            coerced = self._coerce_item_params(params)
            changes: dict = {}
            if item is not None:
                for field, new in coerced.items():
                    old = getattr(item, field, None)
                    # Decimal("20.000")==Decimal("20") 走 ==;其餘型別以 str 正規化比對
                    if old != new and str(old) != str(new):
                        changes[field] = {"from": str(old), "to": str(new)}
            return {
                "action": "update_item",
                "item_code": params.get("item_code"),
                "changes": changes,
            }
        if operation == "open_work_order":
            return {
                "action": "create_work_order",
                "asset_id": params.get("asset_id"),
                "work_type": params.get("work_type"),
                "result_status": "OPEN",
            }
        if operation == "close_work_order":
            wo = await self.session.get(WorkOrder, params.get("work_order_no"))
            return {
                "action": "close_work_order",
                "work_order_no": params.get("work_order_no"),
                "from_status": wo.status if wo else None,
                "to_status": "CLOSED",
                # D6 選填真因(efc);None → 不設。確認者於 dry-run 一眼可見
                "confirmed_reason_code": params.get("confirmed_reason_code"),
            }
        if operation == "void_work_order":
            wo = await self.session.get(WorkOrder, params.get("work_order_no"))
            return {
                "action": "void_work_order",
                "work_order_no": params.get("work_order_no"),
                "from_status": wo.status if wo else None,
                "to_status": "VOIDED",
                "reason": params.get("reason"),
            }
        return {"action": operation, "params": params}

    async def _execute_proposed(
        self, operation: str, params: dict, actor: Actor, *, at: datetime
    ) -> WorkOrder | InventoryItem:
        """在 confirm 的交易內經單一寫入路徑執行提案(不另開交易)。"""
        if operation == "open_work_order":
            return await self._open_impl(
                asset_id=params["asset_id"],
                work_type=params["work_type"],
                actor=actor,
                brief_description=params.get("brief_description"),
                opened_by=params.get("opened_by"),
                at=at,
            )
        if operation == "close_work_order":
            reason = await self._validate_confirmed_reason(params.get("confirmed_reason_code"))
            wo = await self._transition(params["work_order_no"], "CLOSED", actor, at=at)
            self._assert_reason_applicable(wo, reason)  # 真因僅 REACTIVE
            self._record_completion_fields(wo, None, None, reason)
            return wo
        if operation == "void_work_order":
            # 請求作廢(ADR-025 Lane 1):confirm 執行作廢;事由 note 連結該次轉移、同交易原子寫。
            return await self._transition(
                params["work_order_no"],
                "VOIDED",
                actor,
                at=at,
                note_body=(params.get("reason") or "").strip() or None,
            )
        if operation == "update_item":
            # 品項修改提案(裁決 #3 後續):confirm 在同一交易內經 inventory 單一寫入路徑執行
            try:
                return await InventoryService(self.session)._update_item_impl(
                    params["item_code"], actor=actor, **self._coerce_item_params(params)
                )
            except InventoryError as e:  # 統一以 WorkOrderError 面向 confirm 呼叫端
                raise WorkOrderError(str(e)) from e
        raise WorkOrderError(f"non-executable operation: {operation}")

    # ---- ADR-017 Profile B on-box(Analytics 簽章 JWS;單步、機台歸屬)----

    async def open_reactive_work_order_onbox(
        self, *, jws_token: str, key_resolver: KeyResolver, at: datetime | None = None
    ) -> WorkOrder:
        """設備端一鍵報修:驗 Analytics JWS → 開 REACTIVE WO(站別歸屬,單步)。

        idempotent(同 on-box `idempotency_key` 回既有 WO);未知 EID 拒收(ADR-017 Q6)。
        """
        claims = verify_onbox_jws(jws_token, key_resolver=key_resolver)
        if claims.op != "open_reactive_work_order":
            raise OnboxVerificationError(f"op mismatch: got {claims.op}")
        actor = Actor.agent("analytics-onbox")
        async with self.write(actor):
            existing = await self.session.scalar(
                select(WorkOrder).where(WorkOrder.idempotency_key == claims.idempotency_key)
            )
            if existing is not None:
                return existing
            if await self.session.get(Asset, claims.asset_id) is None:
                raise OnboxVerificationError(
                    f"unknown EID (not in asset master): {claims.asset_id}"
                )
            wo = await self._open_impl(
                asset_id=claims.asset_id,
                work_type="REACTIVE",
                actor=actor,
                origin_station=claims.origin_station,
                idempotency_key=claims.idempotency_key,
                evidence_ref=claims.evidence_ref,
                at=at,
            )
        return wo

    async def cancel_reactive_report_onbox(
        self, *, jws_token: str, key_resolver: KeyResolver, at: datetime | None = None
    ) -> WorkOrder:
        """設備端取消報修:驗 Analytics JWS → soft-cancel(限該 on-box 單仍 OPEN;ADR-017 Q4)。"""
        claims = verify_onbox_jws(jws_token, key_resolver=key_resolver)
        if claims.op != "cancel_reactive_report":
            raise OnboxVerificationError(f"op mismatch: got {claims.op}")
        actor = Actor.agent("analytics-onbox")
        async with self.write(actor):
            wo = await self.session.scalar(
                select(WorkOrder).where(WorkOrder.idempotency_key == claims.idempotency_key)
            )
            if wo is None:
                raise WorkOrderError(
                    f"no on-box work_order for idempotency_key {claims.idempotency_key}"
                )
            if wo.status == "CANCELLED":
                return wo  # idempotent
            # OPEN→CANCELLED 由狀態機保證「仍 open、未被接手」(否則 InvalidTransition)
            await self._transition(wo.work_order_no, "CANCELLED", actor, at=at)
        return wo

    async def downtime_lookup_maps(self) -> tuple[dict[str, bool], dict[str, bool]]:
        """載入 downtime 判定用的 lookup 值域(status_is_downtime, hold_is_downtime)。

        供純函式 `segment_is_downtime` 的呼叫端(引擎/讀 API)一次載入、重複使用
        (active-in 一次可回 2000 單,不得每單/每段重查 DB)。
        """
        status_is_downtime = {
            s.code: s.is_downtime for s in (await self.session.scalars(select(WoStatus))).all()
        }
        hold_is_downtime = {
            h.code: h.is_downtime for h in (await self.session.scalars(select(WoHoldReason))).all()
        }
        return status_is_downtime, hold_is_downtime

    async def _recompute_downtime(self, wo: WorkOrder) -> None:
        """依 status_history 的 down 區段累加 downtime(只計生產時段;work_type-aware 純函式)。

        走單一語意源 `segment_is_downtime`(把 `wo.work_type` 傳入):REACTIVE OPEN 計入、
        PM OPEN 不計、ON_HOLD 依 hold_reason、終態不計。**不回溯**——只在未來結案時跑。
        """
        history = await self.get_status_history(wo.work_order_no)
        status_is_downtime, hold_is_downtime = await self.downtime_lookup_maps()
        total = 0
        for cur, nxt in zip(history, history[1:], strict=False):
            if segment_is_downtime(
                wo.work_type,
                cur.to_status,
                cur.hold_reason,
                status_is_downtime=status_is_downtime,
                hold_is_downtime=hold_is_downtime,
            ):
                start = to_taipei_naive(cur.changed_at)
                end = to_taipei_naive(nxt.changed_at)
                total += productive_minutes(start, end)
        wo.downtime_minutes = total
        wo.downtime_estimated = False  # 由系統時間戳精算
