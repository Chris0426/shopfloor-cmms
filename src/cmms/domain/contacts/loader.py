"""contacts.csv 載入器(migration 資料輸入)。

經 ContactsService 單一寫入路徑寫入(ADR-001),idempotent(可重跑)。
★ 編碼 latin-1(ß/ü + `&rsquo;`);全 237 列欄位數一致(無畸形行)。
- organization 由 distinct company 建;website/address/phone 取該 org 內 contactid 最小者的
  非空值(deterministic 聚合)。org_type 推導 + CMB 種子(歷史承包商,不建人員)。
- person 由各列建;PERSON_ALIASES 的別名 contactid 不建 person、改寫入 person_alias
  (僅當 canonical 也在本批時才視為別名,否則照常建 person,不遺失)。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.contacts.models import ContactCategory, OrgType
from cmms.domain.contacts.service import ContactsService
from cmms.domain.contacts.transform import (
    CONTACT_CATEGORY_LABELS,
    ORG_TYPE_LABELS,
    PERSON_ALIASES,
    SEED_ORGANIZATIONS,
    OrganizationImport,
    ParsedContactRow,
    derive_org_type,
    row_to_contact,
)

MIGRATION_ACTOR = Actor.human("migration")


@dataclass(frozen=True, slots=True)
class LoadResult:
    organizations: int  # 含 CMB 等種子
    persons: int  # 已扣除別名
    aliases: int  # 寫入 person_alias 的別名數
    org_types: int  # 種子的 org_type lookup 數
    contact_categories: int  # 種子的 contact_category lookup 數
    seeded_orgs: int  # 不在 CSV、由 SEED_ORGANIZATIONS 補的組織數(如 CMB)


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 contacts.csv(latin-1)。以表頭名對應欄位。"""
    with path.open(encoding="latin-1", newline="") as fh:
        return list(csv.DictReader(fh))


def _build_organizations(parsed: list[ParsedContactRow]) -> tuple[list[OrganizationImport], int]:
    """distinct company → OrganizationImport;website/address/phone 取 contactid 最小者非空值。

    回 (organizations, seeded_count)。seeded = 不在 CSV、由 SEED_ORGANIZATIONS 補的數量。
    """
    # 以 contactid 排序 → 聚合「先到先得」即 contactid 最小者,deterministic。
    ordered = sorted(parsed, key=lambda p: p.person.person_id)
    orgs: dict[str, dict] = {}
    for p in ordered:
        o = orgs.get(p.person.org_id)
        if o is None:
            orgs[p.person.org_id] = {
                "org_id": p.person.org_id,
                "name": p.company,
                "org_type": derive_org_type(p.company, p.category),
                "is_active": True,
                "website": p.website,
                "address": p.address,
                "phone": p.phone,
            }
        else:
            # 補 first non-null(維持 contactid 最小者優先)
            o["website"] = o["website"] or p.website
            o["address"] = o["address"] or p.address
            o["phone"] = o["phone"] or p.phone

    seeded = 0
    for org_id, name, org_type, is_active in SEED_ORGANIZATIONS:
        if org_id not in orgs:  # 不覆寫 CSV 既有
            orgs[org_id] = {
                "org_id": org_id,
                "name": name,
                "org_type": org_type,
                "is_active": is_active,
                "website": None,
                "address": None,
                "phone": None,
            }
            seeded += 1

    return [OrganizationImport(**o) for o in orgs.values()], seeded


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    parsed: list[ParsedContactRow] = [row_to_contact(r) for r in rows]
    organizations, seeded = _build_organizations(parsed)

    all_ids = {p.person.person_id for p in parsed}
    # 只有當 canonical 也在本批時,才把別名轉成 alias;否則別名照常建 person(不遺失資料)。
    effective_aliases = {a: c for a, c in PERSON_ALIASES.items() if c in all_ids}
    persons = [p.person for p in parsed if p.person.person_id not in effective_aliases]

    service = ContactsService(session)
    async with service.write(MIGRATION_ACTOR):
        for code, label in ORG_TYPE_LABELS.items():  # 完整 enum 種子(含 label,ADR-007)
            await service.upsert_lookup(OrgType, code, label)
        for code, label in CONTACT_CATEGORY_LABELS.items():
            await service.upsert_lookup(ContactCategory, code, label)
        for org in organizations:
            await service.upsert_organization(org, MIGRATION_ACTOR)
        for person in persons:
            await service.upsert_person(person, MIGRATION_ACTOR)
        for alias_id, canonical in sorted(effective_aliases.items()):
            await service.upsert_alias(alias_id, canonical, MIGRATION_ACTOR)

    return LoadResult(
        organizations=len(organizations),
        persons=len(persons),
        aliases=len(effective_aliases),
        org_types=len(ORG_TYPE_LABELS),
        contact_categories=len(CONTACT_CATEGORY_LABELS),
        seeded_orgs=seeded,
    )
