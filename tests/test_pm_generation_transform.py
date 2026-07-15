"""PM 生成排程運算的純函式測試(add_interval;無 DB,本機可跑)。ADR-021。

涵蓋 Days / Weeks / Months 三 unit,以及 Months 的年進位 + 月末 clamp 邊界
(Jan31 +1mo → Feb、Dec +1mo → 次年 Jan、閏年 29、跨年 + clamp)。
"""

from __future__ import annotations

from datetime import date

import pytest

from cmms.domain.pm_schedule.transform import add_interval, effective_generation_date


def test_add_interval_days() -> None:
    assert add_interval(date(2026, 6, 1), 10, "Days") == date(2026, 6, 11)
    assert add_interval(date(2026, 6, 28), 5, "Days") == date(2026, 7, 3)  # 跨月
    assert add_interval(date(2026, 1, 1), 0, "Days") == date(2026, 1, 1)


def test_add_interval_weeks() -> None:
    assert add_interval(date(2026, 6, 1), 2, "Weeks") == date(2026, 6, 15)
    assert add_interval(date(2026, 12, 28), 1, "Weeks") == date(2027, 1, 4)  # 跨年


def test_add_interval_months_simple() -> None:
    assert add_interval(date(2026, 6, 1), 12, "Months") == date(2027, 6, 1)  # 一年
    assert add_interval(date(2026, 1, 15), 1, "Months") == date(2026, 2, 15)
    assert add_interval(date(2026, 6, 1), 6, "Months") == date(2026, 12, 1)


def test_add_interval_months_year_rollover() -> None:
    # Dec + 1mo → 次年 Jan(月序進位)
    assert add_interval(date(2026, 12, 10), 1, "Months") == date(2027, 1, 10)
    assert add_interval(date(2026, 11, 30), 3, "Months") == date(2027, 2, 28)  # 進位 + clamp


def test_add_interval_months_clamp_to_month_end() -> None:
    # Jan31 +1mo → Feb 月末(平年 28 / 閏年 29);不溢位到 3 月
    assert add_interval(date(2026, 1, 31), 1, "Months") == date(2026, 2, 28)  # 2026 平年
    assert add_interval(date(2024, 1, 31), 1, "Months") == date(2024, 2, 29)  # 2024 閏年
    assert add_interval(date(2026, 3, 31), 1, "Months") == date(2026, 4, 30)  # 4 月 30 天
    assert add_interval(date(2026, 1, 31), 13, "Months") == date(2027, 2, 28)  # 跨年 + clamp


def test_add_interval_unknown_unit_raises() -> None:
    # 不臆測未知 unit 語意(守護欄 #8)
    with pytest.raises(ValueError):
        add_interval(date(2026, 6, 1), 1, "Years")


# ---- effective_generation_date:週末提前生成(#5d,純函式)----
# 2026-07-06(週一)~ 07-12(週日)一整週,驗五/六/日的有效生成日。

def test_effective_generation_date_weekday_unchanged() -> None:
    # 週一~週五:有效日 = 到期日本身(不提前)
    for d in (date(2026, 7, 6), date(2026, 7, 7), date(2026, 7, 8),
              date(2026, 7, 9), date(2026, 7, 10)):
        assert effective_generation_date(d) == d


def test_effective_generation_date_saturday_to_friday() -> None:
    # 週六(07-11)→ 前一天週五(07-10)
    assert date(2026, 7, 11).weekday() == 5
    assert effective_generation_date(date(2026, 7, 11)) == date(2026, 7, 10)


def test_effective_generation_date_sunday_to_friday() -> None:
    # 週日(07-12)→ 前兩天週五(07-10)
    assert date(2026, 7, 12).weekday() == 6
    assert effective_generation_date(date(2026, 7, 12)) == date(2026, 7, 10)


def test_effective_generation_date_month_boundary() -> None:
    # 跨月:2026-08-01 為週六 → 有效日回退到 07-31(週五)
    assert date(2026, 8, 1).weekday() == 5
    assert effective_generation_date(date(2026, 8, 1)) == date(2026, 7, 31)
