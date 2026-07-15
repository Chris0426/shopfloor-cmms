#!/usr/bin/env python
"""Give every demo asset an owner (maintenance engineer responsible for it).

Owners matter for the demo: filing a breakdown auto-fills the assignee from the
asset's owner list, and the "Mine" filter on the work-order queue is driven by it.
There is no CLI for this (it is normally done in /admin/owners), so this script
calls the domain service directly — still the single write path, no raw SQL.

Deterministic. Idempotent (set_owners is a no-op when nothing changes).
Requires the `admin` account to exist: owner assignment is admin-only, enforced
in the domain layer.

    python scripts/assign_demo_owners.py
"""

from __future__ import annotations

import asyncio

from cmms.audit import Actor
from cmms.db import get_sessionmaker
from cmms.domain.asset.service import AssetService

ADMIN = Actor.human("admin")

# Jordan Lee is the engineer persona used in the demo walkthrough, so he owns a
# healthy share of the fleet (including the three machines the live scenario uses).
OWNERS = ["Jordan Lee", "Sam Wu", "Alice Fang", "Ben Yeh", "Cara Lo"]


def owners_for(index: int) -> list[str]:
    """Owner list for asset #index (EID-10001 + index). Every 7th machine is shared."""
    primary = "Jordan Lee" if index % 5 in (0, 3) else OWNERS[index % len(OWNERS)]
    if index % 7 == 6:  # a few machines have two responsible engineers (0031 multi-owner)
        second = OWNERS[(index + 2) % len(OWNERS)]
        if second != primary:
            return [primary, second]
    return [primary]


async def main() -> None:
    assigned = 0
    async with get_sessionmaker()() as session:
        svc = AssetService(session)
        for i in range(60):  # EID-10001 .. EID-10060 (see generate_demo_data.py)
            await svc.set_owners(f"EID-{10001 + i:05d}", owners_for(i), ADMIN)
            assigned += 1
    print(f"assigned owners on {assigned} assets")


if __name__ == "__main__":
    asyncio.run(main())
