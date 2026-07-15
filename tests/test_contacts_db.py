"""Contacts 切片的 DB 整合測試(testcontainers-postgres)。

本機無 Docker 時自動 skip。驗證:載入、org/person/alias 計數、CMB 種子、別名解析、
org_type 推導(SF→Internal、CMA→Contractor、Supplier、CMB 停用)、保守去重(Group B 保留)、
PII 摘要 vs 完整、過濾、idempotent。
"""

from __future__ import annotations

import pytest

pytest.importorskip("testcontainers.postgres")
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.contacts import models as _contacts_models  # noqa: E402, F401
from cmms.domain.contacts.loader import load as load_contacts  # noqa: E402
from cmms.domain.contacts.schemas import PersonSummary  # noqa: E402
from cmms.domain.contacts.service import ContactsError, ContactsService  # noqa: E402
from cmms.domain.identity.service import (  # noqa: E402
    AuthorizationError,
    IdentityService,
)


def _row(contactid: str, **over: str) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "contactid": contactid,
        "category": "Supplier",
        "fullname": "",
        "fname": "",
        "lname": "",
        "company": "",
        "email": "",
        "wphone": "",
        "ext": "",
        "mobile": "",
        "wweb": "",
        "waddress": "",
        "haddress": "",
    }
    base.update(over)
    return base


ROWS = [
    _row(
        "SAMWU99",
        category="Employee",
        company="CMA",
        fullname="Sam Wu",
        email="sam-wu@cma.example.com",
    ),
    _row(
        "SMWU", category="Employee", company="CMA", fullname="Sam Wu",
        email="Sam-Wu@cma.example.com",
    ),  # 別名 → SAMWU99
    _row(
        "ALFORD",
        category="Customer",
        company="SF",
        fullname="Alan Ford",
        email="Alan.Ford@example.com",
        wphone="886-2-1234-5678",
        ext="204",
    ),
    _row(
        "VELMOT",
        category="Supplier",
        company="Vela Motors Taiwan Co., Ltd.",
        fullname="Paul Yan",
        email="sales@vela.example.com",
        wphone="+886 2 8765 4321",
    ),
    _row(
        "NOPTIC",
        category="Supplier",
        company="Nordic Optics Ltd.",
        fullname="Rob Miller",
        email="r.miller@nordic.example.com",
    ),
    _row(
        "NOPT",
        category="Supplier",
        company="Nordic Optics Ltd.",
        fullname="Rob Miller",
        email="r.miller@nordic.example.com",
    ),  # 別名 → NOPTIC
    # Group B(跨公司同 email)→ 保留為獨立 person + 獨立 org
    _row(
        "BRTELEC",
        category="Supplier",
        company="Brightway Electronics Co., Ltd.",
        email="contact@kaimu.example.com",
    ),
    _row(
        "KAICO.",
        category="Supplier",
        company="Kaimu Co. Ltd.",
        email="contact@kaimu.example.com",
        wphone="02-12345678",
    ),
]


@pytest.fixture
async def session():
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            yield s
        await engine.dispose()


async def test_load_counts_and_seed(session) -> None:
    res = await load_contacts(ROWS, session)
    # 6 distinct company(CMA/SF/Vela/Nordic/Brightway/Kaimu)+ CMB 種子 = 7
    assert res.organizations == 7
    assert res.seeded_orgs == 1
    # 8 contacts − 2 別名(SMWU/NOPT)= 6 person
    assert res.persons == 6
    assert res.aliases == 2
    assert res.org_types == 4 and res.contact_categories == 3


async def test_alias_resolution(session) -> None:
    await load_contacts(ROWS, session)
    svc = ContactsService(session)
    # 別名不建 person 列
    assert await svc.get_person("SMWU") is None
    assert await svc.get_person("NOPT") is None
    # 但可經 resolve_person 解析回 canonical
    assert (await svc.resolve_person("SMWU")).person_id == "SAMWU99"
    assert (await svc.resolve_person("NOPT")).person_id == "NOPTIC"
    # canonical 本身照常
    assert (await svc.resolve_person("SAMWU99")).person_id == "SAMWU99"
    assert await svc.resolve_person("NOPE") is None


