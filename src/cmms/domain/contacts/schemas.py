"""Contacts 讀取 DTO(pydantic v2)。API / MCP 回傳這些,不直接吐 ORM 物件。

人員 PII(email/phone/mobile/address)依 06-contacts §3 治理:
- 批次列舉(REST `list` + MCP):`PersonSummary` 僅非 PII(id/姓名/org/category)。
- 單筆查詢(REST/MCP get,targeted):`PersonRead` 完整(含聯絡 PII)。
組織非 PII → 一律完整。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    org_id: str
    name: str
    org_type: str | None
    is_active: bool
    website: str | None
    address: str | None
    phone: str | None


class PersonSummary(BaseModel):
    """非 PII 摘要(批次列舉用,06-contacts §3)。"""

    model_config = ConfigDict(from_attributes=True)

    person_id: str
    org_id: str
    category: str | None
    full_name: str | None


class PersonRead(PersonSummary):
    """完整人員(含聯絡 PII;單筆 targeted 查詢)。"""

    first_name: str | None
    last_name: str | None
    email: str | None
    work_phone: str | None
    extension: str | None
    mobile: str | None
    work_address: str | None
    is_main: bool = False  # 主要聯絡人(非 PII;admin 編輯面用,ADR-026)


class OrganizationDetail(OrganizationRead):
    """組織 + 其人員(非 PII 摘要)。"""

    persons: list[PersonSummary]
