"""efc 碼 × 維修單交叉比對種子產生器(下游分析平台的 ask;交付形狀由該平台釘死)。

設備軸實資料發現「efc 事件量 ≠ 故障」(單一溫度帶警報碼可在 60 天內噴數千次、機器全程
正常 = 雜訊碼)。分工:**cmms 出客觀種子**(維修 SoR,最有權威的客觀錨)、分析平台學
classifier、詞彙擁有者供碼 + 現場判斷。本模組把對方交付的 efc 事件 CSV 逐碼比對 cmms
REACTIVE 工單活躍窗,吐一份 governed-vocab 形狀的 JSON 種子(逐碼 provenance + golden 硬化)。

★★ 隱私紅線(消費端 requirement)—— 不可違反:
    交叉比對**只用工單的「存在 + 時間窗」欄位**:`work_order_no` / `asset_id` / `work_type` /
    `status` / `opened_at` / `closed_at`。**永不讀取、永不 emit 任何人員欄位**
    (`assigned_person` / `opened_by` / `closed_by` / `source_actor` … 一律不碰)。DB reader 的
    SELECT 只投影上述欄位;種子輸出無任何人員面。這是硬性設計約束,審計此檔即審此點。

比對規則:
- 事件「matched」= 其時戳落在該 EID **任一** REACTIVE 且 status ∉ {CANCELLED, VOIDED} 的工單
  活躍窗 [opened_at − buffer, (closed_at or as_of) + buffer] 內。
- buffer = 日級(預設 1 天,CLI 可調)。理由:歷史工單只有「日 + 預設時」粒度;故障常在開單前
  就被回報、關單後仍延續。**⇒ ratio 為指示性,非分鐘級精確**(granularity_note 誠實標註)。
- 時區:事件時戳為 naive 台北牆鐘 → 以 TAIPEI 落地再轉 UTC;工單 opened_at/closed_at 為 UTC
  aware。全程以 aware datetime 比較。
- 未知 EID:事件 EID **不在資產主檔** → 排除於 ratio 分母、逐碼計入 `n_events_unknown_eid`
  (誠實揭露)。EID 在主檔但零工單 → 算 unmatched(那**是**訊號:有故障碼卻從沒開修單)。
- OPEN 工單:窗尾 = as_of(執行當下;CLI `--as-of` 可覆寫以利重現)。

verdict_hint(詞彙來源方詞彙:real_fault / nuisance / insufficient_data)為**透明門檻的提示**,
**非權威裁決** —— 最終 verdict 由 C2 lookup 策展(詞彙權威分工不變)。門檻見 `VERDICT_RULES`。

離線 transform + thin async DB reader(唯讀,寫零 → 尊重單一寫入路徑護欄);無 migration/API/MCP。
schema_id = `efc_workorder_crosscheck_seed.v1`(golden fixture
`tests/fixtures/efc_workorder_crosscheck_seed.v1.json`;形狀漂移 → 契約測試紅;破壞性變更 bump v2)。
"""

from __future__ import annotations

import csv
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.domain.asset.models import Asset
from cmms.domain.work_order.models import WorkOrder
from cmms.domain.work_order.transform import TAIPEI

SCHEMA_ID = "efc_workorder_crosscheck_seed.v1"

# 事件時戳格式:naive 台北牆鐘,含 4 位小數秒(如 2026-06-02 13:40:44.0139)。
# %f 接受 1–6 位(右補到微秒:0139 → 013900µs),故此格式即可解析。
_EVENT_TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
_EVENT_TS_FMT_NOFRAC = "%Y-%m-%d %H:%M:%S"  # 防禦:偶有無小數秒的列

# REACTIVE 工單活躍窗**排除**的終態(取消/作廢 = 非真故障處理)。
# 其餘 7 態機的狀態(OPEN/IN_PROGRESS/ON_HOLD/COMPLETED/CLOSED)皆納入。
EXCLUDED_WO_STATUSES: tuple[str, ...] = ("CANCELLED", "VOIDED")

