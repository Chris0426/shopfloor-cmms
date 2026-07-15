"""Contacts 載入轉換的純函式測試(無 DB,本機可跑)。"""

from __future__ import annotations

import pytest

from cmms.domain.contacts.loader import _build_organizations
from cmms.domain.contacts.transform import (
    PERSON_ALIASES,
    clean,
    clean_text,
    derive_org_type,
    org_slug,
    row_to_contact,
)


def _row(contactid: str, **over: str) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "contactid": contactid,
        "category": "Supplier",
        "fullname": "",
        "fname": "",
        "lname": "",
        "company": "ACME Co., Ltd.",
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


def test_clean_nan() -> None:
    assert clean("  Max ") == "Max"
    assert clean("nan") is None
    assert clean("") is None
    assert clean(None) is None


def test_clean_text_html_entity() -> None:
    # &rsquo; = ’(右單引號);latin-1 解碼在 loader 讀檔層,這裡測 entity 還原
    assert clean_text("O&rsquo;Brien") == "O’Brien"
    assert clean_text("plain") == "plain"
    assert clean_text("") is None


def test_org_slug() -> None:
    assert org_slug("CMA") == "CMA"
    assert org_slug("SF") == "SF"
    assert org_slug("Vela Motors Taiwan Co., Ltd.") == "VELA_MOTORS_TAIWAN_CO_LTD"
    assert org_slug("  spaced  ") == "SPACED"
    assert org_slug("!!!") == "ORG"  # 全非英數 → fallback
    assert len(org_slug("A" * 80)) == 40  # 截斷


def test_derive_org_type() -> None:
    assert derive_org_type("vela", "Supplier") == "Supplier"
    assert derive_org_type("CMA", "Employee") == "Contractor"  # 外包操作人員
    assert derive_org_type("SF", "Customer") == "Internal"  # 名稱 override(SF=Shopfloor 自有)
    assert derive_org_type("SomeCustomerCo", "Customer") == "Customer"
    assert derive_org_type("X", None) is None


def test_person_aliases_conservative() -> None:
    # 保守去重:只兩對「同公司同人」(Jordan 拍板);跨公司/僅同名不在此
    assert PERSON_ALIASES == {"SMWU": "SAMWU99", "NOPT": "NOPTIC"}


def test_row_to_contact_maps() -> None:
    p = row_to_contact(
        _row(
            "VELMOT",
            category="Supplier",
            fullname="Paul Yan",
            fname="Joseph",
            lname="Yang",
            company="Vela Motors Taiwan Co., Ltd.",
            email="sales@vela.example.com",
            wphone="+886 2 8765 4321",
            waddress="8F.-8 No. 16",
        )
    )
    assert p.person.person_id == "VELMOT"
    assert p.person.org_id == "VELA_MOTORS_TAIWAN_CO_LTD"
    assert p.company == "Vela Motors Taiwan Co., Ltd."
    assert p.person.category == "Supplier"
    assert p.person.full_name == "Paul Yan"
    assert p.person.work_phone == "+886 2 8765 4321"
    assert p.person.work_address == "8F.-8 No. 16"


def test_row_to_contact_requires_keys() -> None:
    with pytest.raises(ValueError):
        row_to_contact(_row("", company="X"))  # 無 contactid
    with pytest.raises(ValueError):
        row_to_contact(_row("X", company=""))  # 無 company


def test_build_organizations_aggregation_and_seed() -> None:
    # 同 org 兩聯絡人,website/address/phone 取 contactid 最小者非空值(first non-null)
    rows = [
        row_to_contact(_row("B002", company="ACME Co., Ltd.", waddress="addr-b", wphone="222")),
        row_to_contact(_row("A001", company="ACME Co., Ltd.", wweb="web-a")),
        row_to_contact(_row("SF01", category="Customer", company="SF")),
    ]
    orgs, seeded = _build_organizations(rows)
    by_id = {o.org_id: o for o in orgs}

    acme = by_id["ACME_CO_LTD"]
    assert acme.website == "web-a"  # A001 提供(contactid 最小)
    assert acme.address == "addr-b"  # A001 空 → B002 補
    assert acme.phone == "222"
    assert acme.org_type == "Supplier"
    assert acme.is_active is True

    assert by_id["SF"].org_type == "Internal"  # SF override

    # CMB 種子(不在 rows;歷史承包商、停用)
    assert seeded == 1
    cmb = by_id["CMB"]
    assert cmb.org_type == "Contractor" and cmb.is_active is False
