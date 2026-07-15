"""匯出中心的欄位註冊表 + 過濾器規格 + CSV 渲染(web 層,ADR-019 thin client)。

- `ColumnSpec` / `DatasetSpec` / `FilterSpec`:五個資料集的欄位、過濾器、標題的靜態註冊表。
- **欄位級 RBAC**:`ColumnSpec.admin_only=True` 的欄,非 admin 完全不出現在 CSV 與預覽
  (`visible_columns` 依角色過濾)。**目前沒有欄標 admin_only**(本範圍五類資料 engineer 全欄可讀,
  PII 只在 contacts、不在此),但機制已就位 —— 日後新增敏感欄只要標 `admin_only=True` 即自動生效。
- CSV:utf-8-sig(BOM,Excel 直開中文不亂碼)+ csv module 預設 `\r\n` 行尾;欄頭依登入者 locale 譯。
  **formula-injection 防護**:字串值以 `= + - @` 開頭 → 前綴 `'`(只對原始字串型別,不動日期/數字)。
- 過濾器解析:每個資料集一個 `parse(QueryParams) -> dict`(壞日期 → `ExportFilterError`,web 轉
  誠實 banner,不 500)。dict 鍵即 `ExportService.count_*/rows_*` 的關鍵字參數。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from starlette.datastructures import QueryParams

from cmms.domain.work_order.transform import TAIPEI, to_taipei_naive
from cmms.web.i18n import translate


class ExportFilterError(ValueError):
    """過濾器輸入非法(如壞日期)。web 層轉誠實 banner,不 500。"""


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """一個匯出欄位。`key` 對應 `ExportService` 列 dict 的鍵;`label_key` 為 i18n 欄頭。"""

    key: str
    admin_only: bool = False

    @property
    def label_key(self) -> str:
        return f"export.col.{self.key}"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """一個過濾器欄位(渲染 + 解析用)。

    kind:`text`(自由文字)/`suggest`(帶 data-suggest 自動完成)/`date`(HTML date)/
    `chips`(多選 checkbox,值可重複 → in-clause)/`tristate`(全部 / 是 / 否 下拉)。
    `options_source` = chips 的動態選項來源(受控 lookup:work_type/asset_type/frequency_unit);
    `options` = 靜態 (value, label_key)(status chips / tristate)。
    """

    key: str
    label_key: str
    kind: str
    suggest: str | None = None
    options_source: str | None = None
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    slug: str
    title_key: str
    desc_key: str
    columns: tuple[ColumnSpec, ...]
    filters: tuple[FilterSpec, ...]
    parse: Callable[[QueryParams], dict]


# ---- 過濾器解析 helpers ----

def _s(qp: QueryParams, key: str) -> str | None:
    v = (qp.get(key) or "").strip()
    return v or None


def _up(qp: QueryParams, key: str) -> str | None:
    v = _s(qp, key)
    return v.upper() if v else None


def _list(qp: QueryParams, key: str) -> list[str] | None:
    vals = [v.strip() for v in qp.getlist(key) if v and v.strip()]
    return vals or None


def _date(qp: QueryParams, key: str) -> date | None:
    v = _s(qp, key)
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError as e:
        raise ExportFilterError(key) from e


def _int(qp: QueryParams, key: str) -> int | None:
    v = _s(qp, key)
    if not v:
        return None
    try:
        return int(v)
    except ValueError as e:
        raise ExportFilterError(key) from e


def _tristate(qp: QueryParams, key: str) -> bool | None:
    """'1' → True、'0' → False、其餘(''/'all')→ None(不過濾)。"""
    v = _s(qp, key)
    if v == "1":
        return True
    if v == "0":
        return False
    return None


# ---- 各資料集過濾器解析 ----

def _parse_work_orders(qp: QueryParams) -> dict:
    return {
        "statuses": _list(qp, "status"),
        "work_types": _list(qp, "work_type"),
        "opened_from": _date(qp, "opened_from"),
        "opened_to": _date(qp, "opened_to"),
        "closed_from": _date(qp, "closed_from"),
        "closed_to": _date(qp, "closed_to"),
        "asset_id": _up(qp, "asset_id"),
        "assigned_person": _s(qp, "assigned_person"),
        "assigned_vendor": _s(qp, "assigned_vendor"),
    }


def _issue_type(qp: QueryParams) -> str | None:
    """領料型別下拉:'work_order' / 'direct' 才過濾;其餘(''/未知)→ None(全部)。"""
    v = _s(qp, "issue_type")
    return v if v in ("work_order", "direct") else None


def _parse_part_usage(qp: QueryParams) -> dict:
    return {
        "issued_from": _date(qp, "issued_from"),
        "issued_to": _date(qp, "issued_to"),
        "item_code": _up(qp, "item_code"),
        "asset_id": _up(qp, "asset_id"),
        "work_order_no": _int(qp, "work_order_no"),
        "issue_type": _issue_type(qp),
    }


def _parse_assets(qp: QueryParams) -> dict:
    return {
        "asset_types": _list(qp, "asset_type"),
        "department": _s(qp, "department"),
        "line": _s(qp, "line"),
        "is_active": _tristate(qp, "is_active"),
    }


def _parse_pm_schedules(qp: QueryParams) -> dict:
    return {
        "asset_id": _up(qp, "asset_id"),
        "assigned_vendor": _s(qp, "assigned_vendor"),
        "assigned_person": _s(qp, "assigned_person"),
        "frequency_units": _list(qp, "frequency_unit"),
        "due_from": _date(qp, "due_from"),
        "due_to": _date(qp, "due_to"),
        "is_suppressed": _tristate(qp, "is_suppressed"),
    }


def _parse_pm_task_details(qp: QueryParams) -> dict:
    return {
        "asset_id": _up(qp, "asset_id"),
        "task_no": _up(qp, "task_no"),
        "task_desc": _s(qp, "task_desc"),
    }


# ---- 靜態選項 ----

# 工單狀態 chips(canonical 7;legacy O/H 匯出資料仍在,空選 = 全部不過濾即涵蓋)
_WO_STATUS_OPTS: tuple[tuple[str, str], ...] = (
    ("OPEN", "status.OPEN"),
    ("IN_PROGRESS", "status.IN_PROGRESS"),
    ("ON_HOLD", "status.ON_HOLD"),
    ("COMPLETED", "status.COMPLETED"),
    ("CLOSED", "status.CLOSED"),
    ("CANCELLED", "status.CANCELLED"),
    ("VOIDED", "status.VOIDED"),
)
_ACTIVE_OPTS: tuple[tuple[str, str], ...] = (
    ("", "export.opt.all"),
    ("1", "export.opt.active"),
    ("0", "export.opt.inactive"),
)
_SUPPRESSED_OPTS: tuple[tuple[str, str], ...] = (
    ("", "export.opt.all"),
    ("0", "export.opt.not_suppressed"),
    ("1", "export.opt.suppressed_only"),
)
# 領料型別下拉(靜態 options,比照 tristate 的 select 渲染機制;值為穩定英文 token)
_ISSUE_TYPE_OPTS: tuple[tuple[str, str], ...] = (
    ("", "export.opt.all"),
    ("work_order", "export.opt.issue_work_order"),
    ("direct", "export.opt.issue_direct"),
)


def _col(key: str, admin_only: bool = False) -> ColumnSpec:
    return ColumnSpec(key=key, admin_only=admin_only)


DATASETS: dict[str, DatasetSpec] = {
    "work_orders": DatasetSpec(
        slug="work_orders",
        title_key="export.card.work_orders.title",
        desc_key="export.card.work_orders.desc",
        columns=(
            _col("work_order_no"), _col("asset_id"), _col("asset_description"),
            _col("work_type"), _col("status"), _col("brief_description"),
            _col("diagnosis"), _col("assigned_person"), _col("assigned_vendor"),
            _col("priority"), _col("external_ref"), _col("opened_date"),
            _col("scheduled_date"), _col("closed_date"), _col("opened_at"),
            _col("closed_at"), _col("downtime_minutes"), _col("downtime_estimated"),
            _col("hold_reason"), _col("labor_hours"), _col("cost"), _col("action_taken"),
        ),
        filters=(
            FilterSpec("status", "export.filter.status", "chips", options=_WO_STATUS_OPTS),
            FilterSpec("work_type", "export.filter.work_type", "chips",
                       options_source="work_type"),
            FilterSpec("opened_from", "export.filter.opened_from", "date"),
            FilterSpec("opened_to", "export.filter.opened_to", "date"),
            FilterSpec("closed_from", "export.filter.closed_from", "date"),
            FilterSpec("closed_to", "export.filter.closed_to", "date"),
            FilterSpec("asset_id", "export.filter.asset_id", "suggest", suggest="asset"),
            FilterSpec("assigned_person", "export.filter.assigned_person", "suggest",
                       suggest="person"),
            FilterSpec("assigned_vendor", "export.filter.assigned_vendor", "text"),
        ),
        parse=_parse_work_orders,
    ),
    "part_usage": DatasetSpec(
        slug="part_usage",
        title_key="export.card.part_usage.title",
        desc_key="export.card.part_usage.desc",
        columns=(
            _col("issued_at"), _col("work_order_no"), _col("asset_id"),
            _col("issue_type"), _col("item_code"), _col("item_name"), _col("quantity"),
        ),
        filters=(
            FilterSpec("issued_from", "export.filter.issued_from", "date"),
            FilterSpec("issued_to", "export.filter.issued_to", "date"),
            FilterSpec("item_code", "export.filter.item_code", "suggest", suggest="part"),
            FilterSpec("asset_id", "export.filter.asset_id", "suggest", suggest="asset"),
            FilterSpec("work_order_no", "export.filter.work_order_no", "text"),
            FilterSpec("issue_type", "export.filter.issue_type", "tristate",
                       options=_ISSUE_TYPE_OPTS),
        ),
        parse=_parse_part_usage,
    ),
    "assets": DatasetSpec(
        slug="assets",
        title_key="export.card.assets.title",
        desc_key="export.card.assets.desc",
        columns=(
            _col("asset_id"), _col("description"), _col("asset_type"),
            _col("asset_subtype"), _col("department"), _col("line"), _col("site"),
            _col("parent_asset_id"), _col("manufacturer"), _col("model_no"),
            _col("serial_no"), _col("owner"), _col("is_active"),
            _col("available_for_service"),
        ),
        filters=(
            FilterSpec("asset_type", "export.filter.asset_type", "chips",
                       options_source="asset_type"),
            FilterSpec("department", "export.filter.department", "text"),
            FilterSpec("line", "export.filter.line", "text"),
            FilterSpec("is_active", "export.filter.is_active", "tristate",
                       options=_ACTIVE_OPTS),
        ),
        parse=_parse_assets,
    ),
    "pm_schedules": DatasetSpec(
        slug="pm_schedules",
        title_key="export.card.pm_schedules.title",
        desc_key="export.card.pm_schedules.desc",
        columns=(
            _col("pm_id"), _col("asset_id"), _col("task_id"), _col("task_name"),
            _col("frequency_interval"), _col("frequency_unit"), _col("calendar_freq_type"),
            _col("next_due_date"), _col("last_pm_date"), _col("last_work_order_no"),
            _col("completion_window_days"), _col("standard_hours"),
            _col("estimated_labor_hours"), _col("assigned_vendor"),
            _col("assigned_person"), _col("is_suppressed"),
        ),
        filters=(
            FilterSpec("asset_id", "export.filter.asset_id", "suggest", suggest="asset"),
            FilterSpec("assigned_person", "export.filter.assigned_person", "suggest",
                       suggest="person"),
            FilterSpec("assigned_vendor", "export.filter.assigned_vendor", "text"),
            FilterSpec("frequency_unit", "export.filter.frequency_unit", "chips",
                       options_source="frequency_unit"),
            FilterSpec("due_from", "export.filter.due_from", "date"),
            FilterSpec("due_to", "export.filter.due_to", "date"),
            FilterSpec("is_suppressed", "export.filter.is_suppressed", "tristate",
                       options=_SUPPRESSED_OPTS),
        ),
        parse=_parse_pm_schedules,
    ),
    "pm_task_details": DatasetSpec(
        slug="pm_task_details",
        title_key="export.card.pm_task_details.title",
        desc_key="export.card.pm_task_details.desc",
        columns=(
            _col("pm_id"), _col("asset_id"), _col("task_no"), _col("task_name"),
            _col("step_no"), _col("step_desc"), _col("item_code"), _col("item_name"),
            _col("replace_qty"),
        ),
        filters=(
            FilterSpec("asset_id", "export.filter.asset_id", "suggest", suggest="asset"),
            FilterSpec("task_no", "export.filter.task_no", "suggest", suggest="task"),
            FilterSpec("task_desc", "export.filter.task_desc", "text"),
        ),
        parse=_parse_pm_task_details,
    ),
}


def visible_columns(spec: DatasetSpec, is_admin: bool) -> list[ColumnSpec]:
    """依角色過濾欄位:非 admin 不見 `admin_only` 欄(CSV 與預覽皆據此,單一真相)。"""
    return [c for c in spec.columns if is_admin or not c.admin_only]


# ---- 值格式化 ----

def _fmt(v: object) -> str:
    """一致的顯示字串:None→空、bool→true/false、datetime→台北 YYYY-MM-DD HH:MM、
    date→ISO、Decimal→str、其餘→str。datetime 須在 date 前判(datetime 是 date 子類)。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return to_taipei_naive(v).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return str(v)
    return str(v)