# provenance 上限(分析平台 spot-check 用;防禦性截斷 + truncated 旗標誠實揭露)。
MATCHED_WO_CAP = 20
EIDS_SEEN_CAP = 50

# verdict_hint 透明門檻(提示,非權威;最終 verdict 由 C2 lookup 策展)。
VERDICT_RULES: dict[str, str] = {
    "nuisance": "n_events_total_checked >= 100 and ratio <= 0.02",
    "real_fault": "n_events_matched >= 3 and ratio >= 0.30",
    "insufficient_data": "otherwise",
}


# ---- 純函式:事件解析 ----


@dataclass(frozen=True, slots=True)
class EfcEvent:
    """一筆 efc 事件(code + EID + UTC-aware 發生時刻)。"""

    code: str
    eid: str
    occurred_at: datetime  # UTC aware(讀入時由台北 naive 轉換)


@dataclass(frozen=True, slots=True)
class EfcEventsRead:
    """`read_efc_events` 結果:事件 + 誠實計入的壞列數(spec §6)。"""

    events: list[EfcEvent]
    n_rows_skipped: int


def _parse_event_ts(raw: str) -> datetime:
    """naive 台北牆鐘字串 → UTC aware。含/不含小數秒皆可;無法解析 → raise ValueError。"""
    for fmt in (_EVENT_TS_FMT, _EVENT_TS_FMT_NOFRAC):
        try:
            naive = datetime.strptime(raw.strip(), fmt)
            break
        except ValueError:
            continue
    else:
        raise ValueError(f"unrecognized event_timestamp: {raw!r}")
    return naive.replace(tzinfo=TAIPEI).astimezone(UTC)


def read_efc_events(path: Path) -> EfcEventsRead:
    """讀 efc 事件 CSV(UTF-8 **含 BOM** → utf-8-sig);表頭 `efc_code,eid,event_timestamp`。

    壞列(缺欄 / 時戳無法解析)**跳過並計數**(不因零星髒值中斷整批);計數回 `n_rows_skipped`。
    """
    events: list[EfcEvent] = []
    skipped = 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("efc_code") or "").strip()
            eid = (row.get("eid") or "").strip()
            ts = (row.get("event_timestamp") or "").strip()
            if not code or not eid or not ts:
                skipped += 1
                continue
            try:
                occurred_at = _parse_event_ts(ts)
            except ValueError:
                skipped += 1
                continue
            events.append(EfcEvent(code=code, eid=eid, occurred_at=occurred_at))
    return EfcEventsRead(events=events, n_rows_skipped=skipped)


# ---- 純函式:交叉比對 ----

# eid → 該 EID 的 REACTIVE 非取消/作廢工單窗:(wo_no, opened_at, closed_at|None)。
WoWindow = tuple[int, datetime, datetime | None]
WoWindows = Mapping[str, Sequence[WoWindow]]


@dataclass(frozen=True, slots=True)
class CodeStat:
    """單一 efc 碼的比對統計 + provenance(對映輸出 `codes[]` 一列)。"""

    code: str
    n_events_total_checked: int
    n_events_matched: int
    n_events_unknown_eid: int
    ratio: float
    verdict_hint: str
    eids_seen: list[str]
    eids_seen_truncated: bool
    event_first: datetime
    event_last: datetime
    matched_work_orders: list[int]
    matched_work_orders_truncated: bool


@dataclass(frozen=True, slots=True)
class SeedResult:
    """整份交叉比對結果(codes 已按 n_events_total_checked DESC 排序)。"""

    codes: list[CodeStat]
    buffer_days: int
    as_of: datetime
    events_by_code_order: list[str] = field(default_factory=list)


def _verdict_hint(n_total: int, n_matched: int, ratio: float) -> str:
    """透明門檻分類(nuisance / real_fault 互斥;皆不中 → insufficient_data)。

    以未捨入的 ratio 比較(避免捨入把邊界值誤判);ratio 捨入僅用於顯示。
    """
    if n_matched >= 3 and ratio >= 0.30:
        return "real_fault"
    if n_total >= 100 and ratio <= 0.02:
        return "nuisance"
    return "insufficient_data"


