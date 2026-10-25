"""`cmms seed-events` 重播的 DB 整合(testcontainers;本機無 Docker 時 error/skip,同其他 *_db.py)。

迷你帳:2 資產(含 containment)、1 料號、1 張 REACTIVE 工單
OPEN→IN_PROGRESS→ON_HOLD(WAITING_PARTS)→IN_PROGRESS→COMPLETED→CLOSED,帶日誌、領料與三個欄位。
驗:status_history 時戳 == 事件 `at`、0036 兩欄、on_hand 20−qty、actor `human:synth.*`、
指紋續跑(不重放)、指紋不符 state_mismatch、cut-off 倒退 until_regressed。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.db import Base  # noqa: E402
from cmms.domain.asset import models as _asset_models  # noqa: E402, F401
from cmms.domain.asset.models import Asset, AssetRelationshipType, AssetType  # noqa: E402
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.failure_vocab import models as _fv_models  # noqa: E402, F401
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity.models import UserAccount  # noqa: E402
from cmms.domain.inventory import models as _inv_models  # noqa: E402, F401
from cmms.domain.inventory.models import InventoryItem, StockTransaction  # noqa: E402
from cmms.domain.notify import models as _notify_models  # noqa: E402, F401
from cmms.domain.pm_schedule import models as _pm_models  # noqa: E402, F401
from cmms.domain.seed import models as _seed_models  # noqa: E402, F401
from cmms.domain.seed.models import SeedWorld  # noqa: E402
from cmms.domain.seed.reader import SeedRejected  # noqa: E402
from cmms.domain.seed.service import SeedReplayService  # noqa: E402
from cmms.domain.seed.transform import DEFAULT_AREA_MAP  # noqa: E402
from cmms.domain.task import models as _task_models  # noqa: E402, F401
from cmms.domain.work_order import models as _wo_models  # noqa: E402, F401
from cmms.domain.work_order.models import WoNoteType, WorkOrder, WorkOrderNote  # noqa: E402
from cmms.domain.work_order.service import WorkOrderService  # noqa: E402
from tests.test_seed_reader import (  # noqa: E402
    WORLD,
    _base_events,
    _define,
    _open,
    _status,
    load,
    write_ledger,
)

PARENT, CHILD = "EID-97001", "EID-97002"
WO = 710001


def _ledger_events() -> list[dict]:
    """base 帳 + 一個子資產(parent=EID-97001),seq 重編為 1..n。"""
    evs = _base_events()
    evs.insert(1, _define(0, CHILD, parent=PARENT))
    for i, e in enumerate(evs, start=1):
        e["seq"] = i
    return evs


def _z(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            # create_all 不含 migration bulk_insert 的 lookup 列(0009 / 0012 種子)
            s.add(AssetRelationshipType(code="contains_module", label="contains module"))
            for code in ("report", "progress", "hold", "resume", "part", "note", "diagnosis"):
                s.add(WoNoteType(code=code, label=code))
            await s.commit()
            yield s
        await engine.dispose()


async def _replay(session, root: Path, until: datetime) -> dict:
    m, r = load(root)
    out = await SeedReplayService(session, area_map=DEFAULT_AREA_MAP).replay(m, r, until=until)
    return out.summary()


async def test_full_replay_end_to_end(session, tmp_path: Path) -> None:
    evs = _ledger_events()
    root = write_ledger(tmp_path / "w", evs)
    summary = await _replay(session, root, datetime(2026, 7, 10, tzinfo=UTC))

    assert summary["line_errors"] == 0, summary
    assert summary["world_id"] == WORLD
    assert summary["events_applied"] == 14  # 16 − schedule_state − wo.scope
    assert summary["events_resumed_from_seq"] == 1
    assert summary["skipped_kinds"] == {"asset.schedule_state": 1}
    assert summary["warnings"] == {}  # is_downtime 與 cmms 判定一致

    # 資產 + containment
    child = await session.get(Asset, CHILD)
    assert child is not None and child.parent_asset_id == PARENT
    assert child.line == "Zone A" and child.asset_type == "segX"
    assert child.source_actor == "human:synth.system"

    # 工單:狀態歷程時戳 == 事件 at;actor 為合成帳號
    wo = await session.get(WorkOrder, WO)
    assert wo is not None and wo.status == "CLOSED"
    status_ats = [e["at"] for e in evs if e["kind"] in ("wo.open", "wo.status")]
    hist = await WorkOrderService(session).get_status_history(WO)
    assert [h.changed_at for h in hist] == [_z(a) for a in status_ats]
    assert [h.to_status for h in hist] == [
        "OPEN", "IN_PROGRESS", "ON_HOLD", "IN_PROGRESS", "COMPLETED", "CLOSED"]
    assert hist[2].hold_reason == "WAITING_PARTS"
    assert hist[0].source_actor == "human:synth.operator"
    assert {h.source_actor for h in hist[1:]} == {"human:synth.technician"}
    assert wo.opened_at == _z("2026-07-03T01:00:00Z")
    assert wo.closed_at == _z("2026-07-03T03:00:00Z")
    assert wo.downtime_minutes is not None and wo.downtime_estimated is False

    # 0036 兩欄 + 三個欄位
    assert wo.failure_started_at == _z("2026-07-03T00:50:00Z")
    assert wo.production_resumed_at == _z("2026-07-03T02:35:00Z")
    assert wo.diagnosis == "Root cause: feeder jam."
    assert wo.action_taken == "recalibrated axis."

    # 日誌(occurred_at = 事件 at)
    notes = await WorkOrderService(session).list_notes(WO)
    assert [(n.entry_type, n.occurred_at, n.author) for n in notes] == [
        ("report", _z("2026-07-03T01:00:00Z"), "human:synth.operator")]

    # 料號:on_hand = 20 − 2;帳 = ADJUST(+20) + ISSUE(−2)
    item = await session.get(InventoryItem, "TL900001")
    assert item is not None and item.quantity_on_hand == Decimal(18)
    kinds = (await session.scalars(
        select(StockTransaction.kind).where(StockTransaction.item_code == "TL900001")
        .order_by(StockTransaction.txn_id))).all()
    assert list(kinds) == ["ADJUST", "ISSUE"]

    # 合成帳號(角色照決策 4)
    roles = {u.user_id: u.role for u in (await session.scalars(select(UserAccount))).all()}
    assert roles == {"synth.system": "admin", "synth.operator": "operator",
                     "synth.technician": "engineer"}

    # 指紋 / 進度
    sw = await session.get(SeedWorld, WORLD)
    assert sw is not None and sw.last_seq == len(evs)
    assert sw.applied_until == datetime(2026, 7, 10, tzinfo=UTC)


async def test_resume_from_fingerprint(session, tmp_path: Path) -> None:
    root = write_ledger(tmp_path / "w", _ledger_events())
    first = await _replay(session, root, _z("2026-07-03T01:30:00Z"))  # 到 ON_HOLD 為止(seq 7)
    assert first["line_errors"] == 0 and first["events_applied"] == 7
    sw = await session.get(SeedWorld, WORLD)
    assert sw is not None and sw.last_seq == 7
    wo = await session.get(WorkOrder, WO)
    assert wo is not None and wo.status == "ON_HOLD"

    second = await _replay(session, root, datetime(2026, 7, 10, tzinfo=UTC))
    assert second["events_resumed_from_seq"] == 8
    assert second["line_errors"] == 0 and second["events_applied"] == 7
    await session.refresh(wo)
    assert wo.status == "CLOSED"
    n_notes = await session.scalar(select(func.count()).select_from(WorkOrderNote))
    assert n_notes == 1  # 未重放

    # 同 cut-off 再跑 = no-op
    third = await _replay(session, root, datetime(2026, 7, 10, tzinfo=UTC))
    assert third["events_applied"] == 0 and third["events_resumed_from_seq"] == len(
        _ledger_events()) + 1


async def test_state_mismatch_and_until_regressed_reject(session, tmp_path: Path) -> None:
    root = write_ledger(tmp_path / "w", _ledger_events())
    await _replay(session, root, _z("2026-07-03T02:00:00Z"))

    with pytest.raises(SeedRejected) as e:
        await _replay(session, root, _z("2026-07-03T01:00:00Z"))
    assert e.value.code == "until_regressed"

    other = write_ledger(tmp_path / "v", _ledger_events(),
                         manifest_patch={"generator_version": "test-gen-2"})
    with pytest.raises(SeedRejected) as e:
        await _replay(session, other, datetime(2026, 7, 10, tzinfo=UTC))
    assert e.value.code == "state_mismatch"
    sw = await session.get(SeedWorld, WORLD)
    assert sw is not None and sw.generator_version == "test-gen"  # 拒讀不改指紋


async def test_seat_level_wo_folds_to_owner_and_null_station_cls_unclassified(
    session, tmp_path: Path
) -> None:
    """Board 裁決:座位 id 工單落到所屬機台;station_cls null 的資產以 UNCLASSIFIED 建立。"""
    owner, jig = "EID-97001", "EID-97003"
    evs = [
        _define(1, owner),
        _define(2, jig, station_cls=None),
        _open(3, "2026-07-03T01:00:00Z", WO, f"{owner}-N2"),
        _status(4, "2026-07-03T01:10:00Z", WO, f"{owner}-N2", "OPEN", "IN_PROGRESS"),
    ]
    summary = await _replay(session, write_ledger(tmp_path / "w", evs),
                            datetime(2026, 7, 10, tzinfo=UTC))
    assert summary["line_errors"] == 0, summary
    assert summary["folded"] == {"seat_folded_to_owner": 1}
    assert summary["skipped_features"] == {"asset.define.station_cls:null": 1}
    wo = await session.get(WorkOrder, WO)
    assert wo is not None and wo.asset_id == owner and wo.status == "IN_PROGRESS"
    assert wo.brief_description == "feeder jam"  # 不改寫
    a = await session.get(Asset, jig)
    assert a is not None and a.asset_type == "UNCLASSIFIED"
    t = await session.get(AssetType, "UNCLASSIFIED")
    assert t is not None and t.label == "Unclassified (no station class in source)"