async def test_org_type_derivation(session) -> None:
    await load_contacts(ROWS, session)
    svc = ContactsService(session)
    assert (await svc.get_organization("CMA")).org_type == "Contractor"  # Employee→Contractor
    assert (await svc.get_organization("CMA")).is_active is True
    assert (await svc.get_organization("SF")).org_type == "Internal"  # override
    assert (await svc.get_organization("VELA_MOTORS_TAIWAN_CO_LTD")).org_type == "Supplier"
    cmb = await svc.get_organization("CMB")
    assert cmb.org_type == "Contractor" and cmb.is_active is False


async def test_group_b_kept_distinct(session) -> None:
    await load_contacts(ROWS, session)
    svc = ContactsService(session)
    # 跨公司同 email 的兩人保留為獨立 person + 獨立 org(保守去重)
    assert await svc.get_person("BRTELEC") is not None
    assert await svc.get_person("KAICO.") is not None
    assert await svc.get_organization("BRIGHTWAY_ELECTRONICS_CO_LTD") is not None
    assert await svc.get_organization("KAIMU_CO_LTD") is not None


async def test_filters_and_category_preserved(session) -> None:
    await load_contacts(ROWS, session)
    svc = ContactsService(session)

    suppliers = await svc.list_organizations(org_type="Supplier", limit=100)
    assert {o.org_id for o in suppliers} == {
        "VELA_MOTORS_TAIWAN_CO_LTD",
        "NORDIC_OPTICS_LTD",
        "BRIGHTWAY_ELECTRONICS_CO_LTD",
        "KAIMU_CO_LTD",
    }
    inactive = await svc.list_organizations(is_active=False, limit=100)
    assert {o.org_id for o in inactive} == {"CMB"}

    # 原始 category 保留(不解讀成 role)
    assert (await svc.get_person("ALFORD")).category == "Customer"
    cma_people = await svc.list_persons(org_id="CMA", limit=100)
    assert {p.person_id for p in cma_people} == {"SAMWU99"}  # SMWU 已別名化
    customers = await svc.list_persons(category="Customer", limit=100)
    assert {p.person_id for p in customers} == {"ALFORD"}
    found = await svc.list_persons(search="miller", limit=100)
    assert {p.person_id for p in found} == {"NOPTIC"}


async def test_pii_summary_excludes_contact_fields(session) -> None:
    await load_contacts(ROWS, session)
    svc = ContactsService(session)
    person = await svc.get_person("ALFORD")
    # 完整查詢保有 PII
    assert person.email == "Alan.Ford@example.com"
    # 但摘要(批次列舉用)不含 PII 欄位(06-contacts §3)
    summary = PersonSummary.model_validate(person).model_dump()
    assert "email" not in summary and "work_phone" not in summary and "mobile" not in summary
    assert summary["full_name"] == "Alan Ford"


async def test_load_is_idempotent(session) -> None:
    await load_contacts(ROWS, session)
    res2 = await load_contacts(ROWS, session)
    assert res2.organizations == 7 and res2.persons == 6 and res2.aliases == 2
    svc = ContactsService(session)
    assert len(await svc.list_organizations(limit=1000)) == 7
    assert len(await svc.list_persons(limit=1000)) == 6
    assert (await svc.resolve_person("SMWU")).person_id == "SAMWU99"


async def _seed_admin(session, user_id: str = "admin1", role: str = "admin") -> Actor:
    """種一個 active 帳號(啟停 org 的 domain 角色守門需要;比照 test_work_orders_db)。"""
    await IdentityService(session).create_user(
        user_id=user_id, username=user_id, display_name=user_id, password="pw-123456",
        org="Shopfloor", actor=Actor.human("bootstrap"), role=role,
    )
    return Actor.human(user_id)


