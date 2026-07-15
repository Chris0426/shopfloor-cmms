"""ExportService — 匯出中心的唯讀領域服務(ADR-004 讀取開放,零寫入)。

五個資料集,各提供 `count_<ds>(**filters) -> int` 與 `rows_<ds>(*, limit, **filters) ->
list[dict]`;dict 的鍵即欄位名(web 層 ColumnSpec.key 對映)。所有軟刪一律過濾
(work_order_part / task_step / task_part 的 deleted_at IS NULL)。排序 deterministic
(見各 `_build_*`),讓分頁 / 預覽 / 下載三者一致。

設計取向:每個資料集一個 `_build_*(**filters) -> (Select, order_by)`,count 與 rows 共用同一
`Select`(count 包成子查詢數列、rows 加排序 + limit)。列以 `select(具名欄位…)` 取出 → 直接
`dict(row._mapping)`,與 ORM 解耦、CSV 渲染只認鍵。join 到顯示欄(設備名 / 品名 / 任務名)一律
outer join,避免因參照缺漏整列消失。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import (
    BigInteger,
    Select,
    and_,
    cast,
    exists,
    false,
    func,
    literal,
    literal_column,
    null,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import aliased

from cmms.domain.asset.models import Asset, AssetOwner, AssetType
from cmms.domain.base import DomainService
from cmms.domain.inventory.models import InventoryItem, StockTransaction
from cmms.domain.pm_schedule.models import FreqUnit, PmSchedule
from cmms.domain.task.models import Task, TaskPart, TaskStep
from cmms.domain.work_order.models import (
    WorkOrder,
    WorkOrderAssignee,
    WorkOrderPart,
    WorkType,
)
from cmms.domain.work_order.transform import TAIPEI


def _owners_agg(asset_id_col):
    """設備負責人清單 → 「; 」相接的純量子查詢(0031;依 position)。無 → NULL。"""
    return (
        select(
            func.string_agg(
                AssetOwner.person_name,
                aggregate_order_by(
                    literal_column("'; '"), AssetOwner.position, AssetOwner.person_name
                ),
            )
        )
        .where(AssetOwner.asset_id == asset_id_col)
        .scalar_subquery()
    )


def _assignees_agg(work_order_no_col):
    """工單負責人清單 → 「; 」相接的純量子查詢(0031;依 position)。無 → NULL。"""
    return (
        select(
            func.string_agg(
                WorkOrderAssignee.person_name,
                aggregate_order_by(
                    literal_column("'; '"),
                    WorkOrderAssignee.position,
                    WorkOrderAssignee.person_name,
                ),
            )
        )
        .where(WorkOrderAssignee.work_order_no == work_order_no_col)
        .scalar_subquery()
    )


# 過濾器 chips 的動態選項來源(受控 lookup 表;code+label,唯讀)。
_LOOKUP_MODELS = {
    "work_type": WorkType,
    "asset_type": AssetType,
    "frequency_unit": FreqUnit,
}


def _day_bounds_taipei(d: date) -> datetime:
    """某廠區(台北)日曆日的 00:00 tz-aware 起點。timestamptz 欄以此比較 = 台北日語意。"""
    return datetime(d.year, d.month, d.day, tzinfo=TAIPEI)


class ExportService(DomainService):
    # ---- 過濾器選項(受控 lookup;讀取開放)----

    async def lookup_options(self, source: str) -> list[tuple[str, str]]:
        """chips 過濾器的動態選項:回 (code, label) 依 code 排序。未知來源 → 空。"""
        model = _LOOKUP_MODELS.get(source)
        if model is None:
            return []
        rows = (await self.session.scalars(select(model).order_by(model.code))).all()
        return [(r.code, r.label) for r in rows]

    # ---- 通用:count / rows 共用同一 Select ----

    async def _count(self, base: Select) -> int:
        """數符合列數(把 base 包成子查詢跑 COUNT(*),不受 base 的欄位 / join 影響)。"""
        n = await self.session.scalar(select(func.count()).select_from(base.subquery()))
        return int(n or 0)

    async def _rows(
        self, base: Select, order_by: list, limit: int | None
    ) -> list[dict]:
        stmt = base.order_by(*order_by)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return [dict(r._mapping) for r in result]

    # ---- ① 工單 ----

    def _build_work_orders(
        self,
        *,
        statuses: list[str] | None = None,
        work_types: list[str] | None = None,
        opened_from: date | None = None,
        opened_to: date | None = None,
        closed_from: date | None = None,
        closed_to: date | None = None,
        asset_id: str | None = None,
        assigned_person: str | None = None,
        assigned_vendor: str | None = None,
    ) -> tuple[Select, list]:
        base = (
            select(
                WorkOrder.work_order_no.label("work_order_no"),
                WorkOrder.asset_id.label("asset_id"),
                Asset.description.label("asset_description"),
                WorkOrder.work_type.label("work_type"),
                WorkOrder.status.label("status"),
                WorkOrder.brief_description.label("brief_description"),
                WorkOrder.diagnosis.label("diagnosis"),
                # 0031:匯出全部負責人「; 」相接(交叉表);無交叉表列(如純測試建構)退回
                # denormalized 相容欄。真實資料回填後皆有交叉表。
                func.coalesce(
                    _assignees_agg(WorkOrder.work_order_no), WorkOrder.assigned_person
                ).label("assigned_person"),
                WorkOrder.assigned_vendor.label("assigned_vendor"),
                WorkOrder.priority.label("priority"),
                WorkOrder.external_ref.label("external_ref"),
                WorkOrder.opened_date.label("opened_date"),
                WorkOrder.scheduled_date.label("scheduled_date"),
                WorkOrder.closed_date.label("closed_date"),
                WorkOrder.opened_at.label("opened_at"),
                WorkOrder.closed_at.label("closed_at"),
                WorkOrder.downtime_minutes.label("downtime_minutes"),
                WorkOrder.downtime_estimated.label("downtime_estimated"),
                WorkOrder.hold_reason.label("hold_reason"),
                WorkOrder.labor_hours.label("labor_hours"),
                WorkOrder.cost.label("cost"),
                WorkOrder.action_taken.label("action_taken"),
            )
            .select_from(WorkOrder)
            .outerjoin(Asset, Asset.asset_id == WorkOrder.asset_id)
        )
        if statuses:
            base = base.where(WorkOrder.status.in_(statuses))
        if work_types:
            base = base.where(WorkOrder.work_type.in_(work_types))
        if opened_from is not None:
            base = base.where(WorkOrder.opened_date >= opened_from)
        if opened_to is not None:
            base = base.where(WorkOrder.opened_date <= opened_to)
        if closed_from is not None:
            base = base.where(WorkOrder.closed_date >= closed_from)
        if closed_to is not None:
            base = base.where(WorkOrder.closed_date <= closed_to)
        if asset_id:
            base = base.where(WorkOrder.asset_id == asset_id)
        if assigned_person:
            base = base.where(WorkOrder.assigned_person == assigned_person)
        if assigned_vendor:
            base = base.where(WorkOrder.assigned_vendor == assigned_vendor)
        return base, [WorkOrder.work_order_no.desc()]

    async def count_work_orders(self, **f) -> int:
        base, _ = self._build_work_orders(**f)
        return await self._count(base)

    async def rows_work_orders(self, *, limit: int | None = None, **f) -> list[dict]:
        base, order_by = self._build_work_orders(**f)
        return await self._rows(base, order_by, limit)

    # ---- ② 領料(工單領料 work_order_part ∪ 直掛設備直領 stock_transaction;ADR-024)----

    def _part_usage_wo_select(
        self,
        *,
        issued_from: date | None,
        issued_to: date | None,
        item_code: str | None,
        asset_id: str | None,
        work_order_no: int | None,
    ) -> Select:
        """分支 a:工單領料(work_order_part;軟刪過濾)。asset 經工單解析、issue_type='work_order'。"""
        stmt = (
            select(
                WorkOrderPart.created_at.label("issued_at"),
                WorkOrderPart.work_order_no.label("work_order_no"),
                WorkOrder.asset_id.label("asset_id"),
                literal("work_order").label("issue_type"),
                WorkOrderPart.item_code.label("item_code"),
                InventoryItem.name.label("item_name"),
                WorkOrderPart.quantity.label("quantity"),
                # 合成穩定 tiebreaker(不曝成 CSV 欄);分支前綴避免跨分支 id 碰撞
                func.concat("wop:", WorkOrderPart.id).label("row_key"),
            )
            .select_from(WorkOrderPart)
            .join(WorkOrder, WorkOrder.work_order_no == WorkOrderPart.work_order_no)
            .outerjoin(InventoryItem, InventoryItem.item_code == WorkOrderPart.item_code)
            .where(WorkOrderPart.deleted_at.is_(None))
        )
        # created_at 為 UTC timestamptz;領料日過濾以台北日邊界比較(與其他頁的台北語意一致)
        if issued_from is not None:
            stmt = stmt.where(WorkOrderPart.created_at >= _day_bounds_taipei(issued_from))
        if issued_to is not None:
            stmt = stmt.where(
                WorkOrderPart.created_at < _day_bounds_taipei(issued_to) + timedelta(days=1)
            )
        if item_code:
            stmt = stmt.where(WorkOrderPart.item_code == item_code)
        if asset_id:
            stmt = stmt.where(WorkOrder.asset_id == asset_id)
        if work_order_no is not None:
            stmt = stmt.where(WorkOrderPart.work_order_no == work_order_no)
        return stmt

    def _part_usage_direct_select(
        self,
        *,
        issued_from: date | None,
        issued_to: date | None,
        item_code: str | None,
        asset_id: str | None,
    ) -> Select:
        """分支 b:非工單直領(stock_transaction ISSUE 直掛設備;ADR-024)。

        歸屬 = `charge_target_asset_id`;quantity=−qty_delta(正數,對齊工單領料語意);
        排除已取消/被改量 supersede 的直領 —— 判準 = 存在對應決定性取消 RETURN 帳
        (idempotency_key='assetissuecancel:v1:'||txn_id),與工單側軟刪排除語意對稱。
        """
        cancel = aliased(StockTransaction)  # 同表自參照:取消 RETURN 帳
        cancel_sq = select(cancel.txn_id).where(
            cancel.kind == "RETURN",
            cancel.idempotency_key
            == func.concat("assetissuecancel:v1:", StockTransaction.txn_id),
        )
        stmt = (
            select(
                StockTransaction.occurred_at.label("issued_at"),
                cast(null(), BigInteger).label("work_order_no"),  # 直領無工單
                StockTransaction.charge_target_asset_id.label("asset_id"),
                literal("direct").label("issue_type"),
                StockTransaction.item_code.label("item_code"),
                InventoryItem.name.label("item_name"),
                (-StockTransaction.qty_delta).label("quantity"),  # ISSUE 為負 → 領出量正數
                func.concat("txn:", StockTransaction.txn_id).label("row_key"),
            )
            .select_from(StockTransaction)
            .outerjoin(
                InventoryItem, InventoryItem.item_code == StockTransaction.item_code
            )
            .where(
                StockTransaction.kind == "ISSUE",
                StockTransaction.charge_target_asset_id.is_not(None),
                ~exists(cancel_sq),
            )
        )
        # occurred_at 為 UTC timestamptz;同以台北日邊界比較(與分支 a 一致)
        if issued_from is not None:
            stmt = stmt.where(StockTransaction.occurred_at >= _day_bounds_taipei(issued_from))
        if issued_to is not None:
            stmt = stmt.where(
                StockTransaction.occurred_at < _day_bounds_taipei(issued_to) + timedelta(days=1)
            )
        if item_code:
            stmt = stmt.where(StockTransaction.item_code == item_code)
        if asset_id:
            stmt = stmt.where(StockTransaction.charge_target_asset_id == asset_id)
        return stmt

    def _build_part_usage(
        self,
        *,
        issued_from: date | None = None,
        issued_to: date | None = None,
        item_code: str | None = None,
        asset_id: str | None = None,
        work_order_no: int | None = None,
        issue_type: str | None = None,
    ) -> tuple[Select, list]:
        it = (issue_type or "").strip().lower() or None
        include_wo = it in (None, "work_order")
        # 直領無工單:一旦帶工單號過濾即語意排除直領分支(與「工單號=工單領料」對稱)
        include_direct = it in (None, "direct") and work_order_no is None
        parts: list[Select] = []
        if include_wo:
            parts.append(
                self._part_usage_wo_select(
                    issued_from=issued_from, issued_to=issued_to, item_code=item_code,
                    asset_id=asset_id, work_order_no=work_order_no,
                )
            )
        if include_direct:
            parts.append(
                self._part_usage_direct_select(
                    issued_from=issued_from, issued_to=issued_to, item_code=item_code,
                    asset_id=asset_id,
                )
            )
        if not parts:
            # 矛盾過濾(如 issue_type=direct + 指定工單號)→ 空集合;保留欄位形狀
            parts.append(
                self._part_usage_wo_select(
                    issued_from=issued_from, issued_to=issued_to, item_code=item_code,
                    asset_id=asset_id, work_order_no=work_order_no,
                ).where(false())
            )
        combined = parts[0] if len(parts) == 1 else union_all(*parts)
        sq = combined.subquery()
        base = select(sq)
        return base, [sq.c.issued_at.desc(), sq.c.row_key.asc()]

    async def count_part_usage(self, **f) -> int:
        base, _ = self._build_part_usage(**f)
        return await self._count(base)

    async def rows_part_usage(self, *, limit: int | None = None, **f) -> list[dict]:
        base, order_by = self._build_part_usage(**f)
        return await self._rows(base, order_by, limit)

    # ---- ③ 設備 ----

    def _build_assets(
        self,
        *,
        asset_types: list[str] | None = None,
        department: str | None = None,
        line: str | None = None,
        is_active: bool | None = None,
    ) -> tuple[Select, list]:
        base = select(
            Asset.asset_id.label("asset_id"),
            Asset.description.label("description"),
            Asset.asset_type.label("asset_type"),
            Asset.asset_subtype.label("asset_subtype"),
            Asset.department.label("department"),
            Asset.line.label("line"),
            Asset.site.label("site"),
            Asset.parent_asset_id.label("parent_asset_id"),
            Asset.manufacturer.label("manufacturer"),
            Asset.model_no.label("model_no"),
            Asset.serial_no.label("serial_no"),
            # 0031:設備負責人(全部)「; 」相接(工單/PM assignee 事實來源;asset_owner 交叉表)
            _owners_agg(Asset.asset_id).label("owner"),
            Asset.is_active.label("is_active"),
            Asset.available_for_service.label("available_for_service"),
        )
        if asset_types:
            base = base.where(Asset.asset_type.in_(asset_types))
        if department:
            base = base.where(Asset.department == department)
        if line:
            base = base.where(Asset.line == line)
        if is_active is not None:
            base = base.where(Asset.is_active == is_active)
        return base, [Asset.asset_id.asc()]

    async def count_assets(self, **f) -> int:
        base, _ = self._build_assets(**f)
        return await self._count(base)

    async def rows_assets(self, *, limit: int | None = None, **f) -> list[dict]:
        base, order_by = self._build_assets(**f)
        return await self._rows(base, order_by, limit)

    # ---- ④ 保養排程 ----

    def _build_pm_schedules(
        self,
        *,
        asset_id: str | None = None,
        assigned_vendor: str | None = None,
        assigned_person: str | None = None,
        frequency_units: list[str] | None = None,
        due_from: date | None = None,
        due_to: date | None = None,
        is_suppressed: bool | None = None,
    ) -> tuple[Select, list]:
        base = (
            select(
                PmSchedule.pm_id.label("pm_id"),
                PmSchedule.asset_id.label("asset_id"),
                PmSchedule.task_id.label("task_id"),
                Task.description.label("task_name"),
                PmSchedule.frequency_interval.label("frequency_interval"),
                PmSchedule.frequency_unit.label("frequency_unit"),
                PmSchedule.calendar_freq_type.label("calendar_freq_type"),
                PmSchedule.next_due_date.label("next_due_date"),
                PmSchedule.last_pm_date.label("last_pm_date"),
                PmSchedule.last_work_order_no.label("last_work_order_no"),
                PmSchedule.completion_window_days.label("completion_window_days"),
                PmSchedule.standard_hours.label("standard_hours"),
                PmSchedule.estimated_labor_hours.label("estimated_labor_hours"),
                PmSchedule.assigned_vendor.label("assigned_vendor"),
                # 0031:匯出有效 assignee = per-PM 覆寫(單值)否則設備負責人(全部「; 」相接)
                func.coalesce(
                    PmSchedule.assigned_person, _owners_agg(PmSchedule.asset_id)
                ).label("assigned_person"),
                PmSchedule.is_suppressed.label("is_suppressed"),
            )
            .select_from(PmSchedule)
            .outerjoin(Task, Task.task_no == PmSchedule.task_id)
            .outerjoin(Asset, Asset.asset_id == PmSchedule.asset_id)
        )
        if asset_id:
            base = base.where(PmSchedule.asset_id == asset_id)
        if assigned_vendor:
            base = base.where(PmSchedule.assigned_vendor == assigned_vendor)
        if assigned_person:
            base = base.where(
                func.coalesce(PmSchedule.assigned_person, _owners_agg(PmSchedule.asset_id))
                == assigned_person
            )
        if frequency_units:
            base = base.where(PmSchedule.frequency_unit.in_(frequency_units))
        if due_from is not None:
            base = base.where(
                PmSchedule.next_due_date.is_not(None), PmSchedule.next_due_date >= due_from
            )
        if due_to is not None:
            base = base.where(
                PmSchedule.next_due_date.is_not(None), PmSchedule.next_due_date <= due_to
            )
        if is_suppressed is not None:
            base = base.where(PmSchedule.is_suppressed == is_suppressed)
        return base, [PmSchedule.pm_id.asc()]

    async def count_pm_schedules(self, **f) -> int:
        base, _ = self._build_pm_schedules(**f)
        return await self._count(base)

    async def rows_pm_schedules(self, *, limit: int | None = None, **f) -> list[dict]:
        base, order_by = self._build_pm_schedules(**f)
        return await self._rows(base, order_by, limit)

    # ---- ⑤ 保養步驟明細(pm_schedule × 步驟 × 用料;粒度=每步一列,多料展多列)----

    def _build_pm_task_details(
        self,
        *,
        asset_id: str | None = None,
        task_no: str | None = None,
        task_desc: str | None = None,
    ) -> tuple[Select, list]:
        # 步驟 UI 序(1..N):每個 task 內以 (proc_seq NULLS LAST, id) 枚舉。必須在「未 join 用料」
        # 前算(否則多料步驟會被 row_number 拆成不同序號)→ 先在 task_step 子查詢算好 step_no。
        step_no = func.row_number().over(
            partition_by=TaskStep.task_no,
            order_by=(TaskStep.proc_seq.nulls_last(), TaskStep.id),
        ).label("step_no")
        step_sq = (
            select(
                TaskStep.id.label("step_id"),
                TaskStep.task_no.label("task_no"),
                TaskStep.task_desc.label("step_desc"),
                step_no,
            )
            .where(TaskStep.deleted_at.is_(None))
            .subquery()
        )
        base = (
            select(
                PmSchedule.pm_id.label("pm_id"),
                PmSchedule.asset_id.label("asset_id"),
                Task.task_no.label("task_no"),
                Task.description.label("task_name"),
                step_sq.c.step_no.label("step_no"),
                step_sq.c.step_desc.label("step_desc"),
                TaskPart.item_code.label("item_code"),
                InventoryItem.name.label("item_name"),
                TaskPart.replace_qty.label("replace_qty"),
            )
            .select_from(PmSchedule)
            .join(Task, Task.task_no == PmSchedule.task_id)
            .join(step_sq, step_sq.c.task_no == Task.task_no)
            .outerjoin(
                TaskPart,
                and_(
                    TaskPart.task_step_id == step_sq.c.step_id,
                    TaskPart.deleted_at.is_(None),
                ),
            )
            .outerjoin(InventoryItem, InventoryItem.item_code == TaskPart.item_code)
        )
        if asset_id:
            base = base.where(PmSchedule.asset_id == asset_id)
        if task_no:
            base = base.where(Task.task_no == task_no)
        if task_desc:
            base = base.where(Task.description.ilike(f"%{task_desc}%"))
        # pm_id 領頭 = 決定性 tiebreaker(多個 PM 共用同一 task 時,各自的步驟不交錯);其後照
        # spec 的 task_no → 步驟序(proc_seq/id 已編進 step_no)→ 用料 id。
        order_by = [
            PmSchedule.pm_id.asc(),
            Task.task_no.asc(),
            step_sq.c.step_no.asc(),
            TaskPart.id.asc().nulls_last(),
        ]
        return base, order_by

    async def count_pm_task_details(self, **f) -> int:
        base, _ = self._build_pm_task_details(**f)
        return await self._count(base)

    async def rows_pm_task_details(self, *, limit: int | None = None, **f) -> list[dict]:
        base, order_by = self._build_pm_task_details(**f)
        return await self._rows(base, order_by, limit)
