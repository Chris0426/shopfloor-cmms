"""ContactsService — Contacts 切片的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:organization / person 的 get + list、別名解析(`resolve_person`)、org 的人員清單。
寫入:upsert(載入器用,idempotent);org/person 的 admin governed CRUD(create/update)。
人員 PII:依 06-contacts §3,API/MCP 批次列舉只回非 PII 摘要(見 schemas / routes / mcp)。
所有寫入經 `DomainService.write()`。
"""

from __future__ import annotations

import secrets

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.base import DomainService
from cmms.domain.contacts.models import (
    ContactCategory,
    Organization,
    OrgType,
    Person,
    PersonAlias,
)
from cmms.domain.contacts.transform import OrganizationImport, PersonImport, org_slug
from cmms.domain.identity.service import assert_active_admin


def _strip_or_none(v: str | None) -> str | None:
    """表單字串 → strip 後空字串轉 None(統一空值表徵)。"""
    v = (v or "").strip()
    return v or None


class ContactsError(Exception):
    """contacts 領域寫入錯誤(人員不存在 / 不屬該 org 等)。"""


class ContactsService(DomainService):
    # ---- 讀取(ADR-004)----

    async def get_organization(self, org_id: str) -> Organization | None:
        return await self.session.get(Organization, org_id)

    async def list_org_types(self) -> list[OrgType]:
        """機構類別受控詞彙(唯讀,ADR-004)。admin 詞彙頁純顯示。"""
        stmt = select(OrgType).order_by(OrgType.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_contact_categories(self) -> list[ContactCategory]:
        """聯絡人分類受控詞彙(唯讀;admin 新增/編輯人員的分類選項)。"""
        stmt = select(ContactCategory).order_by(ContactCategory.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_organizations(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        org_type: str | None = None,
        is_active: bool | None = None,
        search: str | None = None,
    ) -> list[Organization]:
        stmt = select(Organization)
        if org_type is not None:
            stmt = stmt.where(Organization.org_type == org_type)
        if is_active is not None:
            stmt = stmt.where(Organization.is_active == is_active)
        if search is not None:
            stmt = stmt.where(Organization.name.ilike(f"%{search}%"))
        stmt = stmt.order_by(Organization.org_id).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def get_person(self, person_id: str) -> Person | None:
        return await self.session.get(Person, person_id)

    async def resolve_person(self, contact_id: str) -> Person | None:
        """以 contactid 取 person;若為別名(SMWU/NOPT)則解析回 canonical person。"""
        person = await self.session.get(Person, contact_id)
        if person is not None:
            return person
        alias = await self.session.get(PersonAlias, contact_id)
        if alias is None:
            return None
        return await self.session.get(Person, alias.person_id)

    async def list_persons(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        org_id: str | None = None,
        category: str | None = None,
        search: str | None = None,
    ) -> list[Person]:
        stmt = select(Person)
        if org_id is not None:
            stmt = stmt.where(Person.org_id == org_id)
        if category is not None:
            stmt = stmt.where(Person.category == category)
        if search is not None:
            stmt = stmt.where(Person.full_name.ilike(f"%{search}%"))
        stmt = stmt.order_by(Person.person_id).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def list_org_persons(self, org_id: str) -> list[Person]:
        stmt = select(Person).where(Person.org_id == org_id).order_by(Person.person_id)
        return list((await self.session.scalars(stmt)).all())

    async def set_main_contact(self, org_id: str, person_id: str, actor: Actor) -> None:
        """設某機構的主要聯絡人(ADR-026;RFQ 收件人優先此人 email)。清同 org 其他 is_main、設此人。

        governed;person 不存在或不屬該 org → raise。一機構一位 main(先清後設)。
        """
        person = await self.session.get(Person, person_id)
        if person is None or person.org_id != org_id:
            raise ContactsError(f"person {person_id} not in organization {org_id}")
        async with self.write(actor):
            await self.session.execute(
                update(Person)
                .where(Person.org_id == org_id, Person.is_main.is_(True))
                .values(is_main=False, updated_by=actor.value)
            )
            person.is_main = True
            person.updated_by = actor.value
            person.source_actor = actor.value

    async def set_organization_active(
        self, org_id: str, active: bool, actor: Actor
    ) -> Organization:
        """啟用/停用供應商機構(admin-only,治理寫入)。

        停用 = **資訊性標記**(如合約終止,承 CMB 語意);**不阻擋 RFQ** —— RFQ 資格由
        「已連結供應商 org + 有聯絡 email」決定(見 ProcurementService,不查 is_active)。
        admin 限定在 domain 強制(route 藏按鈕不是授權)。冪等:同值 → no-op(不動稽核欄)。
        """
        await assert_active_admin(self.session, actor)
        org = await self.session.get(Organization, org_id)
        if org is None:
            raise ContactsError(f"organization {org_id} not found")
        if org.is_active == active:
            return org  # 冪等 no-op
        async with self.write(actor):
            org.is_active = active
            org.updated_by = actor.value
            org.source_actor = actor.value
        return org

    # ---- 主檔編輯(admin-only;Jordan 2026-07-05 #6。engineer 唯讀 —— 聯絡資料非工程主檔,
    #      不走 engineer 提案面〔那條是給 inventory 工程主檔的〕)----

    async def update_organization(
        self,
        org_id: str,
        *,
        actor: Actor,
        name: str,
        org_type: str | None,
        website: str | None,
        address: str | None,
        phone: str | None,
    ) -> Organization:
        """供應商機構主檔編輯(admin-only,治理寫入)。

        ★ `org_id`(company slug 代理鍵)唯讀,**不在此改** —— 它是 PK 且被 `person.org_id`、
        `inventory_item.supplier_org_id` FK 參照,改動會斷鏈。`name` 則**無任何 FK 參照**
        (參照都指向 org_id),更名對參照完整性安全;僅影響 autolink 的文字比對(以新名往後匹配)
        與顯示,屬預期。org_type 若給值須為既存 lookup code。
        """
        await assert_active_admin(self.session, actor)
        org = await self.session.get(Organization, org_id)
        if org is None:
            raise ContactsError(f"organization {org_id} not found")
        clean_name = (name or "").strip()
        if not clean_name:
            raise ContactsError("organization name cannot be empty")
        new_type = _strip_or_none(org_type)
        if new_type is not None and await self.session.get(OrgType, new_type) is None:
            raise ContactsError(f"org_type {new_type} not found")
        async with self.write(actor):
            org.name = clean_name
            org.org_type = new_type
            org.website = _strip_or_none(website)
            org.address = _strip_or_none(address)
            org.phone = _strip_or_none(phone)
            org.updated_by = actor.value
            org.source_actor = actor.value
        return org

    async def create_organization(
        self,
        *,
        actor: Actor,
        name: str,
        org_type: str | None = None,
        website: str | None = None,
        address: str | None = None,
        phone: str | None = None,
    ) -> Organization:
        """admin 新建供應商機構(治理寫入,非 upsert)。

        `org_id` = 由 `name` 經 `org_slug` 決定性導出的 company slug 代理鍵(PK,建立後不可變);
        名稱相近者會導出同一 slug → **判定重複並拒絕**(create≠upsert,不覆蓋既有列)。
        載入器仍走 `upsert_organization`(idempotent),不受本方法影響。org_type 給值須為既存 lookup。
        """
        await assert_active_admin(self.session, actor)
        clean_name = (name or "").strip()
        if not clean_name:
            raise ContactsError("organization name cannot be empty")
        org_id = org_slug(clean_name)
        existing = await self.session.get(Organization, org_id)
        if existing is not None:
            raise ContactsError(
                f"organization {org_id} already exists (name: {existing.name})"
            )
        new_type = _strip_or_none(org_type)
        if new_type is not None and await self.session.get(OrgType, new_type) is None:
            raise ContactsError(f"org_type {new_type} not found")
        org = Organization(
            org_id=org_id,
            name=clean_name,
            org_type=new_type,
            is_active=True,
            website=_strip_or_none(website),
            address=_strip_or_none(address),
            phone=_strip_or_none(phone),
            created_by=actor.value,
            updated_by=actor.value,
            source_actor=actor.value,
        )
        async with self.write(actor):
            self.session.add(org)
        return org

    async def _new_person_id(self) -> str:
        """為 admin 新建人員配一個合成 person_id(eMaint contactid 是自然鍵,新增者無此值)。"""
        for _ in range(10):
            pid = f"PSN-{secrets.token_hex(4).upper()}"
            if (
                await self.session.get(Person, pid) is None
                and await self.session.get(PersonAlias, pid) is None
            ):
                return pid
        raise ContactsError("could not allocate a unique person id")

    async def _clear_org_main(self, org_id: str, actor: Actor) -> None:
        """清除某 org 現有 main 標記(一機構一位 main;set/建立前先清,呼叫端在 write() 交易內)。"""
        await self.session.execute(
            update(Person)
            .where(Person.org_id == org_id, Person.is_main.is_(True))
            .values(is_main=False, updated_by=actor.value)
        )

    async def create_person(
        self,
        *,
        org_id: str,
        actor: Actor,
        full_name: str | None,
        first_name: str | None = None,
        last_name: str | None = None,
        category: str | None = None,
        email: str | None = None,
        work_phone: str | None = None,
        mobile: str | None = None,
        extension: str | None = None,
        work_address: str | None = None,
        is_main: bool = False,
    ) -> Person:
        """admin 新增聯絡人(掛既有 org;PII 治理不變 —— 明細限 admin,內部規格)。合成 person_id。"""
        await assert_active_admin(self.session, actor)
        if await self.session.get(Organization, org_id) is None:
            raise ContactsError(f"organization {org_id} not found")
        cat = _strip_or_none(category)
        if cat is not None and await self.session.get(ContactCategory, cat) is None:
            raise ContactsError(f"contact_category {cat} not found")
        person_id = await self._new_person_id()
        person = Person(
            person_id=person_id,
            org_id=org_id,
            category=cat,
            first_name=_strip_or_none(first_name),
            last_name=_strip_or_none(last_name),
            full_name=_strip_or_none(full_name),
            email=_strip_or_none(email),
            work_phone=_strip_or_none(work_phone),
            extension=_strip_or_none(extension),
            mobile=_strip_or_none(mobile),
            work_address=_strip_or_none(work_address),
            is_main=is_main,
            created_by=actor.value,
            source_actor=actor.value,
        )
        async with self.write(actor):
            if is_main:
                await self._clear_org_main(org_id, actor)
            self.session.add(person)
        return person

    async def update_person(
        self,
        person_id: str,
        *,
        actor: Actor,
        full_name: str | None,
        first_name: str | None,
        last_name: str | None,
        category: str | None,
        email: str | None,
        work_phone: str | None,
        mobile: str | None,
        extension: str | None,
        work_address: str | None,
        is_main: bool,
    ) -> Person:
        """admin 編輯聯絡人欄位(姓名/分類/聯絡 PII/is_main)。governed;is_main 沿用一機構一位。"""
        await assert_active_admin(self.session, actor)
        person = await self.session.get(Person, person_id)
        if person is None:
            raise ContactsError(f"person {person_id} not found")
        cat = _strip_or_none(category)
        if cat is not None and await self.session.get(ContactCategory, cat) is None:
            raise ContactsError(f"contact_category {cat} not found")
        async with self.write(actor):
            if is_main and not person.is_main:
                await self._clear_org_main(person.org_id, actor)
            person.full_name = _strip_or_none(full_name)
            person.first_name = _strip_or_none(first_name)
            person.last_name = _strip_or_none(last_name)
            person.category = cat
            person.email = _strip_or_none(email)
            person.work_phone = _strip_or_none(work_phone)
            person.mobile = _strip_or_none(mobile)
            person.extension = _strip_or_none(extension)
            person.work_address = _strip_or_none(work_address)
            person.is_main = is_main
            person.updated_by = actor.value
            person.source_actor = actor.value
        return person

    # ---- 寫入(經 self.write() 交易;載入器用,idempotent)----

    async def upsert_lookup(self, model: type, code: str, label: str) -> None:
        stmt = (
            pg_insert(model)
            .values(code=code, label=label)
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await self.session.execute(stmt)

    async def upsert_organization(self, data: OrganizationImport, actor: Actor) -> None:
        values = {
            "org_id": data.org_id,
            "name": data.name,
            "org_type": data.org_type,
            "is_active": data.is_active,
            "website": data.website,
            "address": data.address,
            "phone": data.phone,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("org_id", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(Organization)
            .values(**values)
            .on_conflict_do_update(index_elements=["org_id"], set_=update_cols)
        )
        await self.session.execute(stmt)

    async def upsert_person(self, data: PersonImport, actor: Actor) -> None:
        values = {
            "person_id": data.person_id,
            "org_id": data.org_id,
            "category": data.category,
            "first_name": data.first_name,
            "last_name": data.last_name,
            "full_name": data.full_name,
            "email": data.email,
            "work_phone": data.work_phone,
            "extension": data.extension,
            "mobile": data.mobile,
            "work_address": data.work_address,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("person_id", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(Person)
            .values(**values)
            .on_conflict_do_update(index_elements=["person_id"], set_=update_cols)
        )
        await self.session.execute(stmt)

    async def upsert_alias(self, alias_contact_id: str, person_id: str, actor: Actor) -> None:
        stmt = (
            pg_insert(PersonAlias)
            .values(
                alias_contact_id=alias_contact_id,
                person_id=person_id,
                source_actor=actor.value,
                created_by=actor.value,
            )
            .on_conflict_do_nothing(index_elements=["alias_contact_id"])
        )
        await self.session.execute(stmt)
