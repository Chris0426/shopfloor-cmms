"""task_steps_parts.csv transform 純函式單元測試(無 DB)。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from cmms.domain.task.step_loader import make_step_key
from cmms.domain.task.transform import parse_decimal, parse_int, row_to_step_import


def _row(**kw: str) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "task_no": "TSK0007", "proc_seq": "10", "task_desc": "Clean the head", "item": "",
        "replaceqty": "",
    }
    base.update(kw)
    return base


def test_row_basic() -> None:
    imp = row_to_step_import(_row())
    assert imp.task_no == "TSK0007" and imp.proc_seq == 10
    assert imp.task_desc == "Clean the head"
    assert imp.item_code is None and imp.replace_qty is None


def test_row_with_part() -> None:
    imp = row_to_step_import(
        _row(proc_seq="20", task_desc="Replace blade", item="EC000807", replaceqty="2")
    )
    assert imp.item_code == "EC000807" and imp.replace_qty == Decimal("2")


def test_missing_required_raises() -> None:
    with pytest.raises(ValueError):
        row_to_step_import(_row(task_no=""))
    with pytest.raises(ValueError):
        row_to_step_import(_row(task_desc=""))


def test_proc_seq_non_int_is_none() -> None:
    assert row_to_step_import(_row(proc_seq="abc")).proc_seq is None
    assert row_to_step_import(_row(proc_seq="")).proc_seq is None


def test_qty_missing_kept_none() -> None:
    assert row_to_step_import(_row(item="EC1", replaceqty="")).replace_qty is None
    assert parse_decimal("bad") is None and parse_decimal("1.5") == Decimal("1.5")
    assert parse_int("30") == 30 and parse_int("x") is None


def test_unescape_desc() -> None:
    imp = row_to_step_import(_row(task_desc="Check ground impedance &lt;1Ohm"))
    assert imp.task_desc == "Check ground impedance <1Ohm"  # html.unescape


def test_step_key() -> None:
    assert make_step_key("TSK0007", 3) == "taskstep:v1:TSK0007:3"
