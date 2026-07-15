"""efc 碼 × 維修單交叉比對種子的測試(下游交付;分析平台)。

三層:
1. 純函式單元(窗邊界 / OPEN 窗延伸至 as_of / 未知 EID 排除 / 取消·作廢工單排除 /
   verdict 門檻邊界)—— 免 DB,永遠跑。
2. golden 契約(`tests/fixtures/efc_workorder_crosscheck_seed.v1.json` byte-exact)——
   比照 `tests/test_contract_fixture.py` 制度:序列化形狀一變即紅 → 機械觸發對 分析平台的 push;
   破壞性變更 **bump v2**(新檔名,舊版保留),單純補代表值可原地更新。
3. DB 整合(testcontainers)+ 真 CSV smoke(檔缺則 skip)。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cmms.domain.failure_vocab.crosscheck import (
    SCHEMA_ID,
    EfcEvent,
    EfcEventsRead,
    build_seed,
    crosscheck,
    read_efc_events,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "efc_workorder_crosscheck_seed.v1.json"
_REAL_CSV = Path(__file__).resolve().parents[1] / "data" / "raw" / "efc_events_top20_60d.csv"

_BUFFER = timedelta(days=1)
_AS_OF = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)


def _utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# ---- 純函式:比對窗邊界 ----


def test_event_exactly_at_lower_boundary_matches() -> None:
    """事件恰在 opened_at − buffer 邊界(含端點)→ matched。"""
    opened = _utc(2026, 6, 10, 9)
    ev = EfcEvent("efcX", "EID-001", opened - _BUFFER)  # 恰在下界
    res = crosscheck([ev], {"EID-001": [(1, opened, opened)]}, {"EID-001"},
                     buffer=_BUFFER, as_of=_AS_OF)
    assert res.codes[0].n_events_matched == 1
    assert res.codes[0].matched_work_orders == [1]


def test_event_just_below_lower_boundary_unmatched() -> None:
    opened = _utc(2026, 6, 10, 9)
    ev = EfcEvent("efcX", "EID-001", opened - _BUFFER - timedelta(seconds=1))
    res = crosscheck([ev], {"EID-001": [(1, opened, opened)]}, {"EID-001"},
                     buffer=_BUFFER, as_of=_AS_OF)
    assert res.codes[0].n_events_matched == 0
    assert res.codes[0].matched_work_orders == []


def test_open_wo_window_extends_to_as_of() -> None:
    """closed_at=None(OPEN 工單)→ 窗尾 = as_of + buffer;之後的事件不 match。"""
    opened = _utc(2026, 6, 1, 9)
    inside = EfcEvent("efcX", "EID-001", _AS_OF)  # ≤ as_of + buffer
    outside = EfcEvent("efcX", "EID-001", _AS_OF + _BUFFER + timedelta(minutes=1))
    res = crosscheck([inside, outside], {"EID-001": [(7, opened, None)]}, {"EID-001"},
                     buffer=_BUFFER, as_of=_AS_OF)
    assert res.codes[0].n_events_total_checked == 2
    assert res.codes[0].n_events_matched == 1


def test_unknown_eid_excluded_from_denominator() -> None:
    """未知 EID(不在 known_eids)→ 排除分母、計入 n_events_unknown_eid;仍列入 eids_seen。"""
    known = EfcEvent("efcX", "EID-001", _utc(2026, 6, 10, 10))
    unknown = EfcEvent("efcX", "EID-999", _utc(2026, 6, 10, 10))
    windows = {"EID-001": [(1, _utc(2026, 6, 10, 9), _utc(2026, 6, 10, 17))]}
    res = crosscheck([known, unknown], windows, {"EID-001"}, buffer=_BUFFER, as_of=_AS_OF)
    c = res.codes[0]
    assert c.n_events_total_checked == 1  # 只算已知 EID
    assert c.n_events_matched == 1
    assert c.n_events_unknown_eid == 1
    assert c.eids_seen == ["EID-001", "EID-999"]  # provenance 含未知
    assert c.ratio == 1.0


def test_known_eid_no_wo_is_unmatched_signal() -> None:
    """EID 在主檔但零工單 → unmatched(是訊號,非未知)。"""
    ev = EfcEvent("efcX", "EID-003", _utc(2026, 6, 10, 10))
    res = crosscheck([ev], {}, {"EID-003"}, buffer=_BUFFER, as_of=_AS_OF)
    c = res.codes[0]
    assert c.n_events_total_checked == 1 and c.n_events_matched == 0
    assert c.n_events_unknown_eid == 0 and c.ratio == 0.0


def test_cancelled_void_wo_absent_from_windows_unmatched() -> None:
    """取消/作廢工單由 DB reader 過濾 → 不在 wo_windows;事件無窗可 match。

    (狀態排除的權威測試在 DB 整合層;此處驗純函式對「窗被移除」的結果。)
    """
    ev = EfcEvent("efcX", "EID-001", _utc(2026, 6, 10, 10))
    res = crosscheck([ev], {"EID-001": []}, {"EID-001"}, buffer=_BUFFER, as_of=_AS_OF)
    assert res.codes[0].n_events_matched == 0


def test_verdict_nuisance_boundary() -> None:
    """nuisance:total>=100 且 ratio<=0.02。100 事件 / 2 matched = 0.02 → nuisance。"""
    opened = _utc(2026, 6, 20, 9)
    closed = _utc(2026, 6, 20, 17)
    matched = [EfcEvent("efcN", "EID-001", _utc(2026, 6, 20, 10)) for _ in range(2)]
    # 98 筆遠離窗(6/1),不 match
    unmatched = [EfcEvent("efcN", "EID-001", _utc(2026, 6, 1) + timedelta(minutes=i))
                 for i in range(98)]
    res = crosscheck(matched + unmatched, {"EID-001": [(1, opened, closed)]}, {"EID-001"},
                     buffer=_BUFFER, as_of=_AS_OF)
    c = res.codes[0]
    assert c.n_events_total_checked == 100 and c.n_events_matched == 2
    assert c.ratio == 0.02 and c.verdict_hint == "nuisance"


def test_verdict_real_fault_boundary() -> None:
    """real_fault:matched>=3 且 ratio>=0.30。3 事件全 match = 1.0 → real_fault。"""
    opened = _utc(2026, 6, 20, 9)
    closed = _utc(2026, 6, 20, 17)
    evs = [EfcEvent("efcR", "EID-001", _utc(2026, 6, 20, 10)) for _ in range(3)]
    res = crosscheck(evs, {"EID-001": [(1, opened, closed)]}, {"EID-001"},
                     buffer=_BUFFER, as_of=_AS_OF)
    c = res.codes[0]
    assert c.n_events_matched == 3 and c.verdict_hint == "real_fault"


def test_verdict_insufficient_data() -> None:
    """低量或中間帶 → insufficient_data。"""
    opened = _utc(2026, 6, 20, 9)
    closed = _utc(2026, 6, 20, 17)
    evs = [EfcEvent("efcI", "EID-001", _utc(2026, 6, 20, 10)),
           EfcEvent("efcI", "EID-001", _utc(2026, 6, 1))]
    res = crosscheck(evs, {"EID-001": [(1, opened, closed)]}, {"EID-001"},
                     buffer=_BUFFER, as_of=_AS_OF)
    assert res.codes[0].verdict_hint == "insufficient_data"


def test_codes_sorted_by_volume_desc() -> None:
    big = [EfcEvent("efcBig", "EID-001", _utc(2026, 6, 5)) for _ in range(3)]
    small = [EfcEvent("efcSmall", "EID-001", _utc(2026, 6, 5))]
    res = crosscheck(big + small, {}, {"EID-001"}, buffer=_BUFFER, as_of=_AS_OF)
    assert [c.code for c in res.codes] == ["efcBig", "efcSmall"]


# ---- golden 契約 ----


def _synthetic_seed() -> dict:
    """小型合成情境(3 碼:real_fault / nuisance / insufficient + 未知 EID + known-no-WO)。

    固定 as_of/generated_at → deterministic;涵蓋三 verdict、未知 EID、工單 provenance、排序。
    """
    gen_at = datetime(2026, 7, 1, 0, 0, 0, tzinfo=UTC)
    # real_fault:5 事件全落 WO 1001 窗(6/20 09:00–17:00)→ ratio 1.0
    real = [EfcEvent("efcReal_Fault", "EID-001", _utc(2026, 6, 20, 10 + i)) for i in range(5)]
    # nuisance:100 事件 / 1 matched(WO 2001,6/15 10:00–12:00)→ ratio 0.01
    nuis_matched = [EfcEvent("efcNuisance", "EID-002", _utc(2026, 6, 15, 11))]
    nuis_unmatched = [
        EfcEvent("efcNuisance", "EID-002", _utc(2026, 6, 1) + timedelta(minutes=i))
        for i in range(99)
    ]
    # insufficient:2 已知(EID-003 零工單)+ 1 未知(EID-999)
    sparse = [
        EfcEvent("efcSparse", "EID-003", _utc(2026, 6, 25, 8)),
        EfcEvent("efcSparse", "EID-003", _utc(2026, 6, 26, 8)),
        EfcEvent("efcSparse", "EID-999", _utc(2026, 6, 25, 9)),
    ]
    events = real + nuis_matched + nuis_unmatched + sparse
    windows = {
        "EID-001": [(1001, _utc(2026, 6, 20, 9), _utc(2026, 6, 20, 17))],
        "EID-002": [(2001, _utc(2026, 6, 15, 10), _utc(2026, 6, 15, 12))],
    }
    known = {"EID-001", "EID-002", "EID-003"}  # EID-999 未知
    result = crosscheck(events, windows, known, buffer=_BUFFER, as_of=_AS_OF)
    return build_seed(result, events_file="synthetic_efc_events.csv",
                      generated_at=gen_at, n_rows_skipped=0)


def test_seed_matches_golden_fixture() -> None:
    """合成種子序列化 == 金樣本(形狀契約)。不等 → 形狀漂移,見本檔頂部制度。"""
    seed = _synthetic_seed()
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert seed == expected


def test_golden_has_expected_schema_and_verdicts() -> None:
    """金樣本語意鎖:schema_id + 三 verdict 各中一碼 + 未知 EID 計數。"""
    seed = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert seed["schema_id"] == SCHEMA_ID
    by_code = {c["code"]: c for c in seed["codes"]}
    assert by_code["efcReal_Fault"]["verdict_hint"] == "real_fault"
    assert by_code["efcNuisance"]["verdict_hint"] == "nuisance"
    assert by_code["efcSparse"]["verdict_hint"] == "insufficient_data"
    assert by_code["efcSparse"]["n_events_unknown_eid"] == 1
    assert by_code["efcSparse"]["evidence"]["eids_seen"] == ["EID-003", "EID-999"]
    # 排序:nuisance(100)> real_fault(5)> sparse(2)
    assert [c["code"] for c in seed["codes"]] == ["efcNuisance", "efcReal_Fault", "efcSparse"]


# ---- 真 CSV smoke(檔缺則 skip)----


@pytest.mark.skipif(
    not _REAL_CSV.exists(), reason="real plant data is not shipped in the public repo"
)
def test_read_real_csv_parses_all_rows() -> None:
    """真事件檔:3370 列全解析、20 碼、21 EID、零壞列(檔不在 repo 時 skip)。"""
    read = read_efc_events(_REAL_CSV)
    assert isinstance(read, EfcEventsRead)
    assert len(read.events) == 3370
    assert read.n_rows_skipped == 0
    assert len({e.code for e in read.events}) == 20
    assert len({e.eid for e in read.events}) == 21
    # 台北 naive → UTC aware(全部 tz-aware)
    assert all(e.occurred_at.tzinfo is not None for e in read.events)


def test_read_skips_malformed_rows(tmp_path: Path) -> None:
    """壞列(缺欄 / 壞時戳)跳過並計數。"""
    csv_path = tmp_path / "e.csv"
    csv_path.write_text(
        "efc_code,eid,event_timestamp\n"
        "efcX,EID-001,2026-06-02 13:40:44.0139\n"
        "efcX,EID-001,not-a-timestamp\n"
        ",EID-001,2026-06-02 13:40:44.0139\n"
        "efcX,,2026-06-02 13:40:44.0139\n"
        "efcX,EID-001,2026-06-02 13:40:44\n",  # 無小數秒也解析
        encoding="utf-8-sig",
    )
    read = read_efc_events(csv_path)
    assert len(read.events) == 2 and read.n_rows_skipped == 3


# ---- DB 整合(testcontainers;無 Docker 則 skip)----

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.loader import load as load_assets  # noqa: E402
from cmms.domain.failure_vocab.crosscheck import fetch_wo_windows, generate_seed  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.loader import load as load_wo  # noqa: E402
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402

_TESTER = Actor.human("tester")

# 只為種 lookup 表(work_type/wo_status/wo_hold_reason)用的一列;compid=EID-SEED 不與事件 EID 相干。
_SEED_WO_ROW = {
    "wo": "99001", "compid": "EID-SEED", "comp_desc": "x", "assetsubtp": "",
    "brief_desc": "seed", "diag": "", "comments": "", "date_wo": "05/01/26",
    "sch_date": "", "wo_type": "REACTIVE", "workstatus": "H", "miscreated": "F",
    "assignto": "", "edittime": "10:00:00", "editdate": "05/01/26", "edituser": "",
    "time": "09:00:00", "time_cmpl": "10:00:00",
}
_DB_ASSETS = [
    {"compid": e, "comp_desc": e, "assettype": "Production", "department": "EQ",
     "line_no": "10K", "available": "Yes"}
    for e in ("EID-001", "EID-002", "EID-SEED")
]


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load_assets(_DB_ASSETS, s)
            await load_wo([_SEED_WO_ROW], s)  # 種 lookup(REACTIVE 工單在 EID-SEED,不干擾事件)
            yield s
        await engine.dispose()


async def test_db_reader_excludes_cancelled_and_projects_windows(session) -> None:
    """DB reader:REACTIVE OPEN 工單成窗、CANCELLED 工單排除;§24 只投影存在+時間窗欄。"""
    svc = WorkOrderService(session)
    # EID-001:OPEN REACTIVE(closed_at=None → 窗延伸至 as_of)
    await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=_TESTER,
        at=datetime(2026, 6, 20, 9, tzinfo=UTC),
    )
    # EID-002:開後取消 → CANCELLED,應被 DB reader 排除
    wo_b = await svc.open_work_order(
        asset_id="EID-002", work_type="REACTIVE", actor=_TESTER,
        at=datetime(2026, 6, 10, 9, tzinfo=UTC),
    )
    await svc.cancel_reactive_report(wo_b.work_order_no, _TESTER)

    windows, known = await fetch_wo_windows(session, {"EID-001", "EID-002", "EID-999"})
    assert known == {"EID-001", "EID-002"}  # EID-999 不在主檔
    assert "EID-001" in windows and windows["EID-001"][0][2] is None  # OPEN → closed_at None
    assert "EID-002" not in windows  # CANCELLED 排除


async def test_generate_seed_end_to_end(session) -> None:
    """CLI-level orchestrator:事件 → DB reader → crosscheck → seed dict。"""
    svc = WorkOrderService(session)
    await svc.open_work_order(
        asset_id="EID-001", work_type="REACTIVE", actor=_TESTER,
        at=datetime(2026, 6, 20, 9, tzinfo=UTC),
    )
    wo_b = await svc.open_work_order(
        asset_id="EID-002", work_type="REACTIVE", actor=_TESTER,
        at=datetime(2026, 6, 10, 9, tzinfo=UTC),
    )
    await svc.cancel_reactive_report(wo_b.work_order_no, _TESTER)

    events = EfcEventsRead(
        events=[
            EfcEvent("efcA", "EID-001", datetime(2026, 6, 20, 10, tzinfo=UTC)),  # matched
            EfcEvent("efcA", "EID-002", datetime(2026, 6, 10, 10, tzinfo=UTC)),  # cancelled → skip
            EfcEvent("efcA", "EID-999", datetime(2026, 6, 20, 10, tzinfo=UTC)),  # unknown EID
        ],
        n_rows_skipped=0,
    )
    seed = await generate_seed(
        session, events, events_file="t.csv", buffer_days=1,
        as_of=datetime(2026, 7, 1, tzinfo=UTC),
        generated_at=datetime(2026, 7, 1, tzinfo=UTC),
    )
    assert seed["schema_id"] == SCHEMA_ID
    c = seed["codes"][0]
    assert c["code"] == "efcA"
    assert c["n_events_total_checked"] == 2  # 001+002 known;999 排除分母
    assert c["n_events_matched"] == 1  # 只有 001(002 工單已取消)
    assert c["n_events_unknown_eid"] == 1
    assert c["evidence"]["eids_seen"] == ["EID-001", "EID-002", "EID-999"]
