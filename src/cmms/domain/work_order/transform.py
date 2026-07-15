"""work_orders.csv → 匯入資料的純函式(無 DB,可單元測試)。

CSV 為 **latin-1 + HTML-entity**(loader 以 latin-1 讀)。資料品質處理(02 §5):
- `brief_desc`/`diag` 中文為 `&#xxxxx;` → `html.unescape()` 還原。
- 日期 `MM/DD/YY`(2014–2026 → 20xx)。
- 時間 `time`/`time_cmpl` 為 §5.2 不可靠 12h(無 AM/PM)→ best-effort 解析。
- `assignto` = `VENDOR (Person)`。
- `miscreated=T`(誤開)→ **整列丟棄**(`is_miscreated`,loader 過濾;Jordan 2026-06-22)。

#4b 寫入切片新增:
- `status` 由 legacy `O/H` 映射到 canonical(`O`→OPEN、`H`→CLOSED;STATUS_MAP)。
- **downtime 引擎**(`ProductionCalendar.productive_minutes`):只計「安排生產」時段的停機。
  目前規則 = 每日扣除 00:00–09:00(非生產);未來改由真實生產排程源驅動(Jordan:之後談)。
  歷史工單以開/關日期時間一次算(estimated,因開單 time 是不可靠 12h);未來工單由
  service 依 status_history 的 down 區段累加(見 service `_recompute_downtime`)。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

_ASSIGNTO_RE = re.compile(r"^([A-Za-z0-9]+)\s*\((.*)\)\s*$")
_FULLWIDTH_COLON = "："  # ：(HTML entity &#65306; unescape 後)

# legacy workstatus → canonical 狀態(#4b)。歷史只有 O/H。
STATUS_MAP: dict[str, str] = {"O": "OPEN", "H": "CLOSED"}

# 廠區當地時區(Asia/Taipei = UTC+8,無 DST)。downtime 的生產時段以當地牆鐘時間判斷。
TAIPEI = timezone(timedelta(hours=8))

# 非生產時段(目前規則):每日 00:00–09:00 不安排生產 → downtime 不計入。
# ★ 暫定規則;未來由真實生產排程源取代(Jordan:生產排程源之後再談)。
NONPRODUCTION_DAILY_CUTOFF = time(9, 0)  # 09:00 之前為非生產
_CUTOFF_SECONDS = NONPRODUCTION_DAILY_CUTOFF.hour * 3600 + NONPRODUCTION_DAILY_CUTOFF.minute * 60


def _overlap_seconds(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    lo = max(a0, b0)
    hi = min(a1, b1)
    return max(0.0, (hi - lo).total_seconds())


def _nonproduction_seconds(start: datetime, end: datetime) -> float:
    """[start, end)(naive local)與每日非生產時段 [00:00, 09:00) 的總重疊秒數。

    以解析法處理內部整日(O(1)),不逐秒/逐日迭代,避免跨年工單拖慢。
    """
    day0 = datetime(start.year, start.month, start.day)
    day_end = datetime(end.year, end.month, end.day)
    span_days = (day_end - day0).days
    if span_days <= 1:
        total = 0.0
        for k in range(span_days + 1):
            blk = day0 + timedelta(days=k)
            total += _overlap_seconds(start, end, blk, blk + timedelta(seconds=_CUTOFF_SECONDS))
        return total
    # 首日 + 末日部分重疊 + 中間整日(各完整 9h 落在區間內)
    first = _overlap_seconds(start, end, day0, day0 + timedelta(seconds=_CUTOFF_SECONDS))
    last = _overlap_seconds(start, end, day_end, day_end + timedelta(seconds=_CUTOFF_SECONDS))
    interior = (span_days - 1) * _CUTOFF_SECONDS
    return first + last + interior


def productive_minutes(start: datetime, end: datetime) -> int:
    """[start, end)(naive local)中「安排生產」時段的分鐘數(四捨五入)。

    = 總時長 − 與每日非生產時段(00:00–09:00)的重疊。end<=start → 0。
    """
    if end <= start:
        return 0
    total = (end - start).total_seconds()
    productive = total - _nonproduction_seconds(start, end)
    return int(round(max(0.0, productive) / 60))


def to_taipei_naive(dt: datetime) -> datetime:
    """tz-aware datetime → 廠區當地(Taipei)naive 牆鐘時間(供 downtime 生產時段判斷)。"""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(TAIPEI).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class WorkOrderImport:
    """一筆待匯入工單(work_orders.csv 提供的欄位 + #4b 衍生)。"""

    work_order_no: int
    asset_id: str
    work_type: str
    status: str  # canonical(STATUS_MAP 後)
    brief_description: str | None
    diagnosis: str | None
    external_ref: str | None
    opened_date: date
    scheduled_date: date | None
    work_start_time: time | None
    work_complete_time: time | None
    closed_date: date | None
    closed_time: time | None
    closed_by: str | None
    assigned_vendor: str | None
    assigned_person: str | None
    # #4b 衍生:
    opened_at: datetime | None  # tz-aware;歷史由 opened_date + work_start_time 合成
    closed_at: datetime | None  # tz-aware;歷史由 closed_date + closed_time 合成
    downtime_minutes: int | None  # 歷史一次算(estimated);OPEN 單為 None
    downtime_estimated: bool  # 歷史 True(開單 time 不可靠);未來 service 精算為 False


def clean(value: str | None) -> str | None:
    """去空白;空字串 / `nan` 視為 None。"""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() == "nan":
        return None
    return v


def unescape_text(value: str | None) -> str | None:
    """HTML-entity 還原(中文 `&#xxxxx;`);空值→None。"""
    v = clean(value)
    return html.unescape(v) if v is not None else None


def is_miscreated(row: dict[str, str | None]) -> bool:
    """`miscreated=T` = 誤開 → 整列丟棄(Jordan 2026-06-22;loader 過濾)。"""
    v = clean(row.get("miscreated"))
    return v is not None and v.upper() == "T"


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


def parse_time(value: str | None) -> time | None:
    """時間 → SQL time(best-effort)。

    處理:全形冒號(`&#65306;`→`：`→`:`)、AM/PM 後綴、`H:MM:SS` / `H:MM`、單位數。
    ⚠ 12h 無 AM/PM 者面值解析(可能把下午當早上,§5.2)— 不可用於精確 downtime。
    無法解析→None(不讓零星髒值中斷整批載入)。
    """
    v = clean(value)
    if v is None:
        return None
    v = html.unescape(v).replace(_FULLWIDTH_COLON, ":").strip()
    for fmt in ("%I:%M:%S %p", "%I:%M %p", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(v, fmt).time()
        except ValueError:
            continue
    return None


def parse_assignto(value: str | None) -> tuple[str | None, str | None]:
    """`VENDOR (Person)` → (vendor, person)。空值→(None, None)。裸名→(None, 原值)。"""
    v = clean(value)
    if v is None:
        return (None, None)
    m = _ASSIGNTO_RE.match(v)
    if m:
        return (m.group(1), clean(m.group(2)))
    return (None, v)


def _historical_downtime(
    status: str,
    opened_date: date,
    work_start_time: time | None,
    closed_date: date | None,
    closed_time: time | None,
) -> tuple[datetime | None, datetime | None, int | None]:
    """歷史工單的 (opened_at, closed_at, downtime_minutes)。

    開單牆鐘 = opened_date + work_start_time(缺則用 09:00);關單 = closed_date + closed_time。
    downtime 只在已結案(CLOSED 且有關單日)時算,且僅計生產時段(扣 00:00–09:00)。estimated。
    """
    opened_naive = datetime.combine(opened_date, work_start_time or NONPRODUCTION_DAILY_CUTOFF)
    opened_at = opened_naive.replace(tzinfo=TAIPEI)
    if status != "CLOSED" or closed_date is None:
        return opened_at, None, None
    closed_naive = datetime.combine(closed_date, closed_time or NONPRODUCTION_DAILY_CUTOFF)
    closed_at = closed_naive.replace(tzinfo=TAIPEI)
    minutes = productive_minutes(opened_naive, closed_naive)  # 非正/倒序 → 0
    return opened_at, closed_at, minutes


def row_to_import(row: dict[str, str | None]) -> WorkOrderImport:
    """單列 CSV(dict,鍵為表頭)→ WorkOrderImport(已映射 status + 算好歷史 downtime)。

    注意:`miscreated=T` 的列由 loader 以 `is_miscreated` 事先過濾,不應到達此處。
    """
    wo = clean(row.get("wo"))
    if not wo:
        raise ValueError("row missing wo (work_order_no)")
    if not wo.isdigit():
        raise ValueError(f"wo not integer: {wo!r}")
    asset_id = clean(row.get("compid"))
    if not asset_id:
        raise ValueError(f"wo {wo}: missing compid (asset_id)")
    work_type = clean(row.get("wo_type"))
    if not work_type:
        raise ValueError(f"wo {wo}: missing wo_type")
    raw_status = clean(row.get("workstatus"))
    if not raw_status:
        raise ValueError(f"wo {wo}: missing workstatus")
    status = STATUS_MAP.get(raw_status, raw_status)  # 未知值原樣(防禦;歷史只有 O/H)
    opened_date = parse_date(row.get("date_wo"))
    if opened_date is None:
        raise ValueError(f"wo {wo}: missing date_wo (opened_date)")

    work_start_time = parse_time(row.get("time"))
    closed_date = parse_date(row.get("editdate"))
    closed_time = parse_time(row.get("edittime"))
    opened_at, closed_at, downtime = _historical_downtime(
        status, opened_date, work_start_time, closed_date, closed_time
    )

    vendor, person = parse_assignto(row.get("assignto"))
    return WorkOrderImport(
        work_order_no=int(wo),
        asset_id=asset_id,
        work_type=work_type,
        status=status,
        brief_description=unescape_text(row.get("brief_desc")),
        diagnosis=unescape_text(row.get("diag")),
        external_ref=clean(row.get("comments")),
        opened_date=opened_date,
        scheduled_date=parse_date(row.get("sch_date")),
        work_start_time=work_start_time,
        work_complete_time=parse_time(row.get("time_cmpl")),
        closed_date=closed_date,
        closed_time=closed_time,
        closed_by=clean(row.get("edituser")),
        assigned_vendor=vendor,
        assigned_person=person,
        opened_at=opened_at,
        closed_at=closed_at,
        downtime_minutes=downtime,
        downtime_estimated=True,  # 歷史一律 estimated(未來工單由 service 精算覆寫為 False)
    )
