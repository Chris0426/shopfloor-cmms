#!/usr/bin/env python
"""Leave two PENDING proposals in the queue so /admin/proposals is worth looking at.

This is the governance centrepiece: a write that an agent (or a non-admin human)
wants to make does not happen — it becomes a `pending_proposal` row with a dry-run
diff, and an admin has to confirm it. The two proposals seeded here are:

  1. agent:assistant  -> close_work_order   (the agent CAN read everything and
                         proposes a close; it cannot execute it, and it cannot
                         confirm its own proposal)
  2. human:jordan.lee -> void_work_order    (an engineer flags a work order that
                         was filed by mistake; only an admin may void it)

Both are executed (or rejected) by a human admin in /admin/proposals.

Idempotent: propose() returns the existing pending proposal for the same target.
"""

from __future__ import annotations

import asyncio

from cmms.audit import Actor
from cmms.db import get_sessionmaker
from cmms.domain.work_order.service import WorkOrderService

AGENT = Actor.agent("assistant")
ENGINEER = Actor.human("jordan.lee")


async def main() -> None:
    async with get_sessionmaker()() as session:
        svc = WorkOrderService(session)

        # An in-progress breakdown the agent believes is finished (WO 20301 from the
        # live scenario: the part has been issued and fitted).
        p1 = await svc.propose(
            operation="close_work_order",
            params={
                "work_order_no": 20301,
                "action_taken": (
                    "Replaced the X-axis home sensor (2 x EC000002); "
                    "re-homed and ran three dry cycles - all passed."
                ),
            },
            proposed_by=AGENT,
            idempotency_key="demo:close:20301",
        )
        print(f"proposal {p1.pending_token[:12]}... close_work_order 20301 (by {p1.proposed_by})")

        # A duplicate report the engineer wants voided (WO 20303, filed by the operator).
        p2 = await svc.propose(
            operation="void_work_order",
            params={"work_order_no": 20303, "reason": "Duplicate of an existing oven report."},
            proposed_by=ENGINEER,
            idempotency_key="demo:void:20303",
        )
        print(f"proposal {p2.pending_token[:12]}... void_work_order 20303 (by {p2.proposed_by})")

    print("2 proposals pending - confirm or reject them in /admin/proposals as admin")


if __name__ == "__main__":
    asyncio.run(main())
