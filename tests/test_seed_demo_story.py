"""scripts/seed_demo_story.py:形象片固定劇本(EID-97061、WO-96320 / 96321)。

純函式層(事件表、`--until` 篩選、high-water `t_max` 篩選)不需 DB,永遠跑。
DB 整合(testcontainers)驗逐步往後重放 + 冪等;本機無 Docker 時 skip(docker_daemon fixture)。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "seed_demo_story.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("seed_demo_story_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


story = _load_script()
TPE = timezone(timedelta(hours=8))


def _t(mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(2026, mo, d, h, mi, tzinfo=TPE)


# ---------------------------------------------------------------- 事件表


def test_event_table_is_time_ordered_per_wo_and_opens_first() -> None:
    for wo in story.STORY_WOS:
        evs = story.events_for(wo)
        assert evs == [e for e in story.STORY_EVENTS if e.wo == wo]  # 表列順序 = 時間順序
        assert [e.at for e in evs] == sorted(e.at for e in evs)
        assert evs[0].kind == "open"
        assert all(e.at.tzinfo is not None for e in evs)


def test_no_two_state_events_share_a_timestamp() -> None:
    """t_max 篩選對狀態事件用嚴格 `>`,前提是同一張單不會有兩個狀態事件同刻。"""
    for wo in story.STORY_WOS:
        ats = [e.at for e in story.events_for(wo) if not e.is_note]
        assert len(ats) == len(set(ats))


def test_story_timeline_matches_script() -> None:
    a = story.events_for(96320)
    assert [(e.kind, e.at) for e in a] == [
        ("open", _t(9, 18, 10, 12)), ("note", _t(9, 18, 10, 12)),
        ("start", _t(9, 18, 10, 40)), ("note", _t(9, 18, 10, 40)),
        ("complete", _t(9, 18, 12, 5)), ("note", _t(9, 18, 12, 5)),
        ("close", _t(9, 18, 12, 10)),
    ]
    assert a[0].text == "Breakdown: sensor fault"
    assert a[0].failure_started_at == _t(9, 18, 9, 50)

    b = story.events_for(96321)
    assert [(e.kind, e.at) for e in b] == [
        ("open", _t(10, 1, 14, 58)), ("note", _t(10, 1, 14, 58)),
        ("start", _t(10, 1, 15, 21)), ("note", _t(10, 1, 15, 21)),
        ("complete", _t(10, 1, 19, 34)), ("note", _t(10, 1, 19, 34)),
    ]
    assert b[0].text == "Breakdown: temperature out of range"
    assert b[0].failure_started_at == _t(10, 1, 14, 17)
    # 旁白綁定的那句,逐字
    assert b[-1].text == "Repair complete: reset controller and verified."
    assert [e.entry_type for e in b if e.is_note] == ["report", "progress", "progress"]


def test_failure_starts_strictly_before_open() -> None:
    for e in story.STORY_EVENTS:
        if e.kind == "open":
            assert e.failure_started_at is not None and e.failure_started_at < e.at


def test_note_keys_are_unique() -> None:
    keys = [e.note_key() for e in story.STORY_EVENTS if e.is_note]
    assert len(keys) == len(set(keys))


# ---------------------------------------------------------------- --until


def test_parse_until_naive_is_taipei_and_aware_is_kept() -> None:
    assert story.parse_until("2026-10-01T14:59") == _t(10, 1, 14, 59)
    utc = story.parse_until("2026-10-01T06:59:00+00:00")
    assert utc == _t(10, 1, 14, 59)
    assert story.parse_until(None) is None
    assert story.parse_until("  ") is None
    with pytest.raises(story.StoryError):
        story.parse_until("yesterday")


def test_until_filters_inclusive() -> None:
    b = story.events_for(96321)
    pend = story.select_pending(b, until=_t(10, 1, 14, 59), t_max=None)
    assert [e.kind for e in pend] == ["open", "note"]
    # until 剛好等於事件時刻 → 含
    pend = story.select_pending(b, until=_t(10, 1, 15, 21), t_max=None)
    assert [e.kind for e in pend] == ["open", "note", "start", "note"]
    assert story.count_beyond(b, _t(10, 1, 15, 22)) == 2
    assert story.count_beyond(b, None) == 0
    # until 早於開單 → 什麼都不做(96321 在 F06 前不存在也合法)
    assert story.select_pending(b, until=_t(10, 1, 14, 0), t_max=None) == []
    assert len(story.select_pending(b, until=None, t_max=None)) == len(b)


# ---------------------------------------------------------------- t_max(只往後)


def test_t_max_replays_only_later_events() -> None:
    b = story.events_for(96321)
    done = frozenset(e.note_key() for e in b[:2])
    pend = story.select_pending(b, until=_t(10, 1, 15, 22), t_max=_t(10, 1, 14, 58),
                                done_note_keys=done)
    assert [(e.kind, e.at) for e in pend] == [
        ("start", _t(10, 1, 15, 21)), ("note", _t(10, 1, 15, 21))]
    # 同 until 重跑:high-water 已到 15:21 → 零事件
    done = frozenset(e.note_key() for e in b[:4])
    assert story.select_pending(b, until=_t(10, 1, 15, 22), t_max=_t(10, 1, 15, 21),
                                done_note_keys=done) == []
    # 再往後到完整
    pend = story.select_pending(b, until=None, t_max=_t(10, 1, 15, 21), done_note_keys=done)
    assert [e.kind for e in pend] == ["complete", "note"]


def test_t_max_retries_same_instant_note_after_interruption() -> None:
    """轉移已落地、同刻日誌未落地(兩交易間中斷)→ 只補日誌,不重做轉移。"""
    b = story.events_for(96321)
    done = frozenset({b[1].note_key()})  # 14:58 report 已落地;15:21 start 已落地、note 未落地
    pend = story.select_pending(b, until=None, t_max=_t(10, 1, 15, 21), done_note_keys=done)
    assert [(e.kind, e.at) for e in pend] == [
        ("note", _t(10, 1, 15, 21)), ("complete", _t(10, 1, 19, 34)),
        ("note", _t(10, 1, 19, 34))]


# ---------------------------------------------------------------- DB 整合(testcontainers)


@pytest.fixture
async def session(docker_daemon):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from testcontainers.postgres import PostgresContainer

    from cmms.audit import Actor
    from cmms.db import Base
    from cmms.domain.asset import models as _asset_models  # noqa: F401
    from cmms.domain.asset.loader import load as load_assets
    from cmms.domain.identity import models as _identity_models  # noqa: F401
    from cmms.domain.inventory import models as _inv_models  # noqa: F401
    from cmms.domain.notify import models as _notify_models  # noqa: F401
    from cmms.domain.pm_schedule import models as _pm_models  # noqa: F401
    from cmms.domain.work_order import models as _wo_models  # noqa: F401
    from cmms.domain.work_order.loader import WO_STATUS_SEED
    from cmms.domain.work_order.models import WoNoteType, WorkType
    from cmms.domain.work_order.service import WorkOrderService

    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await load_assets(
                [{"compid": story.STORY_ASSET, "comp_desc": "UV Oven #1",
                  "assettype": "Facility", "assetsubtp": "OVEN", "department": "EQ",
                  "line_no": "Zone B", "available": "Yes"}],
                s,
            )
            svc = WorkOrderService(s)
            async with svc.write(Actor.human("tester")):  # create_all 不含 migration 的 lookup 列
                await svc.upsert_lookup(WorkType, "REACTIVE", "Reactive")
                for code, label, rank, is_terminal, is_downtime in WO_STATUS_SEED:
                    await svc.upsert_status(
                        code, label, rank=rank, is_terminal=is_terminal, is_downtime=is_downtime
                    )
                for code in ("report", "progress"):
                    await svc.upsert_lookup(WoNoteType, code, code)
            yield s
        await engine.dispose()


async def test_stepwise_forward_replay_and_idempotency(session) -> None:
    from cmms.domain.work_order.service import WorkOrderService

    s1 = await story.replay_story(session, _t(10, 1, 14, 59))
    assert s1["work_orders"]["WO-96320"] == {
        "applied": 7, "skipped": 0, "after_until": 0, "status": "CLOSED"}
    assert s1["work_orders"]["WO-96321"] == {
        "applied": 2, "skipped": 0, "after_until": 4, "status": "OPEN"}

    s2 = await story.replay_story(session, _t(10, 1, 15, 22))
    assert s2["work_orders"]["WO-96320"]["applied"] == 0
    assert s2["work_orders"]["WO-96321"] == {
        "applied": 2, "skipped": 2, "after_until": 2, "status": "IN_PROGRESS"}

    s3 = await story.replay_story(session, _t(10, 1, 15, 22))
    assert s3["applied"] == 0

    s4 = await story.replay_story(session, None)
    assert s4["work_orders"]["WO-96321"] == {
        "applied": 2, "skipped": 4, "after_until": 0, "status": "COMPLETED"}
    assert (await story.replay_story(session, None))["applied"] == 0

    svc = WorkOrderService(session)
    wo = await svc.get_work_order(96321)
    assert wo.opened_at == _t(10, 1, 14, 58)
    assert wo.failure_started_at == _t(10, 1, 14, 17)
    hist = await svc.get_status_history(96321)
    assert [(h.to_status, h.changed_at) for h in hist] == [
        ("OPEN", _t(10, 1, 14, 58)), ("IN_PROGRESS", _t(10, 1, 15, 21)),
        ("COMPLETED", _t(10, 1, 19, 34))]
    notes = await svc.list_notes(96321)
    assert [n.body for n in notes] == [e.text for e in story.events_for(96321) if e.is_note]

    # 倒退 → 拒(只往後)
    with pytest.raises(story.StoryError, match="until_regressed"):
        await story.replay_story(session, _t(10, 1, 14, 59))
