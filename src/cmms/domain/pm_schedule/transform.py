"""scheduled_activity.csv → 匯入資料的純函式(無 DB,可單元測試)。

CSV 為 utf-8-sig。資料品質處理(03-scheduled-activity §5):
- 日期 `MM/DD/YY`(2 位年,實測 20–32 → 2020–2032,%y 對 <69 即 20xx,安全)。
- `assignto` = `VENDOR (Person)`(實測 706/706 乾淨)→ 拆 vendor + person。
- `pmfreqx=0` → 不週期(frequency_unit 為空)。
- `suppress` T/F → bool。
- 反正規化欄(line_no/comp_desc/task_desc/pm_type)不讀(§5.1 DROP)。
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

# assignto:`CMA (Lin Hsu)` / `CMB (...)`。vendor 為英數前綴,person 在括號內。
_ASSIGNTO_RE = re.compile(r"^([A-Za-z0-9]+)\s*\((.*)\)\s*$")


@dataclass(frozen=True, slots=True)
class PmScheduleImport:
    """一筆待匯入 PM 排程(scheduled_activity.csv 提供的欄位;[UI] 欄不在此)。"""

    pm_id: str
    asset_id: str
    task_id: str
    frequency_interval: int
    frequency_unit: str | None
    next_due_date: date | None
    last_pm_date: date | None
    last_work_order_no: int | None
    completion_window_days: Decimal | None
    standard_hours: Decimal | None
    estimated_labor_hours: Decimal | None
    assigned_vendor: str | None
    assigned_person: str | None
    is_suppressed: bool


def clean(value: str | None) -> str | None:
    """去空白;空字串視為 None。"""
    if value is None:
        return None
    v = value.strip()
    return v or None


def parse_date(value: str | None) -> date | None:
    """`MM/DD/YY`(主)或 `MM/DD/YYYY`(防禦)→ date;空值→None。"""
    v = clean(value)
    if v is None:
        return None
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date format: {value!r}")


def parse_decimal(value: str | None) -> Decimal | None:
    """十進位(含 `.00` / `.50` 等 leading-dot)→ Decimal;空值→None。"""
    v = clean(value)
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation as e:
        raise ValueError(f"bad decimal: {value!r}") from e


def parse_int(value: str | None) -> int | None:
    """整數(lastpmno)→ int;空值→None。"""
    v = clean(value)
    return int(v) if v is not None else None


def parse_interval(value: str | None) -> int:
    """週期數值(pmfreqx)→ int;空值→0(不週期)。"""
    v = clean(value)
    return int(v) if v is not None else 0


def parse_suppress(value: str | None) -> bool:
    """suppress 旗標:`T`→True,其餘(`F`/空)→False。"""
    return clean(value) == "T"


def parse_assignto(value: str | None) -> tuple[str | None, str | None]:
    """`VENDOR (Person)` → (vendor, person)。空值→(None, None)。

    不符格式(實測 0 筆):保守把原值放 person、不遺失資料,vendor 留空。
    """
    v = clean(value)
    if v is None:
        return (None, None)
    m = _ASSIGNTO_RE.match(v)
    if m:
        return (m.group(1), clean(m.group(2)))
    return (None, v)


def row_to_import(row: dict[str, str | None]) -> PmScheduleImport:
    """單列 CSV(dict,鍵為表頭)→ PmScheduleImport。"""
    pm_id = clean(row.get("pmid"))
    if not pm_id:
        raise ValueError("row missing pmid")
    asset_id = clean(row.get("compid"))
    if not asset_id:
        raise ValueError(f"{pm_id}: missing compid (asset_id)")
    task_id = clean(row.get("task_no"))
    if not task_id:
        raise ValueError(f"{pm_id}: missing task_no")

    vendor, person = parse_assignto(row.get("assignto"))
    return PmScheduleImport(
        pm_id=pm_id,
        asset_id=asset_id,
        task_id=task_id,
        frequency_interval=parse_interval(row.get("pmfreqx")),
        frequency_unit=clean(row.get("pmfreq")),
        next_due_date=parse_date(row.get("pmnextdate")),
        last_pm_date=parse_date(row.get("lastpmdate")),
        last_work_order_no=parse_int(row.get("lastpmno")),
        completion_window_days=parse_decimal(row.get("dayscmpl")),
        standard_hours=parse_decimal(row.get("standard")),
        estimated_labor_hours=parse_decimal(row.get("estlabor")),
        assigned_vendor=vendor,
        assigned_person=person,
        is_suppressed=parse_suppress(row.get("suppress")),
    )


# ---- PM 生成排程運算(ADR-021;純函式,供完成回寫推進 next_due_date)----


def effective_generation_date(due: date) -> date:
    """PM 生成的「有效到期日」——週末提前規則(純函式,ADR-021;03-scheduled-activity §3.2)。

    `next_due_date` 落在週六 → 視同其前的**週五**;週日 → 視同其前的**週五**(-2 天)。
    工作日(週一~週五)不變。目的:週末保養在週五就能被生成/補開,技師週間即可領到工單,
    避免週末沒人開單而延誤。

    ★ **只影響「生成/到期判定時機」**——**不改** `next_due_date` 本身,也**不影響** Fixed
    週期推進鏈(`_advance_pm_schedule` 仍以排定的 next_due_date 起算)。
    v1 **不處理國定假日**(需假日對照表;之後可在此擴充:落假日 → 再往前推到前一個工作日)。
    """
    wd = due.weekday()  # Mon=0 … Fri=4, Sat=5, Sun=6
    if wd == 5:  # 週六 → 前一天(週五)
        return due - timedelta(days=1)
    if wd == 6:  # 週日 → 前兩天(週五)
        return due - timedelta(days=2)
    return due


def add_interval(base: date, interval: int, unit: str) -> date:
    """把 `base` 日期推進 `interval` 個 `unit`(Days / Weeks / Months);純、確定性。

    - Days  → +interval 天;Weeks → +interval 週。
    - Months → 手算日曆月加總(年進位 + 月末 clamp):目標月天數不足時截到該月最後一天
      (如 Jan31 +1mo → Feb28/29、Dec +1mo → 次年 Jan)。不依賴第三方 relativedelta。

    供 PM 完成回寫(`_advance_pm_schedule`)計算下次到期日。`frequency_unit` 受控於
    freq_unit lookup(Months/Weeks/Days);未知值 → ValueError(不臆測語意,守護欄 #8)。
    """
    if unit == "Days":
        return base + timedelta(days=interval)
    if unit == "Weeks":
        return base + timedelta(weeks=interval)
    if unit == "Months":
        total = base.month - 1 + interval  # 以 0-based 月序加總後再進位
        year = base.year + total // 12
        month = total % 12 + 1
        day = min(base.day, calendar.monthrange(year, month)[1])  # clamp 至目標月末
        return date(year, month, day)
    raise ValueError(f"unknown frequency_unit: {unit!r}")
