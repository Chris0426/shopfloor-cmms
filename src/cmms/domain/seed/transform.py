"""種子重播的純函式(無 DB):cut-off 解析、area→line 對照、actor 對照、逐事件預檢。

`Replayer` 是 dry-run 與實跑**共用**的預檢器:對每個事件產生一個 `Plan`(要呼叫哪個 domain
操作 + 參數 + 行級錯誤 / 跳過計數);實跑由 `SeedReplayService` 執行 plan、**成功後**才
`confirm()` 推進模擬狀態,dry-run 則一律視為成功。兩條路徑的預檢規則因此逐字相同。

預檢只判「本 instance 能否無猜測地表達這個事件」(主檔存在、時序約束、閉集);授權 / 狀態機
等由 domain service 自己把關(實跑時的錯誤另記)。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from cmms.domain.asset.transform import CANONICAL_LINES
from cmms.domain.seed.reader import HOLD_REASONS, SeedEvent, parse_date, parse_utc_z
from cmms.domain.work_order.downtime import segment_is_downtime
from cmms.domain.work_order.transform import TAIPEI

# ---- area → line(決策 1;可由 --area-map / CMMS_SEED_AREA_MAP 覆寫)----
DEFAULT_AREA_MAP: dict[str, str] = {
    "za": "Zone A", "zb": "Zone B", "zc": "Zone C", "zd": "Zone D",
}

# ---- seed 角色 → 合成帳號(決策 4)。(username, cmms role)----
ACTOR_MAP: dict[str, tuple[str, str]] = {
    "role:operator": ("synth.operator", "operator"),
    "role:technician": ("synth.technician", "engineer"),
    "role:engineer": ("synth.engineer", "engineer"),
    "role:planner": ("synth.planner", "admin"),
    "role:system": ("synth.system", "admin"),
}

SKIPPED_KINDS = ("asset.schedule_state", "pm.schedule")  # 決策 10:跳過、另計
SEED_FIELDS = ("diagnosis", "action_taken", "production_resumed_at", "scheduled_date",
               "downtime_estimated")
TERMINAL = frozenset({"CLOSED", "CANCELLED", "VOIDED"})
ON_HAND_SEED = 20  # 決策 3:每個 inventory.item 期初在庫量(常數,非契約欄位)


class SeedConfigError(ValueError):
    """CLI 參數 / 環境設定不合法(拒讀前置;code 供 CLI 輸出)。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def parse_until(value: str | None, *, now: datetime | None = None) -> datetime:
    """`--until` → tz-aware UTC。ISO-8601;naive 視為 Asia/Taipei;`Z`/offset 照用;省略 = now。"""
    if value is None or not value.strip():
        return (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as e:
        raise SeedConfigError("bad_until", f"--until {value!r} is not ISO-8601") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt.astimezone(UTC)


def to_utc_z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_area_map(cli_json: str | None, env_json: str | None) -> dict[str, str]:
    """area map:CLI > env > 預設。值必須 ∈ `CANONICAL_LINES`(不鑄新產線)。"""
    raw = cli_json if cli_json else env_json
    if not raw:
        return dict(DEFAULT_AREA_MAP)
    try:
        m = json.loads(raw)
    except ValueError as e:
        raise SeedConfigError("bad_area_map", f"area map is not JSON ({e})") from e
    if not (isinstance(m, dict) and all(isinstance(k, str) and isinstance(v, str)
                                        for k, v in m.items())):
        raise SeedConfigError("bad_area_map", "area map must be a JSON object of str -> str")
    bad = sorted({v for v in m.values() if v not in CANONICAL_LINES})
    if bad:
        raise SeedConfigError("bad_area_map", f"line values not in CANONICAL_LINES: {bad}")
    return dict(m)


# ===========================================================================
# 預檢
# ===========================================================================


@dataclass(slots=True)
class WoState:
    work_type: str
    asset_id: str
    status: str
    opened_at: datetime
    first_start: datetime | None = None  # 首次進 IN_PROGRESS 的時刻(pra 下界)


@dataclass(slots=True)
class Plan:
    """一個事件的處置。`op` 為 None = 不呼叫任何 domain 操作(跳過或丟棄)。

    `errors` = 行級錯誤 [(code, detail)](有 op 時為「照做但記錯」,如 fsa_after_open);
    `warnings` 不計入 line_errors;`features` = 被忽略的契約欄位計數鍵;`skipped_kind` = 整個 kind
    跳過。"""

    event: SeedEvent
    op: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    errors: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    skipped_kind: str | None = None


@dataclass(slots=True)
class LinkPlan:
    """asset.define 的 parent → containment(批次內全部 define 之後才連)。"""

    seq: int
    line: int
    parent: str
    child: str
    error: tuple[str, str] | None = None


@dataclass(slots=True)
class Tally:
    """一次重播的計數(輸出 JSON 的來源)。"""

    events_applied: int = 0
    skipped_kinds: Counter = field(default_factory=Counter)
    skipped_features: Counter = field(default_factory=Counter)
    errors_by_code: Counter = field(default_factory=Counter)
    warnings: Counter = field(default_factory=Counter)
    line_errors: int = 0
    records: list[dict[str, Any]] = field(default_factory=list)  # {code, seq, line, detail}

    def error(self, code: str, seq: int | None, line: int | None, detail: str) -> None:
        self.errors_by_code[code] += 1
        self.line_errors += 1
        self.records.append({"code": code, "seq": seq, "line": line, "detail": detail})


class Replayer:
    """逐事件預檢器(純)。`plan()` 不改狀態;`confirm()` 在 plan 成功落地後推進狀態。"""

    def __init__(
        self,
        area_map: Mapping[str, str],
        *,
        downtime_maps: tuple[Mapping[str, bool], Mapping[str, bool]] | None = None,
    ) -> None:
        self.area_map = dict(area_map)
        self.downtime_maps = downtime_maps
        self.assets: set[str] = set()
        self.items: set[str] = set()
        self.wos: dict[int, WoState] = {}
        self._pending_links: list[LinkPlan] = []

    # ---- 狀態推進 ----

    def confirm(self, plan: Plan) -> None:
        ev, op, a = plan.event, plan.op, plan.args
        if op == "asset.create":
            self.assets.add(a["asset_id"])
            if a.get("parent"):
                self._pending_links.append(LinkPlan(ev.seq, ev.line, a["parent"], a["asset_id"]))
        elif op == "item.create":
            self.items.add(a["item_code"])
        elif op == "wo.open":
            self.wos[a["work_order_no"]] = WoState(
                work_type=a["work_type"], asset_id=a["asset_id"], status="OPEN", opened_at=ev.at
            )
        elif op in _STATUS_OPS:
            wo = self.wos[a["work_order_no"]]
            wo.status = _STATUS_OPS[op]
            if wo.status == "IN_PROGRESS" and wo.first_start is None:
                wo.first_start = ev.at

    def take_links(self) -> list[LinkPlan]:
        """取出待連的 containment(呼叫端在遇到第一個非 define 事件 / 結束時呼叫)。"""
        out, self._pending_links = self._pending_links, []
        for lk in out:
            if lk.parent not in self.assets:
                lk.error = ("unknown_asset", f"parent {lk.parent} of {lk.child} not in master")
        return out

    # ---- 預檢 ----

    def plan(self, ev: SeedEvent) -> Plan:
        p = Plan(event=ev)
        if ev.status != "ok":
            p.skipped_kind = ev.kind
            return p
        if ev.kind in SKIPPED_KINDS:
            p.skipped_kind = ev.kind
            return p
        handler = getattr(self, "_plan_" + ev.kind.replace(".", "_"))
        handler(ev, p)
        return p

    def _plan_asset_define(self, ev: SeedEvent, p: Plan) -> None:
        d = ev.data
        for r in d["roles"]:
            p.features.append(f"asset.define.roles:{r}")
        if d["seats"]:
            p.features.append("asset.define.seats")
        if ev.asset_id in self.assets:
            p.errors.append(("asset_redefined", f"asset {ev.asset_id} defined twice"))
            return
        area = d["area"]
        if area is None or area not in self.area_map:
            p.errors.append(("unknown_area", f"area {area!r} not in area map"))
            return
        if d["station_cls"] is None:
            # asset_type 必填;契約 null = 無站別(治具/工作台),不猜一個型別(護欄 #7)
            p.errors.append(("station_cls_missing", f"asset {ev.asset_id} has null station_cls"))
            return
        p.op = "asset.create"
        p.args = {
            "asset_id": ev.asset_id,
            "description": d["display_name"],
            "asset_type": d["station_cls"],
            "line": self.area_map[area],
            "parent": d["parent_asset_id"],
        }

    def _plan_inventory_item(self, ev: SeedEvent, p: Plan) -> None:
        code = ev.data["item_code"]
        if code in self.items:
            p.errors.append(("item_redefined", f"item {code} defined twice"))
            return
        p.op = "item.create"
        p.args = {"item_code": code, "name": ev.data["display_name"], "on_hand": ON_HAND_SEED}

    def _wo(self, ev: SeedEvent, p: Plan) -> WoState | None:
        """非 open 的 wo.* 共用前置:工單須已開、asset 一致。"""
        no = ev.data["work_order_no"]
        wo = self.wos.get(no)
        if wo is None:
            if ev.asset_id not in self.assets:
                p.errors.append(("unknown_asset", f"asset {ev.asset_id} not in master (wo {no})"))
            else:
                p.errors.append(("unknown_wo", f"work order {no} was not opened"))
            return None
        if ev.asset_id != wo.asset_id:
            p.errors.append(("wo_asset_mismatch", f"wo {no} asset {ev.asset_id} != {wo.asset_id}"))
            return None
        return wo

    def _plan_wo_open(self, ev: SeedEvent, p: Plan) -> None:
        d = ev.data
        no = d["work_order_no"]
        if d["scope"]:
            p.features.append("wo.open.scope")
        if no in self.wos:
            p.errors.append(("wo_duplicate_open", f"wo {no} opened twice"))
            return
        if ev.asset_id not in self.assets:
            p.errors.append(("unknown_asset", f"asset {ev.asset_id} not in master (wo {no})"))
            return
        fsa = parse_utc_z(d["failure_started_at"]) if d["failure_started_at"] else None
        if fsa is not None and not fsa < ev.at:
            p.errors.append(("fsa_after_open", f"wo {no}: failure_started_at not before open"))
            fsa = None  # 照開單、不帶此值(決策 5)
        p.op = "wo.open"
        p.args = {
            "work_order_no": no,
            "asset_id": ev.asset_id,
            "work_type": d["work_type"],
            "brief_description": d["brief_description"],
            "failure_started_at": fsa,
        }

    def _plan_wo_scope(self, ev: SeedEvent, p: Plan) -> None:
        p.features.append("wo.scope")  # cmms 無 scope 概念;整個 kind 計入 skipped_features

    def _plan_wo_status(self, ev: SeedEvent, p: Plan) -> None:
        wo = self._wo(ev, p)
        if wo is None:
            return
        d = ev.data
        no, frm, to, hr = d["work_order_no"], d["from_status"], d["to_status"], d["hold_reason"]
        if frm != wo.status:
            p.errors.append(("wo_status_chain", f"wo {no} from {frm} != current {wo.status}"))
            return
        args: dict[str, Any] = {"work_order_no": no}
        if to == "IN_PROGRESS":
            op = "wo.start" if wo.status == "OPEN" else "wo.resume"
        elif to == "ON_HOLD":
            if hr not in HOLD_REASONS:
                p.errors.append(("hold_reason_missing", f"wo {no} ON_HOLD with {hr!r}"))
                return
            op = "wo.hold"
            args["hold_reason"] = hr
        elif to == "COMPLETED":
            op = "wo.complete"
        elif to == "CLOSED":
            op = "wo.close"
        elif to == "VOIDED":
            op = "wo.void"
        elif to == "CANCELLED":
            if wo.status == "OPEN":
                op = "wo.cancel"
            else:  # cmms 只允許 OPEN→CANCELLED;其餘起點改走作廢並記錯(決策 6)
                op = "wo.void"
                p.errors.append(("cancel_mapped_to_void", f"wo {no}: CANCELLED from {wo.status}"))
        else:  # to == "OPEN":cmms 狀態機無回到 OPEN 的轉移
            p.errors.append(("wo_status_chain", f"wo {no}: transition to {to} not representable"))
            return
        if self.downtime_maps is not None:
            status_map, hold_map = self.downtime_maps
            ours = segment_is_downtime(
                wo.work_type, to, hr if to == "ON_HOLD" else None,
                status_is_downtime=status_map, hold_is_downtime=hold_map,
            )
            if ours != d["is_downtime"]:
                p.warnings.append("is_downtime_mismatch")
        p.op = op
        p.args = args

    def _plan_wo_note(self, ev: SeedEvent, p: Plan) -> None:
        if self._wo(ev, p) is None:
            return
        p.op = "wo.note"
        p.args = {
            "work_order_no": ev.data["work_order_no"],
            "entry_type": ev.data["entry_type"],
            "body": ev.data["body"],
        }

    def _plan_wo_part(self, ev: SeedEvent, p: Plan) -> None:
        if self._wo(ev, p) is None:
            return
        code = ev.data["item_code"]
        if code not in self.items:
            p.errors.append(("unknown_item", f"item {code} not defined"))
            return
        p.op = "wo.part"
        p.args = {
            "work_order_no": ev.data["work_order_no"],
            "item_code": code,
            "quantity": ev.data["quantity"],
        }

    def _plan_wo_field(self, ev: SeedEvent, p: Plan) -> None:
        wo = self._wo(ev, p)
        if wo is None:
            return
        no, fld, val = ev.data["work_order_no"], ev.data["field"], ev.data["value"]
        if fld == "assigned_vendor":
            p.features.append("wo.field.assigned_vendor")  # 無對應 setter,跳過計數
            return
        if fld == "confirmed_reason_code":
            p.op = "wo.reason"
            p.args = {"work_order_no": no, "code": val}
            return
        value: Any = val
        if fld == "production_resumed_at":
            value = parse_utc_z(val)
            if wo.work_type != "REACTIVE":
                p.errors.append(("pra_non_reactive", f"wo {no}: production_resumed_at on PM"))
                return
            start = wo.first_start or wo.opened_at
            if not value > start:  # type: ignore[operator]
                p.errors.append(("pra_not_after_start", f"wo {no}: {val} not after start"))
                return
        elif fld == "scheduled_date":
            value = parse_date(val)
        elif fld == "downtime_estimated" and not isinstance(val, bool):
            p.errors.append(("field_type", f"wo {no}: downtime_estimated must be bool"))
            return
        p.op = "wo.field"
        p.args = {"work_order_no": no, "field": fld, "value": value}


_STATUS_OPS = {
    "wo.start": "IN_PROGRESS",
    "wo.resume": "IN_PROGRESS",
    "wo.hold": "ON_HOLD",
    "wo.complete": "COMPLETED",
    "wo.close": "CLOSED",
    "wo.void": "VOIDED",
    "wo.cancel": "CANCELLED",
}


def tally_plan(t: Tally, plan: Plan, *, applied: bool) -> None:
    """把一個 plan 的結果記入計數(錯誤由呼叫端另記 log)。"""
    if plan.skipped_kind:
        t.skipped_kinds[plan.skipped_kind] += 1
    for f in plan.features:
        t.skipped_features[f] += 1
    for w in plan.warnings:
        t.warnings[w] += 1
    for code, detail in plan.errors:
        t.error(code, plan.event.seq, plan.event.line, detail)
    if applied:
        t.events_applied += 1


def window(events: Iterable[SeedEvent], *, after_seq: int, until: datetime) -> list[SeedEvent]:
    """本次要套用的事件:`seq > after_seq` 且 `at <= until`。"""
    return [e for e in events if e.seq > after_seq and e.at <= until]


def simulate(
    events: Iterable[SeedEvent],
    *,
    area_map: Mapping[str, str],
    after_seq: int,
    until: datetime,
) -> Tally:
    """dry-run:全部 plan 視為成功,只計數(無 DB)。`after_seq` 之前的事件只推進狀態。"""
    rp = Replayer(area_map)
    t = Tally()
    evs = list(events)
    for ev in evs:
        if ev.seq > after_seq:
            break
        if ev.kind != "asset.define":
            rp.take_links()
        plan = rp.plan(ev)
        if plan.op:
            rp.confirm(plan)
    for ev in window(evs, after_seq=after_seq, until=until):
        if ev.kind != "asset.define":
            for lk in rp.take_links():
                if lk.error:
                    t.error(lk.error[0], lk.seq, lk.line, lk.error[1])
        plan = rp.plan(ev)
        if plan.op:
            rp.confirm(plan)
        tally_plan(t, plan, applied=plan.op is not None)
    for lk in rp.take_links():
        if lk.error:
            t.error(lk.error[0], lk.seq, lk.line, lk.error[1])
    return t
