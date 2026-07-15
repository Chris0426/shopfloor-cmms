"""contacts.csv → 匯入資料的純函式(無 DB,可單元測試)。

loader 以 **latin-1** 讀檔(ß/ü);此處 `clean_text` 再做 `html.unescape`(`&rsquo;` 等)。
全 237 列欄位數一致(無 RFC-4180 畸形,異於 inventory)。
- `org_slug`:company 原文 → 穩定 org_id(deterministic;`CMA`→`CMA`、`SF`→`SF`)。
- `derive_org_type`:名稱 override(SF→Internal)優先,否則 category 推導。
- `PERSON_ALIASES`:保守去重(只同公司同人;Jordan 拍板),別名 contactid → canonical。
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

# 保守去重(2026-06-22 profiling + Jordan 拍板「保守」):只併「同公司、近乎一模一樣」的重複,
# canonical 取字母序在前的 contactid。跨公司同 email(Group B)與僅同名(Group C)**不併**。
# 詳見 06-contacts §5。
PERSON_ALIASES: dict[str, str] = {
    "SMWU": "SAMWU99",  # Sam Wu @ CMA(email 僅大小寫不同 sam-wu@ / Sam-Wu@)
    "NOPT": "NOPTIC",  # Rob Miller @ Nordic Optics Ltd.(email + phone 全同)
}

# category(eMaint 原始分類)→ org_type(模型化組織類型)。
ORG_TYPE_BY_CATEGORY: dict[str, str] = {
    "Supplier": "Supplier",
    "Employee": "Contractor",  # 此 CMMS 的「Employee」= 外包操作人員(現皆 CMA)
    "Customer": "Customer",
}
# 名稱 override:SF 的 contacts category=Customer,但 SF = Shopfloor 自有 → Internal。
ORG_TYPE_OVERRIDES: dict[str, str] = {
    "SF": "Internal",
}

# lookup 種子(完整 enum;含 label,ADR-007)。
ORG_TYPE_LABELS: dict[str, str] = {
    "Supplier": "Supplier(零件/物料供應商)",
    "Contractor": "Contractor(維護/製造外包承包商)",
    "Customer": "Customer(客戶)",
    "Internal": "Internal(Shopfloor 自有)",
}
CONTACT_CATEGORY_LABELS: dict[str, str] = {
    "Supplier": "Supplier(供應商窗口)",
    "Employee": "Employee(CMMS 操作使用者;現由 CMA 外包)",
    "Customer": "Customer(eMaint 原始分類;實證多為 Shopfloor 內部人員)",
}

# 種子:歷史承包商(合約終止,不在 contacts.csv;供舊工單 assignto 解析,不建人員)。
# (org_id, name, org_type, is_active)
SEED_ORGANIZATIONS: list[tuple[str, str, str, bool]] = [
    ("CMB", "CMB", "Contractor", False),
]

_SLUG_RE = re.compile(r"[^A-Z0-9]+")


def org_slug(company: str) -> str:
    """company 原文 → 穩定的 org_id slug(大寫、非英數→底線、截斷 40)。

    實證 211 家公司零碰撞;`CMA`→`CMA`、`SF`→`SF`(特例自然落位)。
    """
    s = _SLUG_RE.sub("_", company.strip().upper()).strip("_")
    return s[:40] or "ORG"


def derive_org_type(company: str, category: str | None) -> str | None:
    """組織類型:名稱 override 優先,否則由 category 推導(實證無公司跨 category)。"""
    if company in ORG_TYPE_OVERRIDES:
        return ORG_TYPE_OVERRIDES[company]
    if category is None:
        return None
    return ORG_TYPE_BY_CATEGORY.get(category)


def clean(value: str | None) -> str | None:
    """去空白;空字串 / `nan` 視為 None。"""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() == "nan":
        return None
    return v


def clean_text(value: str | None) -> str | None:
    """文字欄:去空白 + HTML-entity 還原(`&rsquo;`→’);latin-1 解碼已在 loader 讀檔完成。"""
    v = clean(value)
    return html.unescape(v) if v is not None else None


@dataclass(frozen=True, slots=True)
class PersonImport:
    """一筆待匯入人員(contacts.csv 提供的欄位;home_address [DROP] 不在此)。"""

    person_id: str
    org_id: str
    category: str | None
    first_name: str | None
    last_name: str | None
    full_name: str | None
    email: str | None
    work_phone: str | None
    extension: str | None
    mobile: str | None
    work_address: str | None


@dataclass(frozen=True, slots=True)
class OrganizationImport:
    """一筆待匯入組織(由 distinct company 萃取 + 推導)。"""

    org_id: str
    name: str
    org_type: str | None
    is_active: bool
    website: str | None
    address: str | None
    phone: str | None


@dataclass(frozen=True, slots=True)
class ParsedContactRow:
    """單列解析:person 本體 + 其 company/category 與 org 級聯絡值(供 loader 建/聚合 org)。"""

    person: PersonImport
    company: str
    category: str | None
    website: str | None  # wweb(供 org 聚合)
    address: str | None  # waddress(供 org 聚合)
    phone: str | None  # wphone(供 org 聚合)


def row_to_contact(row: dict[str, str | None]) -> ParsedContactRow:
    """單列 CSV(dict,鍵為表頭)→ ParsedContactRow。"""
    contact_id = clean(row.get("contactid"))
    if not contact_id:
        raise ValueError("row missing contactid (person_id)")
    company = clean_text(row.get("company"))
    if not company:
        raise ValueError(f"contact {contact_id}: missing company")
    category = clean(row.get("category"))
    org_id = org_slug(company)

    website = clean(row.get("wweb"))
    address = clean_text(row.get("waddress"))
    phone = clean(row.get("wphone"))

    person = PersonImport(
        person_id=contact_id,
        org_id=org_id,
        category=category,
        first_name=clean_text(row.get("fname")),
        last_name=clean_text(row.get("lname")),
        full_name=clean_text(row.get("fullname")),
        email=clean(row.get("email")),
        work_phone=phone,
        extension=clean(row.get("ext")),
        mobile=clean(row.get("mobile")),
        work_address=address,
    )
    return ParsedContactRow(
        person=person,
        company=company,
        category=category,
        website=website,
        address=address,
        phone=phone,
    )
