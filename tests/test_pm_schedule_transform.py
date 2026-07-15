"""PmSchedule 載入轉換的純函式測試(無 DB,本機可跑)。"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from cmms.domain.pm_schedule.transform import (
    parse_assignto,
    parse_date,
    parse_decimal,
    parse_interval,
    parse_suppress,
    row_to_import,
)


def test_parse_date_two_digit_year() -> None:
    # 實測年份 20–32,%y(<69)→ 20xx
    assert parse_date("05/19/25") == date(2025, 5, 19)
    assert parse_date("12/17/21") == date(2021, 12, 17)
    assert parse_date("01/01/32") == date(2032, 1, 1)
    assert parse_date("") is None
    assert parse_date(None) is None


def test_parse_date_four_digit_fallback() -> None:
    assert parse_date("06/28/2023") == date(2023, 6, 28)


def test_parse_decimal_leading_dot() -> None:
    assert parse_decimal(".00") == Decimal("0.00")
    assert parse_decimal(".50") == Decimal("0.50")
    assert parse_decimal("2.5") == Decimal("2.5")
    assert parse_decimal("1.50") == Decimal("1.50")
    assert parse_decimal("") is None
    assert parse_decimal(None) is None


def test_parse_interval_blank_is_zero() -> None:
    assert parse_interval("12") == 12
    assert parse_interval("0") == 0
    assert parse_interval("") == 0  # 空 → 不週期
    assert parse_interval(None) == 0


def test_parse_suppress() -> None:
    assert parse_suppress("T") is True
    assert parse_suppress("F") is False
    assert parse_suppress("") is False
    assert parse_suppress(None) is False


def test_parse_assignto_split() -> None:
    assert parse_assignto("CMA (Lin Hsu)") == ("CMA", "Lin Hsu")
    assert parse_assignto("CMB (Iris Chiu)") == ("CMB", "Iris Chiu")
    assert parse_assignto("") == (None, None)
    assert parse_assignto(None) == (None, None)


def test_parse_assignto_unparseable_preserves_value() -> None:
    # 不符 'VENDOR (Person)'(實測 0 筆):原值保留於 person,不遺失
    assert parse_assignto("just a name") == (None, "just a name")


def test_row_to_import_periodic() -> None:
    row = {
        "pmid": "_3TN905K21",
        "compid": "EID-70010",
        "task_no": "PRB0001",
        "pmfreqx": "12",
        "pmfreq": "Months",
        "pmnextdate": "05/19/25",
        "lastpmdate": "12/17/21",
        "lastpmno": "10415",
        "suppress": "F",
        "assignto": "CMA (Iris Chiu)",
        "standard": "1.50",
        "estlabor": "1.00",
        "dayscmpl": "2.5",
        "pm_type": "PM",
    }
    imp = row_to_import(row)
    assert imp.pm_id == "_3TN905K21"
    assert imp.asset_id == "EID-70010"
    assert imp.task_id == "PRB0001"
    assert imp.frequency_interval == 12
    assert imp.frequency_unit == "Months"
    assert imp.next_due_date == date(2025, 5, 19)
    assert imp.last_work_order_no == 10415
    assert imp.assigned_vendor == "CMA"
    assert imp.assigned_person == "Iris Chiu"
    assert imp.standard_hours == Decimal("1.50")
    assert imp.is_suppressed is False


def test_row_to_import_non_periodic_and_blank() -> None:
    # pmfreqx=0 → 不週期、frequency_unit 空;無 assignto;無 next_due_date
    row = {
        "pmid": "_7ZX412Q88",
        "compid": "EID-70029",
        "task_no": "TSK0011",
        "pmfreqx": "0",
        "pmfreq": "",
        "pmnextdate": "",
        "lastpmdate": "08/01/25",
        "lastpmno": "22178",
        "suppress": "T",
        "assignto": "",
        "standard": ".00",
        "estlabor": ".20",
        "dayscmpl": "2.5",
        "pm_type": "PM",
    }
    imp = row_to_import(row)
    assert imp.frequency_interval == 0
    assert imp.frequency_unit is None
    assert imp.next_due_date is None
    assert imp.assigned_vendor is None and imp.assigned_person is None
    assert imp.is_suppressed is True
    assert imp.estimated_labor_hours == Decimal("0.20")


def test_row_to_import_requires_keys() -> None:
    with pytest.raises(ValueError):
        row_to_import({"compid": "EID-1", "task_no": "T"})  # missing pmid
    with pytest.raises(ValueError):
        row_to_import({"pmid": "P1", "task_no": "T"})  # missing compid
    with pytest.raises(ValueError):
        row_to_import({"pmid": "P1", "compid": "EID-1"})  # missing task_no
