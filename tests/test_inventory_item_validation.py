"""品項主檔數值欄寫入守門(純函式,免 DB):建立 / 編輯 / 編輯提案 propose 共用同一份規則。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cmms.domain.inventory.service import InventoryError, InventoryService
from cmms.domain.work_order.service import WorkOrderError, WorkOrderService


def _nums(**over):
    kw = {"reorder_point": None, "reorder_quantity": None, "lead_time_weeks": None,
          "unit_cost": None}
    kw.update(over)
    return kw


def test_valid_and_empty_values_pass() -> None:
    InventoryService._validate_item_numbers(**_nums())
    InventoryService._validate_item_numbers(
        **_nums(reorder_point=Decimal("0"), reorder_quantity=Decimal("999999999.999"),
                lead_time_weeks=2**31 - 1, unit_cost=Decimal("999999999.99999"))
    )


@pytest.mark.parametrize("field", ["reorder_point", "reorder_quantity", "unit_cost"])
def test_negative_decimal_rejected(field: str) -> None:
    with pytest.raises(InventoryError, match=f"{field} cannot be negative"):
        InventoryService._validate_item_numbers(**_nums(**{field: Decimal("-0.5")}))


def test_negative_int_rejected() -> None:
    with pytest.raises(InventoryError, match="lead_time_weeks cannot be negative"):
        InventoryService._validate_item_numbers(**_nums(lead_time_weeks=-1))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reorder_point", Decimal("1000000000")),       # Numeric(12,3):整數位上限 9
        ("reorder_quantity", Decimal("999999999.9995")),  # 四捨五入到 3 位後進位溢出
        ("unit_cost", Decimal("1e12")),                  # Numeric(14,5):整數位上限 9
        ("unit_cost", Decimal("999999999.999995")),
    ],
)
def test_decimal_out_of_column_range_rejected(field: str, value: Decimal) -> None:
    with pytest.raises(InventoryError, match=f"{field} out of range for numeric"):
        InventoryService._validate_item_numbers(**_nums(**{field: value}))


def test_integer_out_of_range_rejected() -> None:
    with pytest.raises(InventoryError, match="lead_time_weeks out of range for integer"):
        InventoryService._validate_item_numbers(**_nums(lead_time_weeks=2**31))


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_non_finite_rejected(raw: str) -> None:
    with pytest.raises(InventoryError, match="must be a finite number"):
        InventoryService._validate_item_numbers(**_nums(unit_cost=Decimal(raw)))


def test_coerce_rejects_unparseable_numbers() -> None:
    with pytest.raises(WorkOrderError, match="invalid item field value"):
        WorkOrderService._coerce_item_params({"reorder_point": "abc"})
    with pytest.raises(WorkOrderError, match="invalid item field value"):
        WorkOrderService._coerce_item_params({"lead_time_weeks": "1.5"})
