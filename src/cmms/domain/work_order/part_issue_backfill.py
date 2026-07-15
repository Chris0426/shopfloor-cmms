"""part_issues.csv 歷史領料回填(transform + loader;migration 資料輸入切片)。

把 eMaint 匯出的 4602 筆歷史領料補進既有的 `work_order_part` + `stock_transaction`
(兩表皆 migration 0007 建立;本切片不建新表),經 `WorkOrderService.backfill_part_issue`
單一寫入路徑落地(ADR-001)。每列 → 一筆 `stock_transaction`(ISSUE,qty_delta<0)+
一筆 `work_order_part`(quantity>0),**不動 on_hand**(規則 ②:eMaint onhand snapshot
已反映這些歷史扣減,再扣會雙重扣帳),idempotent(每列 occurrence-based idempotency_key)。

★ 編碼:part_issues.csv 為 **cp1252**(含 em-dash 0x97 / ® 0xae / ° 0xb0 / µ 0xb5);
  utf-8 會解碼失敗。body 4602 列全 12 欄、無畸形行(descrip 內英吋符號為 HTML entity
  `&rdquo;` 而非裸引號 → 不需 RFC-4180 修復)。表頭尾逗號產生一個空名欄,忽略即可。
★ 反正規化欄(assetsubtp/comp_desc/vpartno/unitcost/extcost/category)一律 DROP
  (可經 WO→asset / inventory_item 取得);本切片不建 point-in-time 成本模型。
★ 預期 FK 落空為**正常現象**(非錯誤):指向 load-work-orders 丟棄的 miscreated WO、或
  不在 inventory_item 的 item → loader 計數 + 取樣 log、service 回 outcome enum,不中斷。
★ missing-wo 掛設備救援(ADR-024):WO 不存在但該列 compid 為有效 asset → 掛設備
  (`charge_target_asset_id`,INSERTED_ASSET),不再一律跳過;item 不在庫存主檔仍不可救
  (MISSING_ITEM)。`compid` 經 `expected_asset_id` 傳入 service 作退路。

複用 `cmms.domain.work_order.transform` 的 clean / unescape_text / parse_date / TAIPEI;
`parse_decimal` 本檔自帶(鏡射 inventory 版,避免跨域耦合)。
"""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.domain.inventory.models import StockTxnKind
from cmms.domain.work_order.service import BACKFILL_ACTOR, PartIssueOutcome, WorkOrderService
from cmms.domain.work_order.transform import TAIPEI, clean, parse_date, unescape_text

# 回填發起者:source_actor = "human:data-migration"(稽核 taxonomy 的 <name>=data-migration)。
# 單一真相在 work_order.service.BACKFILL_ACTOR —— cancel/update 領料以此值擋回填帳(防灌爆庫存)。
# 與 work_order loader 的 STOCK_TXN_KIND_LABELS["ISSUE"] 一致(upsert 不覆寫既有 label)。
_ISSUE_LABEL = "Issue(領用出庫)"


# ---- transform(純函式,無 DB,可單元測試)----


def parse_decimal(value: str | None) -> Decimal | None:
    """數值字串 → Decimal;空值→None。鏡射 inventory.transform.parse_decimal(避免跨域耦合)。"""
    v = clean(value)
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation as e:
        raise ValueError(f"bad decimal: {value!r}") from e


@dataclass(frozen=True, slots=True)
class PartIssueImport:
    """一筆待回填的歷史領料(part_issues.csv 的保留欄 → 目標 schema)。"""

    work_order_no: int
    item_code: str
    quantity: Decimal
    occurred_at: datetime  # date_wo @ Taipei 00:00(來源僅 DATE 精度,無 time-of-day)
    reason: str | None  # descrip 經 html.unescape(provenance;D1)
    expected_asset_id: str | None  # compid,僅供交叉檢核,不持久化


def row_to_import(row: dict[str, str | None]) -> PartIssueImport:
    """單列 CSV(dict,鍵為表頭)→ PartIssueImport。缺關鍵欄 → raise(loader 計 malformed)。"""
    wo = clean(row.get("wo"))
    item = clean(row.get("item"))
    if not wo or not item:
        raise ValueError("part_issue row missing wo/item")
    if not wo.isdigit():
        raise ValueError(f"part_issue wo not integer: {wo!r}")
    qty = parse_decimal(row.get("qty"))
    d = parse_date(row.get("date_wo"))
    if qty is None or d is None:
        raise ValueError(f"part_issue {wo}/{item} missing qty/date_wo")
    return PartIssueImport(
        work_order_no=int(wo),
        item_code=item,  # 來源已大寫,不再轉
        quantity=qty,
        occurred_at=datetime(d.year, d.month, d.day, tzinfo=TAIPEI),
        reason=unescape_text(row.get("descrip")),  # &rdquo;→" / &#956;→µ
        expected_asset_id=clean(row.get("compid")),
    )


