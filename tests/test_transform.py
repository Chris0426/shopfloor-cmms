"""Asset 載入轉換的純函式測試(無 DB,本機可跑)。"""

from __future__ import annotations

import pytest

from cmms.domain.asset.transform import (
    clean,
    line_sort_key,
    normalize_line,
    parse_yes_no,
    row_to_import,
)


def test_clean() -> None:
    assert clean("  EID-1 ") == "EID-1"
    assert clean("") is None
    assert clean("   ") is None
    assert clean(None) is None


def test_parse_yes_no() -> None:
    assert parse_yes_no("Yes") is True
    assert parse_yes_no("No") is False
    assert parse_yes_no(None) is True  # 空值保守視為可用


def test_normalize_line_canonical_casing() -> None:
    # 資料品質問題 §5.1:Wet Loop / Wet loop 必須收斂成同一值
    assert normalize_line("Wet Loop") == "Wet Loop"
    assert normalize_line("Wet loop") == "Wet Loop"
    assert normalize_line("  wet loop ") == "Wet Loop"
    # 01K 更名為 1K;legacy 別名 "01k" 亦收斂到 1K(舊 eMaint 排序 workaround)
    assert normalize_line("01K") == "1K"
    assert normalize_line("01k") == "1K"


def test_normalize_line_unknown_and_empty() -> None:
    assert normalize_line("SomeNewLine") == "SomeNewLine"  # 未知值原樣保留
    assert normalize_line("") is None
    assert normalize_line(None) is None


def test_row_to_import_maps_csv_columns() -> None:
    row = {
        "compid": "EID-70009",
        "comp_desc": "AP-500 Cleaner #2",
        "assettype": "Production",
        "assetsubtp": "AP CLEANER",
        "available": "No",
        "department": "EQ",
        "line_no": "01K",  # 舊 CSV 值,normalize 後應收斂為 1K
        "model_no": "AP-500E",
        "serial_no": "SN480912",
    }
    imp = row_to_import(row)
    assert imp.asset_id == "EID-70009"
    assert imp.asset_type == "Production"
    assert imp.department == "EQ"
    assert imp.line == "1K"
    assert imp.available_for_service is False
    assert imp.asset_subtype == "AP CLEANER"


def test_line_sort_key_natural_order() -> None:
    # 自然排序:1K < 10K < EOL < Wet Loop(純字串排序會把 10K 排在 1K 前)
    assert sorted(["Wet Loop", "10K", "EOL", "1K"], key=line_sort_key) == [
        "1K",
        "10K",
        "EOL",
        "Wet Loop",
    ]


def test_row_to_import_requires_id_and_type() -> None:
    with pytest.raises(ValueError):
        row_to_import({"comp_desc": "x", "assettype": "Production", "department": "EQ"})
    with pytest.raises(ValueError):
        row_to_import({"compid": "EID-1", "assettype": "", "department": "EQ"})


def test_row_to_import_department_override_and_blank() -> None:
    # 已知修正:EID-70029 空部門 → EQ
    fixed = row_to_import({"compid": "EID-70029", "assettype": "Production", "department": ""})
    assert fixed.department == "EQ"
    # 未知缺漏仍允許 NULL(department nullable)
    other = row_to_import({"compid": "EID-99999", "assettype": "Production", "department": ""})
    assert other.department is None
