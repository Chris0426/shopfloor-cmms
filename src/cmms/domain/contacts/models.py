"""Contacts ORM models(對應 docs/domain-model/06-contacts.md §9)。

- lookup:`org_type`(Supplier/Contractor/Customer/Internal)、`contact_category`
  (eMaint 原始分類 Supplier/Employee/Customer)。
- `organization`(org_id = company slug 代理鍵)+ `person`(person_id = contactid)。
- `person_alias`:保守去重的別名映射(別名 contactid → canonical person_id)。
- supplier / assigned_person / closed_by 等軟參照**不在此 retrofit FK**(Jordan 拍板:只建本體);
  `app_user`(登入帳號)延到 #4b 寫入切片(帳號≠聯絡人)。
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class _CodeLabel:
    """lookup 共用:code(PK)+ label(人/agent 可讀,ADR-007)。"""

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class OrgType(_CodeLabel, Base):
    __tablename__ = "org_type"  # Supplier / Contractor / Customer / Internal


class ContactCategory(_CodeLabel, Base):
    __tablename__ = "contact_category"  # eMaint 原始分類:Supplier / Employee / Customer


class Organization(AuditMixin, Base):
    __tablename__ = "organization"

    org_id: Mapped[str] = mapped_column(String, primary_key=True)  # company 名 slug(新代理鍵)
    name: Mapped[str] = mapped_column(String, nullable=False)  # company 原文
    org_type: Mapped[str | None] = mapped_column(ForeignKey("org_type.code"), nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )  # CMB=false(合約終止,僅供舊工單 assignto 解析)
    website: Mapped[str | None] = mapped_column(String, nullable=True)  # wweb(org 代表值)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)  # waddress(org 代表值)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)  # wphone(org 代表值)


class Person(AuditMixin, Base):
    __tablename__ = "person"

    person_id: Mapped[str] = mapped_column(String, primary_key=True)  # contactid
    org_id: Mapped[str] = mapped_column(ForeignKey("organization.org_id"), nullable=False)
    category: Mapped[str | None] = mapped_column(
        ForeignKey("contact_category.code"), nullable=True
    )  # 原始 eMaint 分類(不解讀成 role;見 __init__ 說明)
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)  # fname
    last_name: Mapped[str | None] = mapped_column(String, nullable=True)  # lname
    full_name: Mapped[str | None] = mapped_column(String, nullable=True)  # fullname
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    work_phone: Mapped[str | None] = mapped_column(String, nullable=True)  # wphone
    extension: Mapped[str | None] = mapped_column(String, nullable=True)  # ext
    mobile: Mapped[str | None] = mapped_column(String, nullable=True)
    work_address: Mapped[str | None] = mapped_column(Text, nullable=True)  # waddress
    # home_address [DROP]:1 筆垃圾值(06-contacts §2)
    # 主要聯絡人(ADR-026;一機構一位 main;RFQ 收件人優先取其 email)。載入預設 false,由 owner 標記。
    is_main: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))


class PersonAlias(AuditMixin, Base):
    """同公司同人重複 → 別名映射(保守去重,Jordan 拍板)。

    舊資料引用別名 contactid(如 SMWU / NOPT)可經此解析回 canonical person;
    別名**不建** person 列。詳見 06-contacts §5。
    """

    __tablename__ = "person_alias"

    alias_contact_id: Mapped[str] = mapped_column(String, primary_key=True)  # SMWU / NOPT
    person_id: Mapped[str] = mapped_column(
        ForeignKey("person.person_id"), nullable=False
    )  # canonical:SAMWU99 / NOPTIC