def make_idempotency_key(work_order_no: int, item_code: str, occurrence: int) -> str:
    """occurrence-based 冪等鍵:同 (wo,item) 第 n 次出現得獨立鍵 → 重複列各成一筆、重跑仍冪等。

    假設 part_issues.csv 為**凍結檔**(列序穩定);若日後重匯出致列序變動,須重新核對。
    """
    return f"partissue:v1:{work_order_no}:{item_code}:{occurrence}"


# ---- loader(migration 資料輸入;單一 write() 交易、每列冪等)----


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 part_issues.csv(cp1252)。表頭名對欄;無畸形行,DictReader 直讀。"""
    with path.open(encoding="cp1252", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass(frozen=True, slots=True)
class LoadResult:
    rows_read: int
    inserted: int  # 掛工單
    rescued_to_asset: int  # 掛設備救援(missing-wo + 有效 compid,ADR-024)
    duplicates_skipped: int
    missing_wo_skipped: int  # WO 不存在且無有效 compid → 不可救
    missing_item_skipped: int
    malformed_skipped: int
    missing_wo_samples: list[str] = field(default_factory=list)  # 取樣供 CLI echo
    missing_item_samples: list[str] = field(default_factory=list)
    rescued_asset_samples: list[str] = field(default_factory=list)  # "wo->EID" 取樣


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    """回填歷史領料(idempotent)。前置:assets + inventory + work_orders 資料已載入(FK)。

    單一 write() 交易包整批(比照 inventory/work_order loader)。每列 → backfill_part_issue:
    INSERTED / DUPLICATE(冪等命中)/ MISSING_WORK_ORDER / MISSING_ITEM(FK 落空 log+skip,
    不中斷)。重複 (wo,item) 列依**檔案順序**計次 → occurrence-based key,各成一筆、重跑不重複。
    """
    rows = list(rows)
    service = WorkOrderService(session)
    seen: Counter[tuple[int, str]] = Counter()
    tally: dict[PartIssueOutcome, int] = dict.fromkeys(PartIssueOutcome, 0)
    malformed = 0
    miss_wo_samples: list[str] = []
    miss_item_samples: list[str] = []
    rescued_samples: list[str] = []
    async with service.write(BACKFILL_ACTOR):
        # 防禦性 idempotent seed:回填不必依賴 load-work-orders 先種 stock_txn_kind。
        await service.upsert_lookup(StockTxnKind, "ISSUE", _ISSUE_LABEL)
        for raw in rows:
            try:
                imp = row_to_import(raw)
            except ValueError:
                malformed += 1
                continue
            seen[(imp.work_order_no, imp.item_code)] += 1
            key = make_idempotency_key(
                imp.work_order_no, imp.item_code, seen[(imp.work_order_no, imp.item_code)]
            )
            outcome = await service.backfill_part_issue(
                work_order_no=imp.work_order_no,
                item_code=imp.item_code,
                quantity=imp.quantity,
                occurred_at=imp.occurred_at,
                reason=imp.reason,
                actor=BACKFILL_ACTOR,
                idempotency_key=key,
                asset_id=imp.expected_asset_id,  # compid:missing-wo 掛設備救援退路(ADR-024)
            )
            tally[outcome] += 1
            if outcome is PartIssueOutcome.MISSING_WORK_ORDER and len(miss_wo_samples) < 20:
                miss_wo_samples.append(str(imp.work_order_no))
            elif outcome is PartIssueOutcome.MISSING_ITEM and len(miss_item_samples) < 20:
                miss_item_samples.append(imp.item_code)
            elif outcome is PartIssueOutcome.INSERTED_ASSET and len(rescued_samples) < 20:
                rescued_samples.append(f"{imp.work_order_no}->{imp.expected_asset_id}")
    return LoadResult(
        rows_read=len(rows),
        inserted=tally[PartIssueOutcome.INSERTED],
        rescued_to_asset=tally[PartIssueOutcome.INSERTED_ASSET],
        duplicates_skipped=tally[PartIssueOutcome.DUPLICATE],
        missing_wo_skipped=tally[PartIssueOutcome.MISSING_WORK_ORDER],
        missing_item_skipped=tally[PartIssueOutcome.MISSING_ITEM],
        malformed_skipped=malformed,
        missing_wo_samples=miss_wo_samples,
        missing_item_samples=miss_item_samples,
        rescued_asset_samples=rescued_samples,
    )
