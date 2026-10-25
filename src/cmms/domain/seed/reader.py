"""`tendline.seed.v1.1` 種子帳讀取器(純函式;無 DB、無 I/O)。

呼叫端(CLI / 測試)讀檔後把 **bytes** 交進來;本模組只做解析、驗證、正規化。契約權威 =
外部宣告方的 validator(tendline-board `wsynth_seed.py`);本檔照其**消費方模式(寬進)**實作
本 instance 需要的子集,字面值逐一照抄(對版時朗讀的那一份)。

讀法
- **拒讀**(`SeedRejected`,整份不讀):manifest 缺 / 壞形(`manifest`)、schema 不符
  (`schema_mismatch`)、world_id 與預期不符(`world_mismatch`)、日檔 sha256 / 行數 / 集合與
  manifest 不符(`manifest_mismatch`)、測試世界(`test_world`)。任一行的 schema / world_id
  不符同樣整份拒讀。
- **寬進**:未知 kind → 保留為 `status="unknown_kind"` 事件(下游跳過並計數);保留 kind
  (`pm.schedule`)→ `status="reserved_kind"`;事件多出的欄位 → 丟掉(白名單複本)。
- **行級壞資料**(非 UTF-8 / 非 JSON / 缺欄 / 型別錯 / 閉集外)→ 該行丟棄、記錯誤,不炸。
- **順序**:`seq` 必須嚴格遞增、`at` 不得倒退;違反 → `seq_order` / `at_order`,該行丟棄。

錯誤紀錄 = `{code, seq, line, detail}`(`seq` = 該行 seq 值或 None;`line` = 本次輸入中的
1 起位置,跨日檔連續計;manifest 層錯誤不走這裡,直接拒讀)。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

# ===========================================================================
# 契約字面值(照抄宣告方 wsynth_seed.py;改了要對版)
# ===========================================================================

SCHEMA = "tendline.seed.v1.1"
MANIFEST_SCHEMA = "tendline.seed.manifest.v1.1"
COMMON_KEYS = ("schema", "world_id", "seq", "at", "kind", "asset_id", "actor")
ROLES = ("role:operator", "role:technician", "role:engineer", "role:planner", "role:system")
KINDS_V1 = (
    "asset.define", "wo.open", "wo.status", "wo.scope", "wo.note", "wo.part",
    "wo.field", "asset.schedule_state", "inventory.item",
)
RESERVED_KINDS = ("pm.schedule",)
ITEM_ASSET_ID = "EID-00000"  # inventory.item 的哨兵 asset_id;永不拿它查資產主檔
KIND_KEYS: dict[str, tuple[str, ...]] = {
    "asset.define": ("station_cls", "stations", "display_name", "area", "parent_asset_id",
                     "seats", "roles", "in_service_from"),
    "wo.open": ("work_order_no", "work_type", "brief_description", "failure_started_at",
                "scope", "sub_units", "pm_source_id"),
    "wo.status": ("work_order_no", "from_status", "to_status", "hold_reason", "is_downtime"),
    "wo.scope": ("work_order_no", "scope", "sub_units"),
    "wo.note": ("work_order_no", "entry_type", "body"),
    "wo.part": ("work_order_no", "item_code", "quantity"),
    "wo.field": ("work_order_no", "field", "value"),
    "asset.schedule_state": ("state", "effective_date", "sub_unit", "reason"),
    "inventory.item": ("item_code", "display_name"),
}
WORK_TYPES = ("PM", "REACTIVE")
WO_STATUSES = ("OPEN", "IN_PROGRESS", "ON_HOLD", "COMPLETED", "CLOSED", "CANCELLED", "VOIDED")
HOLD_REASONS = ("WAITING_MACHINE_TIME", "TEST_RUN", "WAITING_PARTS", "WAITING_VENDOR", "OTHER")
NOTE_ENTRY_TYPES = ("report", "progress", "hold", "resume", "part", "note")
WO_FIELDS = ("diagnosis", "action_taken", "confirmed_reason_code", "production_resumed_at",
             "downtime_estimated", "scheduled_date", "assigned_vendor")
ASSET_ROLES = ("contains_module", "cotag", "alias_of")
SCHEDULE_STATES = ("not_scheduled", "in_production")
DISEASE_FLAGS = ("zero_length_stations", "on_hold_reasons", "one_click_close", "long_stop",
                 "seat_level_wo")
MANIFEST_KEYS = ("schema", "world_id", "generator_version", "fleet_digest", "horizon_start",
                 "exposed_until", "days")
MANIFEST_OPTIONAL_KEYS = ("flags", "test_only")
MANIFEST_DAY_KEYS = ("day", "file", "n_events", "sha256")
STATE_FINGERPRINT_KEYS = ("schema", "world_id", "generator_version", "fleet_digest",
                          "horizon_start")
WO_NO_FIRST, WO_NO_LAST = 710001, 799999
ITEM_CODE_RE = re.compile(r"^TL(\d{6})$")
ITEM_CODE_FIRST, ITEM_CODE_LAST = 900001, 999999

_UTC_Z_FMT = "%Y-%m-%dT%H:%M:%SZ"
_WORLD_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ASSET_ID_RE = re.compile(r"^EID-\d{5}(-N[1-9]\d*)?$")
_MACHINE_ID_RE = re.compile(r"^EID-\d{5}$")
_NEST_UNIT_RE = re.compile(r"^nest:([1-9]\d*)$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class SeedRejected(ValueError):
    """拒讀(整份不讀)。`code` 為拒讀類別,`detail` 為人讀說明。"""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ===========================================================================
# 小工具
# ===========================================================================


def parse_utc_z(value: Any) -> datetime | None:
    """契約時戳(UTC ISO `Z`,秒精度)→ tz-aware UTC datetime;不合形 → None。"""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _UTC_Z_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_date(value: Any) -> date | None:
    if not (isinstance(value, str) and _DATE_RE.match(value)):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v: Any) -> bool:
    return isinstance(v, str) and v.strip() != ""


def _str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def day_file_name(day: str) -> str:
    return f"seed-{day}.jsonl"


# ===========================================================================
# manifest
# ===========================================================================


@dataclass(frozen=True, slots=True)
class DayEntry:
    day: str
    file: str
    n_events: int
    sha256: str


@dataclass(frozen=True, slots=True)
class Manifest:
    schema: str
    world_id: str
    generator_version: str
    fleet_digest: str
    horizon_start: str
    exposed_until: str
    days: tuple[DayEntry, ...]

    def fingerprint(self) -> dict[str, str]:
        """續讀指紋(STATE_FINGERPRINT_KEYS 五鍵,字串原樣)。"""
        return {k: getattr(self, k) for k in STATE_FINGERPRINT_KEYS}


def _flags_default(flags: Any) -> bool:
    default = dict.fromkeys(DISEASE_FLAGS, True)
    return flags is None or (isinstance(flags, dict) and flags == default)


def parse_manifest(raw: bytes, *, expect_world: str | None = None) -> Manifest:
    """manifest bytes → `Manifest`。形狀 / schema / world / 測試世界不合 → `SeedRejected`。"""
    try:
        m = json.loads(bytes(raw).decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise SeedRejected("manifest", f"manifest is not valid UTF-8 JSON ({e})") from e
    if not isinstance(m, dict):
        raise SeedRejected("manifest", "manifest is not an object")
    if m.get("schema") != MANIFEST_SCHEMA:
        raise SeedRejected(
            "schema_mismatch", f"manifest schema {m.get('schema')!r} != {MANIFEST_SCHEMA!r}"
        )
    missing = [k for k in MANIFEST_KEYS if k not in m]
    if missing:
        raise SeedRejected("manifest", f"manifest missing keys {missing}")
    problems: list[str] = []
    if not (isinstance(m["world_id"], str) and _WORLD_ID_RE.match(m["world_id"])):
        problems.append("world_id must be 16 lowercase hex")
    if not _nonempty_str(m["generator_version"]):
        problems.append("generator_version must be non-empty str")
    if not (isinstance(m["fleet_digest"], str) and _HEX64_RE.match(m["fleet_digest"])):
        problems.append("fleet_digest must be 64 lowercase hex")
    for k in ("horizon_start", "exposed_until"):
        if parse_utc_z(m[k]) is None:
            problems.append(f"{k} must be UTC Z")
    if not isinstance(m["days"], list):
        problems.append("days must be a list")
    if problems:
        raise SeedRejected("manifest", "; ".join(problems))
    if expect_world is not None and m["world_id"] != expect_world:
        raise SeedRejected(
            "world_mismatch", f"manifest world_id {m['world_id']!r} != expected {expect_world!r}"
        )
    if m.get("test_only") is True or not _flags_default(m.get("flags")):
        raise SeedRejected("test_world", "manifest marks a test-only world (flags/test_only)")
    days: list[DayEntry] = []
    prev: str | None = None
    for d in m["days"]:
        if not (isinstance(d, dict) and set(d) == set(MANIFEST_DAY_KEYS)):
            raise SeedRejected("manifest", f"day entry must have exactly {MANIFEST_DAY_KEYS}")
        if parse_date(d["day"]) is None or d["file"] != day_file_name(d["day"]):
            raise SeedRejected("manifest", f"day/file mismatch {d!r}")
        if not (_is_int(d["n_events"]) and d["n_events"] >= 0):
            raise SeedRejected("manifest", f"n_events must be int >= 0 ({d['day']})")
        if not (isinstance(d["sha256"], str) and _HEX64_RE.match(d["sha256"])):
            raise SeedRejected("manifest", f"sha256 must be 64 lowercase hex ({d['day']})")
        if prev is not None and d["day"] <= prev:
            raise SeedRejected("manifest", "days must be strictly ascending")
        prev = d["day"]
        days.append(DayEntry(d["day"], d["file"], d["n_events"], d["sha256"]))
    return Manifest(
        schema=m["schema"],
        world_id=m["world_id"],
        generator_version=m["generator_version"],
        fleet_digest=m["fleet_digest"],
        horizon_start=m["horizon_start"],
        exposed_until=m["exposed_until"],
        days=tuple(days),
    )


def verify_day_files(manifest: Manifest, files: Mapping[str, bytes]) -> None:
    """日檔 bytes 對 manifest:每個列名的檔必須在場、sha256(全檔位元組)相符、行數相符、
    LF 行尾。多給的檔(未列名)一律算錯。任一不符 → `SeedRejected("manifest_mismatch")`。"""
    listed = {d.file: d for d in manifest.days}
    problems: list[str] = []
    for name in sorted(set(listed) - set(files)):
        problems.append(f"listed file {name} is missing")
    for name in sorted(set(files) - set(listed)):
        problems.append(f"file {name} is not listed in manifest")
    for name, d in listed.items():
        raw = files.get(name)
        if raw is None:
            continue
        if hashlib.sha256(raw).hexdigest() != d.sha256:
            problems.append(f"sha256 mismatch for {name}")
        if raw.count(b"\n") != d.n_events or (raw and not raw.endswith(b"\n")) or b"\r" in raw:
            problems.append(f"line count / line ending mismatch for {name}")
    if problems:
        raise SeedRejected("manifest_mismatch", "; ".join(problems))


# ===========================================================================
# 事件
# ===========================================================================


@dataclass(frozen=True, slots=True)
class SeedEvent:
    """正規化後的一行。`data` = 該 kind 的專屬欄(白名單複本);未知 / 保留 kind 的 `data` 為空。

    `status`:`ok` / `unknown_kind` / `reserved_kind`(後兩者下游跳過並計數)。"""

    seq: int
    at: datetime
    at_z: str
    kind: str
    asset_id: str
    actor: str
    world_id: str
    line: int
    status: str = "ok"
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ReadResult:
    events: list[SeedEvent]
    errors: list[dict[str, Any]]
    n_lines: int


def _check_wo_no(ev: dict, errs: list[tuple[str, str]]) -> None:
    n = ev["work_order_no"]
    if not _is_int(n):
        errs.append(("field", "work_order_no must be int"))
    elif not WO_NO_FIRST <= n <= WO_NO_LAST:
        errs.append(("range", f"work_order_no {n} outside synthetic range"))


def _check_item_code(ic: Any, errs: list[tuple[str, str]]) -> None:
    m = ITEM_CODE_RE.match(ic) if isinstance(ic, str) else None
    if m is None:
        errs.append(("field", "item_code must be TL + 6 digits"))
    elif not ITEM_CODE_FIRST <= int(m.group(1)) <= ITEM_CODE_LAST:
        errs.append(("range", f"item_code {ic} outside synthetic range"))


def _seat_ok(owner: str, s: Any) -> bool:
    if not (isinstance(s, dict) and set(s) == {"unit", "asset_id"}):
        return False
    unit, aid = s["unit"], s["asset_id"]
    if not (isinstance(unit, str) and isinstance(aid, str)):
        return False
    m = _NEST_UNIT_RE.match(unit)
    if m:
        return aid == f"{owner}-N{m.group(1)}"
    if _MACHINE_ID_RE.match(unit):
        return aid == unit and unit != owner
    return False


def _check_kind_fields(kind: str, ev: dict) -> list[tuple[str, str]]:
    errs: list[tuple[str, str]] = []
    if kind == "asset.define":
        if not _MACHINE_ID_RE.match(ev["asset_id"]):
            errs.append(("field", "asset.define asset_id must be a machine id (no -N suffix)"))
        elif ev["asset_id"] == ITEM_ASSET_ID:
            errs.append(("field", f"asset.define must not define the sentinel {ITEM_ASSET_ID}"))
        if not (ev["station_cls"] is None or _nonempty_str(ev["station_cls"])):
            errs.append(("field", "station_cls must be str or null"))
        if not _str_list(ev["stations"]):
            errs.append(("field", "stations must be list[str]"))
        if not _nonempty_str(ev["display_name"]):
            errs.append(("field", "display_name must be non-empty str"))
        if not (ev["area"] is None or _nonempty_str(ev["area"])):
            errs.append(("field", "area must be str or null"))
        p = ev["parent_asset_id"]
        if not (p is None or (isinstance(p, str) and _MACHINE_ID_RE.match(p))):
            errs.append(("field", "parent_asset_id must be a machine EID or null"))
        seats = ev["seats"]
        if not (isinstance(seats, list) and all(_seat_ok(ev["asset_id"], s) for s in seats)):
            errs.append(("field", "seats malformed"))
        roles = ev["roles"]
        if not _str_list(roles):
            errs.append(("field", "roles must be list[str]"))
        elif any(r not in ASSET_ROLES for r in roles):
            errs.append(("closed_set", f"roles {roles!r} outside closed set"))
        if parse_date(ev["in_service_from"]) is None:
            errs.append(("field", "in_service_from must be YYYY-MM-DD"))
    elif kind == "wo.open":
        _check_wo_no(ev, errs)
        if ev["work_type"] not in WORK_TYPES:
            errs.append(("closed_set", f"work_type {ev['work_type']!r}"))
        if not _nonempty_str(ev["brief_description"]):
            errs.append(("field", "brief_description must be non-empty str"))
        f = ev["failure_started_at"]
        if not (f is None or parse_utc_z(f) is not None):
            errs.append(("field", "failure_started_at must be UTC Z or null"))
        if not _str_list(ev["scope"]):
            errs.append(("field", "scope must be list[str]"))
        if not _str_list(ev["sub_units"]):
            errs.append(("field", "sub_units must be list[str]"))
        if not (ev["pm_source_id"] is None or _nonempty_str(ev["pm_source_id"])):
            errs.append(("field", "pm_source_id must be str or null"))
    elif kind == "wo.status":
        _check_wo_no(ev, errs)
        for k in ("from_status", "to_status"):
            if ev[k] not in WO_STATUSES:
                errs.append(("closed_set", f"{k} {ev[k]!r}"))
        if not (ev["hold_reason"] is None or isinstance(ev["hold_reason"], str)):
            errs.append(("field", "hold_reason must be str or null"))
        if not isinstance(ev["is_downtime"], bool):
            errs.append(("field", "is_downtime must be bool"))
    elif kind == "wo.scope":
        _check_wo_no(ev, errs)
        if not _str_list(ev["scope"]):
            errs.append(("field", "scope must be list[str]"))
        if not _str_list(ev["sub_units"]):
            errs.append(("field", "sub_units must be list[str]"))
    elif kind == "wo.note":
        _check_wo_no(ev, errs)
        if ev["entry_type"] not in NOTE_ENTRY_TYPES:
            errs.append(("closed_set", f"entry_type {ev['entry_type']!r}"))
        if not _nonempty_str(ev["body"]):
            errs.append(("field", "body must be non-empty str"))
    elif kind == "wo.part":
        _check_wo_no(ev, errs)
        _check_item_code(ev["item_code"], errs)
        if not (_is_int(ev["quantity"]) and ev["quantity"] >= 1):
            errs.append(("field", "quantity must be int >= 1"))
    elif kind == "wo.field":
        _check_wo_no(ev, errs)
        fld, val = ev["field"], ev["value"]
        if fld not in WO_FIELDS:
            errs.append(("closed_set", f"field {fld!r}"))
        elif fld == "production_resumed_at":
            if parse_utc_z(val) is None:
                errs.append(("field", "production_resumed_at value must be UTC Z"))
        elif fld == "scheduled_date":
            if parse_date(val) is None:
                errs.append(("field", "scheduled_date value must be YYYY-MM-DD"))
        elif fld == "downtime_estimated":
            if val is None or isinstance(val, (list, dict)):
                errs.append(("field", "downtime_estimated value must be a non-null scalar"))
        elif not _nonempty_str(val):
            errs.append(("field", f"{fld} value must be non-empty str"))
    elif kind == "asset.schedule_state":
        if ev["state"] not in SCHEDULE_STATES:
            errs.append(("closed_set", f"state {ev['state']!r}"))
        if parse_date(ev["effective_date"]) is None:
            errs.append(("field", "effective_date must be YYYY-MM-DD"))
        su = ev["sub_unit"]
        if not (su is None or (isinstance(su, str) and _NEST_UNIT_RE.match(su))):
            errs.append(("field", "sub_unit must be nest:<n> or null"))
        if not _nonempty_str(ev["reason"]):
            errs.append(("field", "reason must be non-empty str"))
    elif kind == "inventory.item":
        if ev["asset_id"] != ITEM_ASSET_ID:
            errs.append(("field", f"inventory.item asset_id must be the sentinel {ITEM_ASSET_ID}"))
        _check_item_code(ev["item_code"], errs)
        if not _nonempty_str(ev["display_name"]):
            errs.append(("field", "display_name must be non-empty str"))
    return errs


def _envelope_errors(ev: dict) -> list[tuple[str, str]]:
    errs: list[tuple[str, str]] = []
    if not _is_int(ev["seq"]) or ev["seq"] < 1:
        errs.append(("envelope", "seq must be a positive int"))
    if parse_utc_z(ev["at"]) is None:
        errs.append(("envelope", "at must be UTC ISO Z with second precision"))
    if not isinstance(ev["kind"], str):
        errs.append(("envelope", "kind must be str"))
    if not (isinstance(ev["asset_id"], str) and _ASSET_ID_RE.match(ev["asset_id"])):
        errs.append(("envelope", "asset_id must be EID-ddddd or EID-ddddd-N<n>"))
    if not (isinstance(ev["world_id"], str) and _WORLD_ID_RE.match(ev["world_id"])):
        errs.append(("envelope", "world_id must be 16 lowercase hex"))
    if ev["actor"] not in ROLES:
        errs.append(("closed_set", f"actor {ev['actor']!r} not a role string"))
    return errs


def read_ledger(manifest: Manifest, files: Mapping[str, bytes]) -> ReadResult:
    """依 manifest `days[]` 順序逐檔逐行讀 → 正規化事件 + 行級錯誤。

    前置:`verify_day_files` 已通過(本函式不重驗 sha)。任一行 schema / world_id 不符 →
    `SeedRejected`(整份拒讀)。其餘壞行丟棄並記錯誤。"""
    events: list[SeedEvent] = []
    errors: list[dict[str, Any]] = []
    pos = 0
    prev_seq: int | None = None
    prev_at: str | None = None

    def err(code: str, seq: Any, detail: str) -> None:
        errors.append({
            "code": code, "seq": seq if _is_int(seq) else None, "line": pos, "detail": detail,
        })

    for d in manifest.days:
        for chunk in files[d.file].split(b"\n"):
            if chunk == b"":
                continue
            pos += 1
            try:
                ev = json.loads(chunk.decode("utf-8"))
            except UnicodeDecodeError as e:
                err("encoding", None, f"{d.file}: line is not valid UTF-8 ({e.reason})")
                continue
            except ValueError:
                err("envelope", None, f"{d.file}: line is not valid JSON")
                continue
            if not isinstance(ev, dict):
                err("envelope", None, f"{d.file}: line is not a JSON object")
                continue
            # 拒讀閘(只看 schema / world_id;先於其他驗證,同宣告方 _gate)
            if "schema" in ev and ev["schema"] != SCHEMA:
                raise SeedRejected(
                    "schema_mismatch", f"line {pos}: schema {ev['schema']!r} != {SCHEMA!r}"
                )
            if "world_id" in ev and ev["world_id"] != manifest.world_id:
                raise SeedRejected(
                    "world_mismatch",
                    f"line {pos}: world_id {ev['world_id']!r} != {manifest.world_id!r}",
                )
            seq = ev.get("seq")
            missing = [k for k in COMMON_KEYS if k not in ev]
            if missing:
                err("envelope", seq, f"missing common keys {missing}")
                continue
            env = _envelope_errors(ev)
            if env:
                for code, detail in env:
                    err(code, seq, detail)
                continue
            kind = ev["kind"]
            status = "ok"
            data: dict[str, Any] = {}
            if kind in RESERVED_KINDS:
                status = "reserved_kind"
            elif kind not in KIND_KEYS:
                status = "unknown_kind"
            else:
                miss = [k for k in KIND_KEYS[kind] if k not in ev]
                if miss:
                    err("field", seq, f"{kind} missing {miss}")
                    continue
                kerrs = _check_kind_fields(kind, ev)
                if kerrs:
                    for code, detail in kerrs:
                        err(code, seq, detail)
                    continue
                data = {k: ev[k] for k in KIND_KEYS[kind]}  # 白名單複本:多出欄位丟掉
            # 跨行順序(只對通過行級檢查的行判;壞行不推進游標)
            if prev_seq is not None and seq <= prev_seq:
                err("seq_order", seq, f"seq {seq} not greater than previous {prev_seq}")
                continue
            if prev_at is not None and ev["at"] < prev_at:  # 同形 UTC Z → 字串序 = 時間序
                err("at_order", seq, f"at {ev['at']} earlier than previous {prev_at}")
                continue
            prev_seq, prev_at = seq, ev["at"]
            events.append(
                SeedEvent(
                    seq=seq,
                    at=parse_utc_z(ev["at"]),  # type: ignore[arg-type]  # 已驗
                    at_z=ev["at"],
                    kind=kind,
                    asset_id=ev["asset_id"],
                    actor=ev["actor"],
                    world_id=ev["world_id"],
                    line=pos,
                    status=status,
                    data=data,
                )
            )
    return ReadResult(events=events, errors=errors, n_lines=pos)