def _event_matches(occurred_at: datetime, windows: Sequence[WoWindow],
                   buffer: timedelta, as_of: datetime) -> list[int]:
    """回傳「窗涵蓋此事件」的工單號清單(可能多張;空 = unmatched)。"""
    hits: list[int] = []
    for wo_no, opened_at, closed_at in windows:
        start = opened_at - buffer
        end = (closed_at or as_of) + buffer
        if start <= occurred_at <= end:
            hits.append(wo_no)
    return hits


def crosscheck(events: Sequence[EfcEvent], wo_windows: WoWindows, known_eids: set[str],
               *, buffer: timedelta, as_of: datetime) -> SeedResult:
    """逐碼比對事件 × 工單窗 → SeedResult(DB-free,可單元測試)。

    - `wo_windows`:eid → REACTIVE 非取消/作廢工單窗清單(DB reader 已過濾人員欄、狀態、型別)。
    - `known_eids`:資產主檔已知 EID 集合(判「未知 EID」用;known 但零工單 → unmatched)。
    - `buffer`:日級窗緩衝;`as_of`:OPEN 工單窗尾。
    """
    buffer_days = round(buffer.total_seconds() / 86400)
    # 依碼分組,保面值順序穩定
    by_code: dict[str, list[EfcEvent]] = {}
    for ev in events:
        by_code.setdefault(ev.code, []).append(ev)

    stats: list[CodeStat] = []
    for code, code_events in by_code.items():
        n_total = 0  # 已知 EID 的事件(ratio 分母)
        n_matched = 0
        n_unknown = 0
        matched_wos: set[int] = set()
        eids: set[str] = set()
        first = min(ev.occurred_at for ev in code_events)
        last = max(ev.occurred_at for ev in code_events)
        for ev in code_events:
            eids.add(ev.eid)
            if ev.eid not in known_eids:
                n_unknown += 1
                continue
            n_total += 1
            hits = _event_matches(ev.occurred_at, wo_windows.get(ev.eid, ()), buffer, as_of)
            if hits:
                n_matched += 1
                matched_wos.update(hits)
        ratio = n_matched / n_total if n_total else 0.0
        eids_sorted = sorted(eids)
        wos_sorted = sorted(matched_wos)
        stats.append(
            CodeStat(
                code=code,
                n_events_total_checked=n_total,
                n_events_matched=n_matched,
                n_events_unknown_eid=n_unknown,
                ratio=round(ratio, 4),
                verdict_hint=_verdict_hint(n_total, n_matched, ratio),
                eids_seen=eids_sorted[:EIDS_SEEN_CAP],
                eids_seen_truncated=len(eids_sorted) > EIDS_SEEN_CAP,
                event_first=first,
                event_last=last,
                matched_work_orders=wos_sorted[:MATCHED_WO_CAP],
                matched_work_orders_truncated=len(wos_sorted) > MATCHED_WO_CAP,
            )
        )
    # 高量碼優先(n_events_total_checked DESC;同量以 code 昇冪求 deterministic)
    stats.sort(key=lambda s: (-s.n_events_total_checked, s.code))
    return SeedResult(codes=stats, buffer_days=buffer_days, as_of=as_of)


# ---- 序列化(golden;deterministic key order)----


