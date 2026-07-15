"""work_orders.csv 載入器(migration 資料輸入)。

經 WorkOrderService 單一寫入路徑寫入(ADR-001),idempotent(可重跑)。
★ 編碼:work_orders.csv 為 **latin-1**(含 HTML-entity 中文),非 utf-8-sig。
★ #4b:`miscreated=T`(誤開)整列丟棄(Jordan 2026-06-22);wo_status 種 canonical 狀態機(7)、
  新增 wo_hold_reason / stock_txn_kind 種子;歷史 status 映射 O→OPEN/H→CLOSED、downtime 由
  transform 算好(estimated)。前置:asset 已載入(FK)。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.inventory.models import StockTxnKind
from cmms.domain.work_order.models import WorkType
from cmms.domain.work_order.service import WorkOrderService
from cmms.domain.work_order.transform import WorkOrderImport, is_miscreated, row_to_import

MIGRATION_ACTOR = Actor.human("migration")

# canonical 狀態機種子(code, label, rank, is_terminal, is_downtime)。見 02-work-orders §3。
WO_STATUS_SEED: list[tuple[str, str, int, bool, bool]] = [
    ("OPEN", "Open(已開立,待處理)", 10, False, True),
    ("IN_PROGRESS", "In Progress(維修中)", 20, False, True),
    # ON_HOLD 預設 is_downtime=True;TEST_RUN(試跑)由 hold_reason 覆寫為 False
    ("ON_HOLD", "On Hold(暫停;試跑/等料)", 30, False, True),
    ("COMPLETED", "Completed(完工待結案)", 40, False, False),
    ("CLOSED", "Closed(已結案)", 90, True, False),
    ("CANCELLED", "Cancelled(報修取消)", 91, True, False),
    ("VOIDED", "Voided(誤開作廢)", 92, True, False),
]

# 暫停原因種子(code, label, is_downtime)。等零件=機台停產=計入 downtime(Jordan 2026-06-22);
# 等機台空檔 / 試跑觀察 = 機台運轉中,不計(Jordan 2026-07-03;migration 0019 對 prod 補種)。
WO_HOLD_REASON_SEED: list[tuple[str, str, bool]] = [
    ("TEST_RUN", "Test Run(試跑,機台運轉中)", False),
    ("WAITING_PARTS", "Waiting for Parts(等待零件)", True),
    ("WAITING_VENDOR", "Waiting for Vendor(等待承包商)", True),
    ("WAITING_MACHINE_TIME", "Waiting for Machine Window(等機台空檔,機台運轉中)", False),
    ("OTHER", "Other(其他,機台停產)", True),
]

# 庫存異動帳類別(#4b 引入;領料只用 ISSUE)。
STOCK_TXN_KIND_LABELS: dict[str, str] = {
    "ISSUE": "Issue(領用出庫)",
    "RETURN": "Return(退回入庫)",
    "ADJUST": "Adjust(盤點調整)",
    "RECEIVE": "Receive(採購入庫)",
}


@dataclass(frozen=True, slots=True)
class LoadResult:
    work_orders: int
    filtered_miscreated: int  # 因 miscreated=T 丟棄的列數
    work_types: int
    wo_statuses: int
    wo_hold_reasons: int
    stock_txn_kinds: int
    vendors: int


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 work_orders.csv(latin-1)。以表頭名對應欄位。"""
    with path.open(encoding="latin-1", newline="") as fh:
        return list(csv.DictReader(fh))


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    rows = list(rows)
    kept = [r for r in rows if not is_miscreated(r)]  # 誤開列丟棄(不匯入)
    filtered = len(rows) - len(kept)
    imports: list[WorkOrderImport] = [row_to_import(r) for r in kept]

    work_types = sorted({i.work_type for i in imports})
    vendors = sorted({i.assigned_vendor for i in imports if i.assigned_vendor})

    service = WorkOrderService(session)
    async with service.write(MIGRATION_ACTOR):
        for code in work_types:
            await service.upsert_lookup(WorkType, code, code)  # 照原樣(W4 重分類延後)
        for code, label, rank, is_terminal, is_downtime in WO_STATUS_SEED:
            await service.upsert_status(
                code, label, rank=rank, is_terminal=is_terminal, is_downtime=is_downtime
            )
        for code, label, is_downtime in WO_HOLD_REASON_SEED:
            await service.upsert_hold_reason(code, label, is_downtime=is_downtime)
        for code, label in STOCK_TXN_KIND_LABELS.items():
            await service.upsert_lookup(StockTxnKind, code, label)
        # vendor 沿用 pm_schedule 切片建的表;種子 WO 出現的 vendor(CMA/CMB 已存→no-op,SF 新增)
        from cmms.domain.pm_schedule.models import Vendor

        for code in vendors:
            await service.upsert_lookup(Vendor, code, code)
        for imp in imports:
            await service.upsert_work_order(imp, MIGRATION_ACTOR)

    return LoadResult(
        work_orders=len(imports),
        filtered_miscreated=filtered,
        work_types=len(work_types),
        wo_statuses=len(WO_STATUS_SEED),
        wo_hold_reasons=len(WO_HOLD_REASON_SEED),
        stock_txn_kinds=len(STOCK_TXN_KIND_LABELS),
        vendors=len(vendors),
    )
