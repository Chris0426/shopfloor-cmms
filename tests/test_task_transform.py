"""Task 載入轉換的純函式測試(無 DB,本機可跑)。"""

from __future__ import annotations

import pytest

from cmms.domain.task.transform import clean, row_to_import


def test_clean() -> None:
    assert clean("  CAL1DM ") == "CAL1DM"
    assert clean("") is None
    assert clean("   ") is None
    assert clean(None) is None


def test_row_to_import_maps_csv_columns() -> None:
    # tasks.csv 真實表頭:task_no, task_desc(+ 尾端多餘逗號形成的空欄,以名對應即忽略)
    row = {
        "task_no": "CAL1DM",
        "task_desc": "Calibrator Gen1 Daily Maintenance",
        "": "",
    }
    imp = row_to_import(row)
    assert imp.task_no == "CAL1DM"
    assert imp.description == "Calibrator Gen1 Daily Maintenance"


def test_row_to_import_requires_task_no() -> None:
    with pytest.raises(ValueError):
        row_to_import({"task_desc": "x"})
    with pytest.raises(ValueError):
        row_to_import({"task_no": "  ", "task_desc": "x"})


def test_row_to_import_blank_description_falls_back_to_task_no() -> None:
    # 描述空白時退回 task_no,確保 description 非空且可追溯
    imp = row_to_import({"task_no": "CAL1DR", "task_desc": ""})
    assert imp.description == "CAL1DR"
