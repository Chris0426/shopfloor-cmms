"""Contacts 讀取 API(thin client,只呼叫 ContactsService)。寫入留待後續切片。

人員 PII 治理(06-contacts §3):批次列舉(list)只回非 PII 摘要;單筆查詢(get)回完整,
且別名 contactid(如 SMWU)自動解析回 canonical。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.api.deps import get_session
from cmms.domain.contacts.schemas import (
    OrganizationDetail,
    OrganizationRead,
    PersonRead,
    PersonSummary,
)
from cmms.domain.contacts.service import ContactsService

router = APIRouter(tags=["contacts"])


@router.get("/organizations", response_model=list[OrganizationRead])
async def list_organizations(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    org_type: str | None = None,
    is_active: bool | None = None,
    search: str | None = Query(None, description="公司名稱關鍵字(ilike)"),
) -> list[OrganizationRead]:
    orgs = await ContactsService(session).list_organizations(
        limit=limit, offset=offset, org_type=org_type, is_active=is_active, search=search
    )
    return [OrganizationRead.model_validate(o) for o in orgs]


@router.get("/organizations/{org_id}", response_model=OrganizationDetail)
async def get_organization(
    org_id: str, session: AsyncSession = Depends(get_session)
) -> OrganizationDetail:
    service = ContactsService(session)
    org = await service.get_organization(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail=f"organization {org_id} not found")
    persons = await service.list_org_persons(org_id)
    return OrganizationDetail(
        **OrganizationRead.model_validate(org).model_dump(),
        persons=[PersonSummary.model_validate(p) for p in persons],
    )


@router.get("/persons", response_model=list[PersonSummary])
async def list_persons(
    session: AsyncSession = Depends(get_session),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    org_id: str | None = None,
    category: str | None = None,
    search: str | None = Query(None, description="姓名關鍵字(ilike)"),
) -> list[PersonSummary]:
    """人員列舉只回非 PII 摘要(06-contacts §3);完整 PII 經單筆查詢。"""
    persons = await ContactsService(session).list_persons(
        limit=limit, offset=offset, org_id=org_id, category=category, search=search
    )
    return [PersonSummary.model_validate(p) for p in persons]


@router.get("/persons/{person_id}", response_model=PersonRead)
async def get_person(person_id: str, session: AsyncSession = Depends(get_session)) -> PersonRead:
    """單筆人員(含 PII);別名 contactid(如 SMWU)自動解析回 canonical。"""
    person = await ContactsService(session).resolve_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail=f"person {person_id} not found")
    return PersonRead.model_validate(person)
