"""downtime 語意純函式 `segment_is_downtime` 的 table-driven 單元測試(免 Docker,永遠跑)。

判定表(內部規格;Jordan 拍板):ON_HOLD 依 hold_reason(None/未知→True)、PM OPEN→False
(唯一行為差異)、其餘沿用 wo_status.is_downtime lookup。REACTIVE 與其他 work_type 無硬編碼特例。
"""

from __future__ import annotations

import pytest

from cmms.domain.work_order.downtime import segment_is_downtime

# lookup 值域比照 DB 種子(loader.py WO_STATUS_SEED / WO_HOLD_REASON_SEED)。
STATUS_IS_DOWNTIME = {
    "OPEN": True,
    "IN_PROGRESS": True,
    "ON_HOLD": True,
    "COMPLETED": False,
    "CLOSED": False,
    "CANCELLED": False,
    "VOIDED": False,
}
HOLD_IS_DOWNTIME = {
    "TEST_RUN": False,
    "WAITING_PARTS": True,
    "WAITING_VENDOR": True,
    "WAITING_MACHINE_TIME": False,
    "OTHER": True,
}

# (work_type, to_status, hold_reason, expected)
CASES = [
    # 規則 2 唯一行為差異:PM OPEN 不計、REACTIVE / 其他 work_type OPEN 計入
    ("REACTIVE", "OPEN", None, True),
    ("PM", "OPEN", None, False),
    ("CALIBRATION", "OPEN", None, True),  # 其他 work_type 沿用 lookup(不臆測)
    # IN_PROGRESS 一律計入(PM 也是——工程師已切狀態,機台停產)
    ("REACTIVE", "IN_PROGRESS", None, True),
    ("PM", "IN_PROGRESS", None, True),
    # ON_HOLD 依 hold_reason lookup,REACTIVE 與 PM 同(work_type 不影響 ON_HOLD)
    ("REACTIVE", "ON_HOLD", "TEST_RUN", False),
    ("PM", "ON_HOLD", "TEST_RUN", False),
    ("REACTIVE", "ON_HOLD", "WAITING_PARTS", True),
    ("PM", "ON_HOLD", "WAITING_PARTS", True),
    ("REACTIVE", "ON_HOLD", "WAITING_MACHINE_TIME", False),
    ("REACTIVE", "ON_HOLD", None, True),  # 未標 hold_reason → 預設停產
    ("PM", "ON_HOLD", None, True),
    ("REACTIVE", "ON_HOLD", "ZZ_UNKNOWN", True),  # 未知受控碼 → 保守停產
    ("PM", "ON_HOLD", "ZZ_UNKNOWN", True),
    # 終態一律不計(COMPLETED / CLOSED / CANCELLED / VOIDED)
    ("REACTIVE", "COMPLETED", None, False),
    ("PM", "COMPLETED", None, False),
    ("REACTIVE", "CLOSED", None, False),
    ("REACTIVE", "CANCELLED", None, False),
    ("REACTIVE", "VOIDED", None, False),
]


@pytest.mark.parametrize("work_type,to_status,hold_reason,expected", CASES)
def test_segment_is_downtime(
    work_type: str, to_status: str, hold_reason: str | None, expected: bool
) -> None:
    assert (
        segment_is_downtime(
            work_type,
            to_status,
            hold_reason,
            status_is_downtime=STATUS_IS_DOWNTIME,
            hold_is_downtime=HOLD_IS_DOWNTIME,
        )
        is expected
    )


def test_unknown_status_falls_back_to_false() -> None:
    """查無 status(非種子碼)→ 沿用現行 fallback = False(不臆測)。"""
    assert (
        segment_is_downtime(
            "REACTIVE",
            "SOME_FUTURE_STATUS",
            None,
            status_is_downtime=STATUS_IS_DOWNTIME,
            hold_is_downtime=HOLD_IS_DOWNTIME,
        )
        is False
    )