async def test_set_organization_active_admin(session) -> None:
    """admin 啟停機構(治理寫入 + 稽核);冪等同值 no-op。"""
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    # CMB 種子 is_active=False → admin 啟用
    org = await svc.set_organization_active("CMB", True, actor=admin)
    assert org.is_active is True
    assert (await svc.get_organization("CMB")).is_active is True
    assert org.updated_by == "human:admin1"          # 稽核欄(ADR-005)
    # 冪等:同值 → no-op(仍 True)
    again = await svc.set_organization_active("CMB", True, actor=admin)
    assert again.is_active is True
    # 停用
    await svc.set_organization_active("CMB", False, actor=admin)
    assert (await svc.get_organization("CMB")).is_active is False


async def test_set_organization_active_requires_admin(session) -> None:
    """engineer 呼叫 → AuthorizationError(domain 強制,非只靠 route 藏按鈕);未變更。"""
    await load_contacts(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = ContactsService(session)
    with pytest.raises(AuthorizationError):
        await svc.set_organization_active("CMB", True, actor=Actor.human("eng1"))
    assert (await svc.get_organization("CMB")).is_active is False


async def test_set_organization_active_unknown(session) -> None:
    await _seed_admin(session)
    svc = ContactsService(session)
    with pytest.raises(ContactsError):
        await svc.set_organization_active("NOPE", True, actor=Actor.human("admin1"))


# ---- #6a 機構主檔編輯(admin) ----


async def test_update_organization_admin(session) -> None:
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    org = await svc.update_organization(
        "VELA_MOTORS_TAIWAN_CO_LTD", actor=admin,
        name="Vela Motors TW", org_type="Contractor",
        website="https://vela.example.com", address="Taichung", phone="04-000",
    )
    assert org.name == "Vela Motors TW"
    assert org.org_type == "Contractor"
    assert org.website == "https://vela.example.com"
    assert org.updated_by == "human:admin1"  # 稽核欄
    # 空欄 → None
    org2 = await svc.update_organization(
        "VELA_MOTORS_TAIWAN_CO_LTD", actor=admin,
        name="Vela Motors TW", org_type="", website="", address="", phone="",
    )
    assert org2.website is None and org2.org_type is None


async def test_update_organization_requires_admin(session) -> None:
    await load_contacts(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = ContactsService(session)
    with pytest.raises(AuthorizationError):
        await svc.update_organization(
            "SF", actor=Actor.human("eng1"), name="X", org_type=None,
            website=None, address=None, phone=None,
        )


async def test_update_organization_guards(session) -> None:
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    with pytest.raises(ContactsError):  # 不存在
        await svc.update_organization("NOPE", actor=admin, name="X", org_type=None,
                                      website=None, address=None, phone=None)
    with pytest.raises(ContactsError):  # 名稱不可空
        await svc.update_organization("SF", actor=admin, name="  ", org_type=None,
                                      website=None, address=None, phone=None)
    with pytest.raises(ContactsError):  # org_type 非既存 lookup
        await svc.update_organization("SF", actor=admin, name="X", org_type="BOGUS",
                                      website=None, address=None, phone=None)


# ---- #6b 聯絡人新增/編輯(admin) ----


async def test_create_person_admin(session) -> None:
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    person = await svc.create_person(
        org_id="CMA", actor=admin, full_name="New Guy", email="ng@contractor.com",
        category="Employee", is_main=True,
    )
    assert person.person_id.startswith("PSN-")
    assert person.org_id == "CMA" and person.full_name == "New Guy"
    assert person.is_main is True
    assert person.created_by == "human:admin1"
    # is_main 一機構一位:CMA 既有 main(若有)被清 —— 此人為唯一 main
    contractor = await svc.list_org_persons("CMA")
    assert [p.person_id for p in contractor if p.is_main] == [person.person_id]


async def test_create_person_requires_admin_and_valid_org(session) -> None:
    await load_contacts(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    with pytest.raises(AuthorizationError):
        await svc.create_person(org_id="CMA", actor=Actor.human("eng1"), full_name="X")
    with pytest.raises(ContactsError):  # org 不存在
        await svc.create_person(org_id="NOPE", actor=admin, full_name="X")
    with pytest.raises(ContactsError):  # category 非既存 lookup
        await svc.create_person(org_id="CMA", actor=admin, full_name="X", category="BOGUS")


async def test_update_person_admin_and_main(session) -> None:
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    # 先在 CMA 建兩人並設第一位為 main
    a = await svc.create_person(org_id="CMA", actor=admin, full_name="A", is_main=True)
    b = await svc.create_person(org_id="CMA", actor=admin, full_name="B")
    # 編輯 b 設為 main → a 自動退位(一機構一位)
    await svc.update_person(
        b.person_id, actor=admin, full_name="B2", first_name=None, last_name=None,
        category=None, email="b@x.com", work_phone=None, mobile=None, extension=None,
        work_address=None, is_main=True,
    )
    people = {p.person_id: p for p in await svc.list_org_persons("CMA")}
    assert people[b.person_id].full_name == "B2" and people[b.person_id].email == "b@x.com"
    assert people[b.person_id].is_main is True
    assert people[a.person_id].is_main is False
    # 稽核欄
    assert people[b.person_id].updated_by == "human:admin1"


async def test_update_person_requires_admin(session) -> None:
    await load_contacts(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = ContactsService(session)
    with pytest.raises(AuthorizationError):
        await svc.update_person(
            "ALFORD", actor=Actor.human("eng1"), full_name="X", first_name=None,
            last_name=None, category=None, email=None, work_phone=None, mobile=None,
            extension=None, work_address=None, is_main=False,
        )


# ---- 新增機構(admin governed create;org_id 由名稱導出) ----


async def test_create_organization_admin(session) -> None:
    await load_contacts(ROWS, session)  # 種 org_type lookup(Supplier 等)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    org = await svc.create_organization(
        actor=admin, name="Acme Pumps Ltd.", org_type="Supplier",
        website="https://acme.example", address="", phone="",
    )
    assert org.org_id == "ACME_PUMPS_LTD"      # 名稱 → slug 決定性導出
    assert org.name == "Acme Pumps Ltd." and org.org_type == "Supplier"
    assert org.is_active is True
    assert org.website == "https://acme.example"
    assert org.address is None and org.phone is None    # 空欄 → None
    assert org.created_by == "human:admin1"
    assert org.updated_by == "human:admin1"
    assert org.source_actor == "human:admin1"
    # round-trip
    fetched = await svc.get_organization("ACME_PUMPS_LTD")
    assert fetched is not None and fetched.name == "Acme Pumps Ltd."


async def test_create_organization_dup_slug(session) -> None:
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    await svc.create_organization(actor=admin, name="Acme Pumps Ltd.")
    # 不同寫法但導出同一 slug(ACME_PUMPS_LTD)→ 判定重複
    with pytest.raises(ContactsError, match="already exists"):
        await svc.create_organization(actor=admin, name="Acme, Pumps  Ltd")


async def test_create_organization_requires_admin(session) -> None:
    await load_contacts(ROWS, session)
    await _seed_admin(session, user_id="eng1", role="engineer")
    svc = ContactsService(session)
    with pytest.raises(AuthorizationError):
        await svc.create_organization(actor=Actor.human("eng1"), name="Some Vendor")


async def test_create_organization_guards(session) -> None:
    await load_contacts(ROWS, session)
    admin = await _seed_admin(session)
    svc = ContactsService(session)
    with pytest.raises(ContactsError):  # 名稱不可空
        await svc.create_organization(actor=admin, name="   ")
    with pytest.raises(ContactsError):  # org_type 非既存 lookup
        await svc.create_organization(actor=admin, name="Bogus Vendor", org_type="BOGUS")
    # org_type="" → 存 None
    org = await svc.create_organization(actor=admin, name="Blank Type Vendor", org_type="")
    assert org.org_type is None