def _iso_z(dt: datetime) -> str:
    """tz-aware datetime → UTC ISO 字串帶 `Z` 後綴(同契約 fixture 紀律)。"""
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_seed(result: SeedResult, *, events_file: str, generated_at: datetime,
               n_rows_skipped: int) -> dict:
    """SeedResult → 種子 JSON dict(固定 key 順序;`generated_at` 由呼叫端注入以利 deterministic)。"""
    return {
        "schema_id": SCHEMA_ID,
        "generated_at": _iso_z(generated_at),
        "method": {
            "events_file": events_file,
            "buffer_days": result.buffer_days,
            "as_of": _iso_z(result.as_of),
            "n_rows_skipped": n_rows_skipped,
            "wo_scope": (
                "REACTIVE, non-cancelled/non-void; §24: existence+time-window only, "
                "no personnel fields"
            ),
            "granularity_note": (
                "historical WOs carry day+default-time granularity; day-level buffer applied; "
                "ratios are indicative, not minute-precise"
            ),
            "verdict_rules": dict(VERDICT_RULES),
        },
        "codes": [
            {
                "code": s.code,
                "n_events_total_checked": s.n_events_total_checked,
                "n_events_matched": s.n_events_matched,
                "n_events_unknown_eid": s.n_events_unknown_eid,
                "ratio": s.ratio,
                "verdict_hint": s.verdict_hint,
                "evidence": {
                    "eids_seen": s.eids_seen,
                    "eids_seen_truncated": s.eids_seen_truncated,
                    "event_period": {
                        "first": _iso_z(s.event_first),
                        "last": _iso_z(s.event_last),
                    },
                    "matched_work_orders": s.matched_work_orders,
                    "matched_work_orders_truncated": s.matched_work_orders_truncated,
                },
            }
            for s in result.codes
        ],
    }


# ---- thin async DB reader(唯讀;§24:只投影存在 + 時間窗欄)----


async def fetch_wo_windows(
    session: AsyncSession, eids: set[str]
) -> tuple[dict[str, list[WoWindow]], set[str]]:
    """讀取比對所需的兩份唯讀資料(**寫零** → 尊重單一寫入路徑護欄):

    1. `wo_windows`:各 EID 的 REACTIVE 非取消/作廢工單窗 —— SELECT **只投影**
       work_order_no / asset_id / opened_at / closed_at(§24 紅線:不碰任何人員欄)。
       opened_at 為 None(無窗起點)的列略過。
    2. `known_eids`:上述 eids 中存在於資產主檔者(判「未知 EID」用)。
    """
    if not eids:
        return {}, set()

    known_stmt = select(Asset.asset_id).where(Asset.asset_id.in_(eids))
    known_eids = set((await session.scalars(known_stmt)).all())

    # §24:投影只含存在 + 時間窗欄(work_type/status 供過濾,不輸出)。
    wo_stmt = (
        select(
            WorkOrder.work_order_no,
            WorkOrder.asset_id,
            WorkOrder.opened_at,
            WorkOrder.closed_at,
        )
        .where(
            WorkOrder.work_type == "REACTIVE",
            WorkOrder.status.notin_(EXCLUDED_WO_STATUSES),
            WorkOrder.asset_id.in_(eids),
        )
    )
    windows: dict[str, list[WoWindow]] = {}
    for wo_no, asset_id, opened_at, closed_at in (await session.execute(wo_stmt)).all():
        if opened_at is None:
            continue  # 無窗起點,無法比對(防禦;歷史單 opened_at 由 loader 合成)
        windows.setdefault(asset_id, []).append((wo_no, opened_at, closed_at))
    return windows, known_eids


async def generate_seed(
    session: AsyncSession,
    events_read: EfcEventsRead,
    *,
    events_file: str,
    buffer_days: int,
    as_of: datetime,
    generated_at: datetime,
) -> dict:
    """端到端:DB reader → crosscheck → build_seed(CLI 與 DB 整合測試共用的 orchestrator)。"""
    eids = {ev.eid for ev in events_read.events}
    wo_windows, known_eids = await fetch_wo_windows(session, eids)
    result = crosscheck(
        events_read.events,
        wo_windows,
        known_eids,
        buffer=timedelta(days=buffer_days),
        as_of=as_of,
    )
    return build_seed(
        result,
        events_file=events_file,
        generated_at=generated_at,
        n_rows_skipped=events_read.n_rows_skipped,
    )
