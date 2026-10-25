"""`tendline.seed.v1.1` 讀取器 + 預檢的純函式測試(免 Docker,永遠跑)。

迷你帳在 tmp 目錄現做(正確 manifest sha);不複製外部產生器的 fixture。若本機有產生器實際
產出的帳(`CMMS_SEED_SAMPLE_DIR` 或 scratch 路徑),另取第一天做形狀對照(無則 skip)。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cmms.domain.seed.reader import (
    MANIFEST_SCHEMA,
    SCHEMA,
    SeedRejected,
    parse_manifest,
    read_ledger,
    verify_day_files,
)
from cmms.domain.seed.transform import (
    DEFAULT_AREA_MAP,
    Replayer,
    SeedConfigError,
    parse_until,
    resolve_area_map,
    resolve_default_asset_type,
    simulate,
    window,
)

WORLD = "0123456789abcdef"
FLEET = "f" * 64
DAY = "2026-07-03"


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def _ev(seq: int, at: str, kind: str, asset_id: str, actor: str = "role:system", **kw) -> dict:
    return {"schema": SCHEMA, "world_id": WORLD, "seq": seq, "at": at, "kind": kind,
            "asset_id": asset_id, "actor": actor, **kw}


def _define(seq: int, asset_id: str, *, area: str = "za", station_cls: str | None = "segX",
            parent: str | None = None, roles: list[str] | None = None) -> dict:
    return _ev(seq, "2026-07-02T16:00:00Z", "asset.define", asset_id,
               station_cls=station_cls, stations=[station_cls] if station_cls else [],
               display_name=f"Rig {asset_id}", area=area, parent_asset_id=parent,
               seats=[], roles=roles or [], in_service_from="2026-07-03")


def _open(seq: int, at: str, no: int, asset_id: str, *, fsa: str | None = None,
          work_type: str = "REACTIVE", scope: list[str] | None = None) -> dict:
    return _ev(seq, at, "wo.open", asset_id, actor="role:operator", work_order_no=no,
               work_type=work_type, brief_description="feeder jam", failure_started_at=fsa,
               scope=scope or [], sub_units=[], pm_source_id=None)


def _status(seq: int, at: str, no: int, asset_id: str, frm: str, to: str,
            hold: str | None = None, down: bool = True) -> dict:
    return _ev(seq, at, "wo.status", asset_id, actor="role:technician", work_order_no=no,
               from_status=frm, to_status=to, hold_reason=hold, is_downtime=down)


def _base_events() -> list[dict]:
    """1 資產、1 料號、1 張 REACTIVE 工單完整走一輪(15 行)。"""
    a = "EID-97001"
    return [
        _define(1, a),
        _ev(2, "2026-07-02T16:00:00Z", "inventory.item", "EID-00000",
            item_code="TL900001", display_name="bearing"),
        _open(3, "2026-07-03T01:00:00Z", 710001, a, fsa="2026-07-03T00:50:00Z"),
        _ev(4, "2026-07-03T01:00:00Z", "wo.note", a, actor="role:operator",
            work_order_no=710001, entry_type="report", body="jammed"),
        _status(5, "2026-07-03T01:10:00Z", 710001, a, "OPEN", "IN_PROGRESS"),
        _status(6, "2026-07-03T01:30:00Z", 710001, a, "IN_PROGRESS", "ON_HOLD", "WAITING_PARTS"),
        _ev(7, "2026-07-03T01:31:00Z", "wo.part", a, actor="role:technician",
            work_order_no=710001, item_code="TL900001", quantity=2),
        _status(8, "2026-07-03T02:00:00Z", 710001, a, "ON_HOLD", "IN_PROGRESS"),
        _ev(9, "2026-07-03T02:30:00Z", "wo.field", a, actor="role:technician",
            work_order_no=710001, field="diagnosis", value="Root cause: feeder jam."),
        _ev(10, "2026-07-03T02:30:00Z", "wo.field", a, actor="role:technician",
            work_order_no=710001, field="action_taken", value="recalibrated axis."),
        _status(11, "2026-07-03T02:30:00Z", 710001, a, "IN_PROGRESS", "COMPLETED", down=False),
        _ev(12, "2026-07-03T02:40:00Z", "wo.field", a, actor="role:technician",
            work_order_no=710001, field="production_resumed_at", value="2026-07-03T02:35:00Z"),
        _status(13, "2026-07-03T03:00:00Z", 710001, a, "COMPLETED", "CLOSED", down=False),
        _ev(14, "2026-07-03T03:00:00Z", "asset.schedule_state", a, actor="role:planner",
            state="in_production", effective_date="2026-07-04", sub_unit=None, reason="plan"),
        _ev(15, "2026-07-03T03:05:00Z", "wo.scope", a, actor="role:technician",
            work_order_no=710001, scope=["x"], sub_units=[]),
    ]


def write_ledger(root: Path, events: list[dict] | list[bytes], *,
                 manifest_patch: dict | None = None) -> Path:
    """事件(dict 或原始 bytes 行)→ 單日檔 + 正確 sha 的 manifest。回目錄。"""
    root.mkdir(parents=True, exist_ok=True)
    raw = b"".join(e if isinstance(e, bytes) else _line(e) for e in events)
    name = f"seed-{DAY}.jsonl"
    (root / name).write_bytes(raw)
    manifest = {
        "schema": MANIFEST_SCHEMA, "world_id": WORLD, "generator_version": "test-gen",
        "fleet_digest": FLEET, "horizon_start": "2026-07-02T16:00:00Z",
        "exposed_until": "2026-07-04T16:00:00Z",
        "days": [{"day": DAY, "file": name, "n_events": raw.count(b"\n"),
                  "sha256": hashlib.sha256(raw).hexdigest()}],
    }
    manifest.update(manifest_patch or {})
    (root / "seed-manifest.json").write_bytes(_line(manifest))
    return root


def load(root: Path, *, expect_world: str | None = None):
    m = parse_manifest((root / "seed-manifest.json").read_bytes(), expect_world=expect_world)
    files = {d.file: (root / d.file).read_bytes() for d in m.days}
    verify_day_files(m, files)
    return m, read_ledger(m, files)


UNTIL_ALL = datetime(2026, 7, 10, tzinfo=UTC)


# ---- 解析 ----


def test_valid_parse(tmp_path: Path) -> None:
    m, r = load(write_ledger(tmp_path, _base_events()), expect_world=WORLD)
    assert m.world_id == WORLD and m.fingerprint()["schema"] == MANIFEST_SCHEMA
    assert r.n_lines == 15 and len(r.events) == 15 and r.errors == []
    assert [e.seq for e in r.events] == list(range(1, 16))
    assert r.events[2].at == datetime(2026, 7, 3, 1, 0, tzinfo=UTC)
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL)
    assert t.line_errors == 0, t.records
    assert t.events_applied == 13  # 15 − schedule_state − wo.scope
    assert dict(t.skipped_kinds) == {"asset.schedule_state": 1}
    assert dict(t.skipped_features) == {"wo.scope": 1}


def test_manifest_schema_mismatch_rejects(tmp_path: Path) -> None:
    patch = {"schema": "tendline.seed.manifest.v1"}
    root = write_ledger(tmp_path, _base_events(), manifest_patch=patch)
    with pytest.raises(SeedRejected) as e:
        load(root)
    assert e.value.code == "schema_mismatch"


def test_event_schema_mismatch_rejects_whole_ledger(tmp_path: Path) -> None:
    evs = _base_events()
    evs[5]["schema"] = "tendline.seed.v1"
    with pytest.raises(SeedRejected) as e:
        load(write_ledger(tmp_path, evs))
    assert e.value.code == "schema_mismatch"


def test_world_mismatch_rejects(tmp_path: Path) -> None:
    with pytest.raises(SeedRejected) as e:
        load(write_ledger(tmp_path, _base_events()), expect_world="ffffffffffffffff")
    assert e.value.code == "world_mismatch"


def test_test_world_rejects(tmp_path: Path) -> None:
    root = write_ledger(tmp_path, _base_events(), manifest_patch={"test_only": True})
    with pytest.raises(SeedRejected) as e:
        load(root)
    assert e.value.code == "test_world"


def test_manifest_sha_mismatch_rejects(tmp_path: Path) -> None:
    root = write_ledger(tmp_path, _base_events())
    f = root / f"seed-{DAY}.jsonl"
    f.write_bytes(f.read_bytes().replace(b"jammed", b"JAMMED"))  # 同長度、不同位元組
    with pytest.raises(SeedRejected) as e:
        load(root)
    assert e.value.code == "manifest_mismatch"


def test_unknown_kind_skipped_and_counted(tmp_path: Path) -> None:
    evs = _base_events()[:3] + [_ev(4, "2026-07-03T01:00:00Z", "x.future", "EID-97001", foo=1),
                                _ev(5, "2026-07-03T01:00:00Z", "pm.schedule", "EID-97001")]
    _, r = load(write_ledger(tmp_path, evs))
    assert r.errors == []
    assert [(e.kind, e.status) for e in r.events[3:]] == [
        ("x.future", "unknown_kind"), ("pm.schedule", "reserved_kind")]
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL)
    assert dict(t.skipped_kinds) == {"x.future": 1, "pm.schedule": 1}
    assert t.line_errors == 0


def test_extra_field_dropped(tmp_path: Path) -> None:
    evs = _base_events()
    evs[3]["mood"] = "grumpy"
    _, r = load(write_ledger(tmp_path, evs))
    assert r.errors == []
    note = r.events[3]
    assert note.kind == "wo.note" and "mood" not in note.data
    assert set(note.data) == {"work_order_no", "entry_type", "body"}


def test_bad_line_dropped_with_error(tmp_path: Path) -> None:
    evs: list = _base_events()[:4]
    evs.insert(2, b"{not json\n")
    bad_field = _base_events()[4]
    bad_field["to_status"] = "EXPLODED"
    evs.append(bad_field)
    _, r = load(write_ledger(tmp_path, evs))
    assert len(r.events) == 4
    assert [(e["code"], e["line"]) for e in r.errors] == [("envelope", 3), ("closed_set", 6)]
    assert r.errors[1]["seq"] == 5


def test_seq_and_at_order_violations_dropped(tmp_path: Path) -> None:
    evs = _base_events()[:5]
    dup = dict(evs[4])  # 重複 seq 5
    late = _ev(7, "2026-07-03T00:00:00Z", "wo.note", "EID-97001", actor="role:technician",
               work_order_no=710001, entry_type="note", body="time travel")
    evs += [dup, _ev(6, "2026-07-03T01:20:00Z", "wo.note", "EID-97001", actor="role:technician",
                     work_order_no=710001, entry_type="note", body="ok"), late]
    _, r = load(write_ledger(tmp_path, evs))
    assert [e["code"] for e in r.errors] == ["seq_order", "at_order"]
    assert [e.seq for e in r.events] == [1, 2, 3, 4, 5, 6]


# ---- 窗 / cut-off ----


def test_until_filtering(tmp_path: Path) -> None:
    _, r = load(write_ledger(tmp_path, _base_events()))
    cut = datetime(2026, 7, 3, 1, 30, tzinfo=UTC)  # 含 seq 6(at == cut)
    assert [e.seq for e in window(r.events, after_seq=0, until=cut)] == [1, 2, 3, 4, 5, 6]
    assert [e.seq for e in window(r.events, after_seq=4, until=cut)] == [5, 6]
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=cut)
    assert t.events_applied == 6 and t.line_errors == 0


def test_resume_simulation_hydrates_state(tmp_path: Path) -> None:
    """after_seq 之前的事件只推進狀態:續跑窗內的 wo.* 不會誤報 unknown_wo。"""
    _, r = load(write_ledger(tmp_path, _base_events()))
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=8, until=UNTIL_ALL)
    assert t.line_errors == 0, t.records
    assert t.events_applied == 5  # seq 9..13


def test_parse_until_naive_is_taipei() -> None:
    assert parse_until("2026-10-01T19:36:00") == datetime(2026, 10, 1, 11, 36, tzinfo=UTC)
    assert parse_until("2026-10-01T11:36:00Z") == datetime(2026, 10, 1, 11, 36, tzinfo=UTC)
    assert parse_until("2026-10-01T12:36:00+01:00") == datetime(2026, 10, 1, 11, 36, tzinfo=UTC)
    now = datetime(2026, 10, 25, 3, 0, 0, 123456, tzinfo=UTC)
    assert parse_until(None, now=now) == datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
    with pytest.raises(SeedConfigError) as e:
        parse_until("yesterday")
    assert e.value.code == "bad_until"


# ---- area map / 預檢 ----


def test_area_map_unknown_key_blocks_asset_and_its_events(tmp_path: Path) -> None:
    evs = [_define(1, "EID-97001", area="nope"),
           _open(2, "2026-07-03T01:00:00Z", 710001, "EID-97001"),
           _status(3, "2026-07-03T01:10:00Z", 710001, "EID-97001", "OPEN", "IN_PROGRESS")]
    _, r = load(write_ledger(tmp_path, evs))
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL)
    assert dict(t.errors_by_code) == {"unknown_area": 1, "unknown_asset": 2}
    assert t.events_applied == 0


def test_area_map_override_and_validation() -> None:
    assert resolve_area_map(None, None) == DEFAULT_AREA_MAP
    assert resolve_area_map('{"k1":"Zone A"}', '{"k2":"Zone B"}') == {"k1": "Zone A"}
    assert resolve_area_map(None, '{"k2":"Zone B"}') == {"k2": "Zone B"}
    for bad in ('{"k1":"Dana Base"}', "[1]", "{"):
        with pytest.raises(SeedConfigError) as e:
            resolve_area_map(bad, None)
        assert e.value.code == "bad_area_map"


def test_precheck_soft_errors(tmp_path: Path) -> None:
    """fsa 不早於開單 → 照開、不帶 fsa;pra 不晚於開始 → 丟棄;CANCELLED 非 OPEN 起點 → 作廢。"""
    a = "EID-97001"
    evs = [
        _define(1, a),
        _open(2, "2026-07-03T01:00:00Z", 710001, a, fsa="2026-07-03T01:00:00Z"),
        _status(3, "2026-07-03T01:10:00Z", 710001, a, "OPEN", "IN_PROGRESS"),
        _ev(4, "2026-07-03T01:20:00Z", "wo.field", a, actor="role:technician",
            work_order_no=710001, field="production_resumed_at", value="2026-07-03T01:05:00Z"),
        _status(5, "2026-07-03T01:30:00Z", 710001, a, "IN_PROGRESS", "CANCELLED", down=False),
    ]
    _, r = load(write_ledger(tmp_path, evs))
    rp = Replayer(DEFAULT_AREA_MAP)
    plans = []
    for ev in r.events:
        p = rp.plan(ev)
        if p.op:
            rp.confirm(p)
        plans.append(p)
    assert plans[1].op == "wo.open" and plans[1].args["failure_started_at"] is None
    assert plans[1].errors[0][0] == "fsa_after_open"
    assert plans[3].op is None and plans[3].errors[0][0] == "pra_not_after_start"
    assert plans[4].op == "wo.void" and plans[4].errors[0][0] == "cancel_mapped_to_void"


def test_null_station_cls_applied_with_default_type(tmp_path: Path) -> None:
    _, r = load(write_ledger(tmp_path, [_define(1, "EID-97001", station_cls=None)]))
    rp = Replayer(DEFAULT_AREA_MAP)
    p = rp.plan(r.events[0])
    assert p.op == "asset.create" and p.errors == []
    assert p.args["asset_type"] == "UNCLASSIFIED"
    assert p.args["asset_type_label"] == "Unclassified (no station class in source)"
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL)
    assert t.line_errors == 0 and t.events_applied == 1
    assert dict(t.skipped_features) == {"asset.define.station_cls:null": 1}
    t2 = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL,
                  default_asset_type="NOCLASS")
    assert t2.line_errors == 0
    assert Replayer(DEFAULT_AREA_MAP, default_asset_type="NOCLASS").plan(
        r.events[0]).args["asset_type"] == "NOCLASS"


def test_null_station_cls_disabled_fallback_errors(tmp_path: Path) -> None:
    _, r = load(write_ledger(tmp_path, [_define(1, "EID-97001", station_cls=None)]))
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL,
                 default_asset_type=None)
    assert dict(t.errors_by_code) == {"station_cls_missing": 1}
    assert t.events_applied == 0


def test_resolve_default_asset_type() -> None:
    assert resolve_default_asset_type(None, None) == "UNCLASSIFIED"
    assert resolve_default_asset_type(None, "ENV") == "ENV"
    assert resolve_default_asset_type("CLI", "ENV") == "CLI"
    assert resolve_default_asset_type("", "ENV") is None  # 空字串 = 停用
    assert resolve_default_asset_type(None, "  ") is None


def test_seat_id_folded_to_owner_incl_follow_up(tmp_path: Path) -> None:
    owner, seat = "EID-97001", "EID-97001-N3"
    evs = [_define(1, owner),
           _open(2, "2026-07-03T01:00:00Z", 710001, seat),
           _status(3, "2026-07-03T01:10:00Z", 710001, seat, "OPEN", "IN_PROGRESS"),
           _status(4, "2026-07-03T01:20:00Z", 710001, owner, "IN_PROGRESS", "COMPLETED",
                   down=False),
           _status(5, "2026-07-03T01:30:00Z", 710001, "EID-97009-N1", "COMPLETED", "CLOSED",
                   down=False)]
    _, r = load(write_ledger(tmp_path, evs))
    rp = Replayer(DEFAULT_AREA_MAP)
    plans = []
    for ev in r.events:
        p = rp.plan(ev)
        if p.op:
            rp.confirm(p)
        plans.append(p)
    assert plans[1].op == "wo.open" and plans[1].args["asset_id"] == owner
    assert plans[1].folded == "seat_folded_to_owner"
    assert plans[2].op == "wo.start" and plans[2].errors == []  # 後續座位 id 同折
    assert plans[3].op == "wo.complete" and plans[3].errors == []  # 直接用機台 id 亦一致
    assert plans[4].op is None and plans[4].errors[0][0] == "wo_asset_mismatch"  # 折後比對
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL)
    assert dict(t.folded) == {"seat_folded_to_owner": 1}  # 只計 wo.open
    assert dict(t.errors_by_code) == {"wo_asset_mismatch": 1}


def test_seat_fold_with_unknown_owner_is_unknown_asset(tmp_path: Path) -> None:
    evs = [_define(1, "EID-97001"),
           _open(2, "2026-07-03T01:00:00Z", 710001, "EID-20002-N1"),
           _status(3, "2026-07-03T01:10:00Z", 710001, "EID-20002-N1", "OPEN", "IN_PROGRESS")]
    _, r = load(write_ledger(tmp_path, evs))
    t = simulate(r.events, area_map=DEFAULT_AREA_MAP, after_seq=0, until=UNTIL_ALL)
    assert dict(t.errors_by_code) == {"unknown_asset": 2}
    assert t.folded["seat_folded_to_owner"] == 0
    assert "EID-20002" in t.records[0]["detail"]


def test_is_downtime_mismatch_is_a_warning(tmp_path: Path) -> None:
    a = "EID-97001"
    evs = [_define(1, a), _open(2, "2026-07-03T01:00:00Z", 710001, a),
           _status(3, "2026-07-03T01:10:00Z", 710001, a, "OPEN", "IN_PROGRESS", down=False)]
    _, r = load(write_ledger(tmp_path, evs))
    rp = Replayer(DEFAULT_AREA_MAP, downtime_maps=({"IN_PROGRESS": True}, {}))
    rp.confirm(rp.plan(r.events[0]))
    rp.confirm(rp.plan(r.events[1]))
    p = rp.plan(r.events[2])
    assert p.op == "wo.start" and p.errors == [] and p.warnings == ["is_downtime_mismatch"]


# ---- 實際產生器產出(若在場)----

_SAMPLE = Path(os.environ.get(
    "CMMS_SEED_SAMPLE_DIR",
    "/private/tmp/claude-501/-Users-chris-Dev-showcase-shopfloor-cmms/"
    "27147468-3c46-46bd-8c11-c23c57f1c364/scratchpad/wsynth-out/seed",
))


@pytest.mark.skipif(not (_SAMPLE / "seed-manifest.json").is_file(), reason="no generated ledger")
def test_generated_ledger_first_day_parses_clean(tmp_path: Path) -> None:
    m = json.loads((_SAMPLE / "seed-manifest.json").read_bytes())
    first = m["days"][0]
    shutil.copy(_SAMPLE / first["file"], tmp_path / first["file"])
    m["days"] = [first]
    (tmp_path / "seed-manifest.json").write_bytes(_line(m))
    man, r = load(tmp_path, expect_world=m["world_id"])
    assert r.errors == [] and len(r.events) == first["n_events"]
    assert {e.kind for e in r.events} >= {"asset.define", "inventory.item", "wo.open"}
