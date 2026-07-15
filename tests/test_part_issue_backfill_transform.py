"""part_issue_backfill 純函式單元測試(無 DB,本機可跑)。

驗證:欄位映射、html.unescape(`&rdquo;`/`&#956;`)、反正規化欄 DROP、parse_date 2 位年、
缺關鍵欄 raise、parse_decimal、冪等鍵 occurrence、read_rows(cp1252 + 尾逗號空名欄)。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from cmms.domain.work_order.part_issue_backfill import (
    PartIssueImport,
    make_idempotency_key,
    parse_decimal,
    read_rows,
    row_to_import,
)
from cmms.domain.work_order.transform import TAIPEI


def _row(**over: str | None) -> dict[str, str | None]:
    """一列乾淨的 part_issues.csv(鍵 = 表頭,含尾逗號的空名欄)。"""
    base: dict[str, str | None] = {
        "wo": "103",
        "date_wo": "08/31/16",
        "assetsubtp": "INSPECT",
        "compid": "EID-70022",
        "comp_desc": "Inspector 4",
        "item": "EC000008",
        "vpartno": "CTG-10388-01",
        "descrip": "0.2 micron 10&rdquo; polypro cartridge",
        "qty": "1.00",
        "unitcost": "41.00",
        "extcost": "41.00",
        "category": "Parts",
        "": None,
    }
    base.update(over)
    return base


def test_field_mapping() -> None:
    imp = row_to_import(_row())
    assert isinstance(imp, PartIssueImport)
    assert imp.work_order_no == 103 and isinstance(imp.work_order_no, int)
    assert imp.item_code == "EC000008"  # 來源已大寫,保留
    assert imp.quantity == Decimal("1.00")
    assert imp.occurred_at == datetime(2016, 8, 31, tzinfo=TAIPEI)  # date_wo @ Taipei 00:00
    assert imp.expected_asset_id == "EID-70022"  # compid 僅供交叉檢核


def test_html_unescape_in_reason() -> None:
    imp = row_to_import(_row(descrip="0.2 micron 10&rdquo; polypro"))
    assert imp.reason == "0.2 micron 10” polypro"  # &rdquo; → 右雙引號
    imp2 = row_to_import(_row(descrip="&#956;m filter"))
    assert imp2.reason == "μm filter"  # &#956; → µ


def test_denormalized_columns_dropped() -> None:
    imp = row_to_import(_row())
    for attr in ("comp_desc", "assetsubtp", "vpartno", "unitcost", "extcost", "category"):
        assert not hasattr(imp, attr)


def test_parse_date_two_digit_year() -> None:
    imp = row_to_import(_row(date_wo="08/31/16"))
    assert imp.occurred_at.year == 2016
    assert imp.occurred_at.month == 8 and imp.occurred_at.day == 31


@pytest.mark.parametrize("missing", ["wo", "item", "qty", "date_wo"])
def test_missing_key_field_raises(missing: str) -> None:
    with pytest.raises(ValueError):
        row_to_import(_row(**{missing: ""}))


def test_non_integer_wo_raises() -> None:
    with pytest.raises(ValueError):
        row_to_import(_row(wo="abc"))


def test_parse_decimal() -> None:
    assert parse_decimal("6.00") == Decimal("6.00")
    assert parse_decimal("") is None
    assert parse_decimal(None) is None
    with pytest.raises(ValueError):
        parse_decimal("not-a-number")


def test_make_idempotency_key_occurrence() -> None:
    assert make_idempotency_key(103, "EC000008", 1) == "partissue:v1:103:EC000008:1"
    assert make_idempotency_key(103, "EC000008", 2) == "partissue:v1:103:EC000008:2"


def test_read_rows_cp1252_and_trailing_comma(tmp_path) -> None:
    # 表頭 13 欄(尾逗號 → 空名欄);body 12 欄;descrip 內 &rdquo; entity + cp1252 high byte(®)
    csv_text = (
        "wo,date_wo,assetsubtp,compid,comp_desc,item,vpartno,descrip,qty,unitcost,extcost,category,\n"
        '103,08/31/16,"INSPECT","EID-70022","Inspector 4","EC000008","CTG-10388",'
        '"10&rdquo; \xae cartridge",1.00,41.00,41.00,"Parts"\n'
    )
    p = tmp_path / "part_issues.csv"
    p.write_bytes(csv_text.encode("cp1252"))  # ® = 0xAE in cp1252

    rows = read_rows(p)
    assert len(rows) == 1
    imp = row_to_import(rows[0])
    assert imp.work_order_no == 103 and imp.item_code == "EC000008"
    assert imp.quantity == Decimal("1.00")
    assert "”" in (imp.reason or "") and "\xae" in (imp.reason or "")  # entity + cp1252 還原