def display_cell(v: object) -> str:
    """預覽(HTML)用:純格式化,不做 formula guard(Jinja 已 autoescape)。"""
    return _fmt(v)


def _csv_cell(v: object) -> str:
    """CSV 用:格式化 + 只對原始字串型別做 formula-injection 前綴(不動日期/數字/布林)。"""
    s = _fmt(v)
    if isinstance(v, str) and s and s[0] in ("=", "+", "-", "@"):
        return "'" + s
    return s


def csv_filename(slug: str) -> str:
    """cmms-<slug>-<台北 YYYYMMDD-HHMM>.csv。"""
    return f"cmms-{slug}-{datetime.now(TAIPEI).strftime('%Y%m%d-%H%M')}.csv"


def stream_csv(
    columns: list[ColumnSpec], rows: list[dict], locale: str
) -> Iterator[str]:
    """逐列產出 CSV 文字(欄頭 + 資料列)。第一塊前置 BOM(utf-8-sig,Excel 直開中文不亂碼)。

    `rows` 已由呼叫端一次撈齊(21.5k 列 OK,spec 允許全進記憶體再分塊 yield)。
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([translate(c.label_key, locale) for c in columns])
    yield "﻿" + buf.getvalue()  # BOM 前綴(utf-8-sig,Excel 直開中文不亂碼)
    buf.seek(0)
    buf.truncate(0)
    for row in rows:
        writer.writerow([_csv_cell(row.get(c.key)) for c in columns])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
