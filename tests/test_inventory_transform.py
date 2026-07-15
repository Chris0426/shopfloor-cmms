"""Inventory 載入轉換的純函式測試(無 DB,本機可跑)。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cmms.domain.inventory.loader import read_rows
from cmms.domain.inventory.transform import (
    canonical_subtype,
    clean,
    clean_text,
    parse_bool,
    parse_decimal,
    parse_int,
    repair_malformed_line,
    row_to_import,
    split_multi,
)


def test_clean_nan() -> None:
    assert clean("  ES1 ") == "ES1"
    assert clean("nan") is None
    assert clean("") is None
    assert clean(None) is None


def test_clean_text_html_entity() -> None:
    # &#956; = μ;cp1252 解碼在 loader 讀檔層,這裡測 entity 還原
    assert clean_text("pore size 0.8 &#956;m") == "pore size 0.8 μm"
    assert clean_text("plain") == "plain"
    assert clean_text("") is None


def test_parse_decimal_and_int() -> None:
    assert parse_decimal("574.600") == Decimal("574.600")
    assert parse_decimal("5.77000") == Decimal("5.77000")
    assert parse_decimal("") is None
    assert parse_int("6") == 6
    assert parse_int("4.0") == 4  # 偶見小數字串
    assert parse_int("") is None


def test_parse_bool() -> None:
    assert parse_bool("T", default=False) is True
    assert parse_bool("F", default=True) is False
    assert parse_bool("", default=True) is True
    assert parse_bool(None, default=False) is False


def test_split_multi_dedup_order() -> None:
    assert split_multi("A, B ,A,C") == ["A", "B", "C"]  # 去空白、去重、保序
    assert split_multi("") == []
    assert split_multi(None) == []


def test_canonical_subtype_alias() -> None:
    # A3:明顯變體整併
    assert canonical_subtype("STA1 CALIBRATOR") == "CALIBRATOR STA1"
    assert canonical_subtype("CALIBRATOR STA1") == "CALIBRATOR STA1"
    assert canonical_subtype("APEX SORTER") == "APEX SORTER"  # 無 alias → 原樣


def test_row_to_import_maps_and_multivalue() -> None:
    row = {
        "item": "ES000701",
        "asset_sub": "APEX SORTER, STA1 CALIBRATOR",
        "sf_desc": "Syringe Filter",
        "vpartno": "SF-08-025",
        "descrip": "Syringe filter, MCE membrane, pore size 0.8 &#956;m",
        "location": "20B",
        "orderpt": "50.000",
        "onhand": "0.000",
        "cost": "5.77000",
        "lead_time": "6",
        "obsol": "F",
        "stock": "T",
        "supplier": "MERIDIAN FILTERS INC.",
        "weblink": "",
        "photo": "<img id=x>",
        "parnt_item": "",
        "child_item": "",
        "alt_item": "EC000808",
        "comment": "",
    }
    p = row_to_import(row)
    assert p.item.item_code == "ES000701"
    assert p.item.item_category == "ES"  # 前綴
    assert p.item.name == "Syringe Filter"
    assert p.item.description == "Syringe filter, MCE membrane, pore size 0.8 μm"  # entity 還原
    assert p.item.unit_cost == Decimal("5.77000")
    assert p.item.is_stocked is True and p.item.is_obsolete is False
    # 多值 + canonical
    assert p.asset_subtypes == ["APEX SORTER", "CALIBRATOR STA1"]
    assert p.alternatives == ["EC000808"]


def test_row_to_import_requires_item() -> None:
    with pytest.raises(ValueError):
        row_to_import({"descrip": "x"})


_HEADER = (
    "item,asset_sub,sf_desc,vpartno,descrip,location,orderpt,onhand,cost,lead_time,"
    "obsol,stock,supplier,weblink,photo,parnt_item,child_item,alt_item,comment,"
)


def test_read_rows_skips_unrecoverable(tmp_path) -> None:
    # 無數值錨點的畸形行無法修復 → 仍跳過(item 第 0 欄可讀;欄位數 21)。
    good = "ES001" + "," * 19  # 20 欄
    bad = "ES002" + "," * 20  # 21 欄,但無 descrip/location/數值區塊 → 不可錨定
    f = tmp_path / "inv.csv"
    f.write_text("\n".join([_HEADER, good, bad]) + "\n", encoding="cp1252")

    valid, skipped, repaired = read_rows(f)
    assert [r["item"] for r in valid] == ["ES001"]
    assert skipped == [("ES002", 21)]
    assert repaired == []


def test_read_rows_repairs_inch_quote(tmp_path) -> None:
    # A3b:descrip 內未跳脫英吋符號 " 的真實畸形行,read_rows 應自動修回並併入乾淨列。
    good = "ES001" + "," * 19
    bad = (
        '"ES000802","STA4 GEN1, STA4 GEN2","","QDC-22006-T",'
        '"Quick (male) connector (with valve) insert 3/8"I,D-T",'
        '"09A",10.000,25.000,12.67000,2,F,T,"FOXGLOVE FLUIDICS LTD.","",'
        '"<img id=_i__6K603BXF3 src=x >","","","","",'
    )
    f = tmp_path / "inv.csv"
    f.write_text("\n".join([_HEADER, good, bad]) + "\n", encoding="cp1252")

    valid, skipped, repaired = read_rows(f)
    assert skipped == []
    assert repaired == ["ES000802"]
    fixed = next(r for r in valid if r["item"] == "ES000802")
    # descrip 原樣保留(含英吋符號:品名一律原樣載入,不改寫)
    assert fixed["descrip"] == 'Quick (male) connector (with valve) insert 3/8"I,D-T'
    assert fixed["location"] == "09A"
    assert fixed["cost"] == "12.67000"
    assert fixed["supplier"] == "FOXGLOVE FLUIDICS LTD."


def test_repair_malformed_line_split_two_inch_quotes() -> None:
    # descrip 含兩個英吋符號(0.1" / 0.43")→ csv 解析成 22 欄;repair 接回原樣。
    line = (
        '"ES000801","STA4 GEN1, STA4 GEN2, PROBER","Pogo Pin","PGO-22-005-0091",'
        '"Connector, 5 way, modular spring pogo contact, 0.1" pitch, 0.43" height, surface mount",'
        '"19",120.000,150.000,13.82000,4,F,T,"Kestrel Pneumatics, Inc","","<img src=x >",'
        '"ES000043","","","",'
    )
    fields = repair_malformed_line(line)
    assert fields is not None
    assert len(fields) == 20
    assert fields[4] == (
        'Connector, 5 way, modular spring pogo contact, 0.1" pitch, 0.43" height, surface mount'
    )
    assert fields[5] == "19"
    assert fields[15] == "ES000043"  # parnt_item 尾段仍正確


def test_repair_malformed_line_merged_location() -> None:
    # 英吋符號在 descrip 結尾(.046")致 location 被併入(欄位數變少,19 欄)。
    line = (
        '"EC000806","POTTER GEN1","","TA-741-046",'
        '"SS TIP ADAPTER -741V .046"","15C",2.000,4.000,145.00000,6,F,T,'
        '"Granite Bay Tooling, Ltd.","","<img src=x >","","","","",'
    )
    fields = repair_malformed_line(line)
    assert fields is not None
    assert len(fields) == 20
    assert fields[4] == 'SS TIP ADAPTER -741V .046"'
    assert fields[5] == "15C"
    assert fields[12] == "Granite Bay Tooling, Ltd."  # supplier 內含逗號,正確還原


def test_repair_malformed_line_unrecoverable() -> None:
    assert repair_malformed_line("ES999" + "," * 20) is None
