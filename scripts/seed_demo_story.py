#!/usr/bin/env python
"""Replay the film's fixed story onto the demo database: UV Oven #1 and two work orders.

The asset (EID-97061, Zone B) comes from scripts/generate_demo_data.py; this script
only writes the two work orders that hang on it, with timestamps pinned to the story:

  WO-96320  2026-09-18  an earlier sensor fault, already CLOSED (the oven's history)
  WO-96321  2026-10-01  the film's repair: reported 14:58, started 15:21,
                        COMPLETED 19:34 - left waiting for a close proposal

Every write goes through WorkOrderService with explicit `at` / `failure_started_at` /
`occurred_at` (single write path; the audit columns are filled as usual). No raw SQL.

    python scripts/seed_demo_story.py [--until 2026-10-01T14:59]

`--until` (ISO; a naive value is read as Asia/Taipei) replays only events at or before
that instant; omitted = the whole story.

Idempotent and forward-only: for a work order that already exists, the newest
status-history / note timestamp is its high-water mark, and only events after it are
replayed. So one database can be stepped `--until 14:59` -> `--until 15:22` -> full,
and re-running the same `--until` writes nothing. An `--until` earlier than what is
already in the database is an error (there is no rewind).

Prints a one-line JSON summary last; exits non-zero on any failure.
Database: CMMS_DATABASE_URL (same as every other script).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# 廠區時區 Asia/Taipei(UTC+8,無 DST)。用固定位移而非 ZoneInfo:python:3.12-slim 映像
# 沒有系統 tzdata(同 cmms.domain.work_order.transform.TAIPEI 的作法)。
TPE = timezone(timedelta(hours=8))

STORY_ASSET = "EID-97061"  # UV Oven #1(generate_demo_data.py 的第 61 台)

# 動作者(demo 帳號,seed_demo.sh 4/5 建立)。記為 human:<id>。
OPERATOR = "operator.a"
ENGINEER = "jordan.lee"
ADMIN = "admin"
# 負責人顯示名(= jordan.lee 帳號的 emaint-assignee)。EID-97061 不在
# assign_demo_owners.py 的 60 台內,沒有設備負責人可衍生,所以開單時明確指派。
ENGINEER_NAME = "Jordan Lee"


def _tpe(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=TPE)


@dataclass(frozen=True, slots=True)
class StoryEvent:
    """劇本的一個事件。`kind` ∈ open / note / start / complete / close。

    note 的 `entry_type` 取 wo_note_type 受控詞彙(migration 0012):Report 日誌 = `report`
    (初始報修),Progress 日誌與完工日誌 = `progress`(進度 / 處理紀錄)。
    """

    wo: int
    at: datetime
    kind: str
    user: str
    text: str | None = None  # open = brief;note = 日誌內文
    entry_type: str | None = None
    failure_started_at: datetime | None = None

    @property
    def is_note(self) -> bool:
        return self.kind == "note"

    def note_key(self) -> str:
        """日誌的冪等鍵(ADR-006);中斷重跑時,同刻的日誌以此判斷是否已落地。"""
        return f"demo-story:{self.wo}:{self.entry_type}:{self.at.isoformat()}"


# 事件表 = 劇本 §2(文字逐字)。同一時刻的事件依表列順序執行(先轉移、後日誌)。
STORY_EVENTS: tuple[StoryEvent, ...] = (
    # ---- WO-96320:舊的感測器故障單(已結)
    StoryEvent(
        96320, _tpe(2026, 9, 18, 10, 12), "open", OPERATOR,
        text="Breakdown: sensor fault",
        failure_started_at=_tpe(2026, 9, 18, 9, 50),
    ),
    StoryEvent(
        96320, _tpe(2026, 9, 18, 10, 12), "note", OPERATOR, entry_type="report",
        text="Operator reported the chamber temperature sensor reading stuck at 25 °C.",
    ),
    StoryEvent(96320, _tpe(2026, 9, 18, 10, 40), "start", ENGINEER),
    StoryEvent(
        96320, _tpe(2026, 9, 18, 10, 40), "note", ENGINEER, entry_type="progress",
        text="Replaced the chamber temperature sensor; running a heat-up check.",
    ),
    StoryEvent(96320, _tpe(2026, 9, 18, 12, 5), "complete", ENGINEER),
    StoryEvent(
        96320, _tpe(2026, 9, 18, 12, 5), "note", ENGINEER, entry_type="progress",
        text="Sensor replaced and verified over a full cure cycle.",
    ),
    StoryEvent(96320, _tpe(2026, 9, 18, 12, 10), "close", ADMIN),
    # ---- WO-96321:本片主線的修理單(停在 COMPLETED,等結案提案)
    StoryEvent(
        96321, _tpe(2026, 10, 1, 14, 58), "open", OPERATOR,
        text="Breakdown: temperature out of range",
        failure_started_at=_tpe(2026, 10, 1, 14, 17),
    ),
    StoryEvent(
        96321, _tpe(2026, 10, 1, 14, 58), "note", OPERATOR, entry_type="report",
        text="Operator reported temperature out of range.",
    ),
    StoryEvent(96321, _tpe(2026, 10, 1, 15, 21), "start", ENGINEER),
    StoryEvent(
        96321, _tpe(2026, 10, 1, 15, 21), "note", ENGINEER, entry_type="progress",
        text="Technician started work on temperature out of range.",
    ),
    StoryEvent(96321, _tpe(2026, 10, 1, 19, 34), "complete", ENGINEER),
    StoryEvent(
        96321, _tpe(2026, 10, 1, 19, 34), "note", ENGINEER, entry_type="progress",
        text="Repair complete: reset controller and verified.",
    ),
)

STORY_WOS: tuple[int, ...] = tuple(dict.fromkeys(e.wo for e in STORY_EVENTS))


class StoryError(Exception):
    """劇本無法套用(工單號被別的單佔用、--until 倒退、domain 拒絕…)。"""


# ---------------------------------------------------------------- 純函式(無 DB)


def parse_until(value: str | None) -> datetime | None:
    """`--until` 解析:ISO 字串;無時區 = Asia/Taipei。None / 空字串 = 全部。"""
    if value is None or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError as e:
        raise StoryError(f"--until is not an ISO datetime: {value!r}") from e
    return dt.replace(tzinfo=TPE) if dt.tzinfo is None else dt


def events_for(wo: int, events: Iterable[StoryEvent] = STORY_EVENTS) -> list[StoryEvent]:
    """某張單的事件,依時間排序(穩定排序:同刻保留表列順序)。"""
    return sorted((e for e in events if e.wo == wo), key=lambda e: e.at)


def select_pending(
    events: Sequence[StoryEvent],
    *,
    until: datetime | None,
    t_max: datetime | None,
    done_note_keys: frozenset[str] = frozenset(),
) -> list[StoryEvent]:
    """該重放的事件:`at <= until`(None = 不限)且 `at > t_max`(None = 工單尚不存在)。

    例外一條(中斷安全):與 `t_max` **同刻**的日誌若其冪等鍵尚未落地,也重放 ——
    轉移與它的日誌是兩個交易,轉移落地、日誌未落地時 `t_max` 已等於該刻,嚴格 `>` 會永久漏掉
    那筆日誌。同一張單不會有兩個狀態事件同刻(test 守住),所以狀態事件嚴格 `>` 即精確。
    """
    out = []
    for e in events:
        if until is not None and e.at > until:
            continue
        later = t_max is None or e.at > t_max
        unfinished_note = e.is_note and e.at == t_max and e.note_key() not in done_note_keys
        if later or unfinished_note:
            out.append(e)
    return out


def count_beyond(events: Sequence[StoryEvent], until: datetime | None) -> int:
    """`until` 之後、本次不重放的事件數。"""
    return 0 if until is None else sum(1 for e in events if e.at > until)


# ---------------------------------------------------------------- DB 套用


async def _high_water(svc, wo_no: int) -> tuple[datetime | None, frozenset[str]]:
    """工單既有狀態的最大時間(status_history.changed_at ∪ note.occurred_at)與已落地日誌鍵。"""
    hist = await svc.get_status_history(wo_no)
    notes = await svc.list_notes(wo_no)
    stamps = [h.changed_at for h in hist] + [n.occurred_at for n in notes]
    keys = frozenset(n.idempotency_key for n in notes if n.idempotency_key)
    return (max(stamps) if stamps else None), keys


async def _apply(svc, ev: StoryEvent) -> None:
    from cmms.audit import Actor

    actor = Actor.human(ev.user)
    if ev.kind == "open":
        await svc.open_work_order(
            asset_id=STORY_ASSET,
            work_type="REACTIVE",
            actor=actor,
            brief_description=ev.text,
            opened_by=ev.user,
            assignees=[ENGINEER_NAME],
            work_order_no=ev.wo,
            at=ev.at,
            failure_started_at=ev.failure_started_at,
        )
    elif ev.kind == "note":
        await svc.add_note(
            ev.wo,
            entry_type=ev.entry_type,
            body=ev.text,
            actor=actor,
            occurred_at=ev.at,
            idempotency_key=ev.note_key(),
        )
    elif ev.kind == "start":
        await svc.start_work(ev.wo, actor, at=ev.at)
    elif ev.kind == "complete":
        await svc.complete_work(ev.wo, actor, at=ev.at)
    elif ev.kind == "close":
        await svc.close_work_order(ev.wo, actor, at=ev.at)
    else:  # pragma: no cover - 事件表只有上列 kind
        raise AssertionError(f"unknown story event kind {ev.kind}")


async def replay_story(session, until: datetime | None) -> dict:
    """把劇本套到 `session` 的 DB。回傳摘要 dict;不可套用時拋 StoryError。"""
    from cmms.domain.work_order.service import WorkOrderService

    svc = WorkOrderService(session)
    per_wo: dict[str, dict] = {}
    for wo_no in STORY_WOS:
        evs = events_for(wo_no)
        existing = await svc.get_work_order(wo_no)
        t_max: datetime | None = None
        done: frozenset[str] = frozenset()
        if existing is not None:
            if existing.asset_id != STORY_ASSET:
                raise StoryError(
                    f"work order {wo_no} already exists on {existing.asset_id}, "
                    f"not on {STORY_ASSET}; refusing to write the story over it"
                )
            t_max, done = await _high_water(svc, wo_no)
            if until is not None and t_max is not None and until < t_max:
                raise StoryError(
                    f"until_regressed: WO {wo_no} already has state up to "
                    f"{t_max.astimezone(TPE).isoformat()}, later than --until "
                    f"{until.astimezone(TPE).isoformat()} (forward-only)"
                )
        pending = select_pending(evs, until=until, t_max=t_max, done_note_keys=done)
        for ev in pending:
            try:
                await _apply(svc, ev)
            except Exception as e:
                raise StoryError(
                    f"WO {wo_no} {ev.kind} at {ev.at.isoformat()} rejected: "
                    f"{type(e).__name__}: {e}"
                ) from e
        beyond = count_beyond(evs, until)
        wo = await svc.get_work_order(wo_no)
        if wo is not None:
            await session.refresh(wo)
        per_wo[f"WO-{wo_no}"] = {
            "applied": len(pending),
            "skipped": len(evs) - len(pending) - beyond,  # 已在 DB
            "after_until": beyond,
            "status": wo.status if wo is not None else None,
        }
    return {
        "story": "film-a1",
        "asset": STORY_ASSET,
        "until": until.astimezone(TPE).isoformat() if until is not None else None,
        "applied": sum(v["applied"] for v in per_wo.values()),
        "work_orders": per_wo,
    }


async def _main(until: datetime | None) -> dict:
    from cmms.db import get_engine, get_sessionmaker

    try:
        async with get_sessionmaker()() as session:
            return await replay_story(session, until)
    finally:
        await get_engine().dispose()


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay the film story (EID-97061, WO-96320/96321).")
    ap.add_argument(
        "--until",
        default=None,
        help="ISO datetime (naive = Asia/Taipei); replay only events at or before it",
    )
    args = ap.parse_args(argv)
    try:
        until = parse_until(args.until)
        summary = asyncio.run(_main(until))
    except StoryError as e:
        print(f"seed_demo_story: {e}", file=sys.stderr)
        print(json.dumps({"story": "film-a1", "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
