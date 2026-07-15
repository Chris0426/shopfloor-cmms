"""WorkOrder 載入轉換的純函式測試(無 DB,本機可跑)。含 #4b downtime 引擎。"""

from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from cmms.domain.work_order.transform import (
    STATUS_MAP,
    clean,
    is_miscreated,
    parse_assignto,
    parse_date,
    parse_time,
    productive_minutes,
    row_to_import,
    to_taipei_naive,
    unescape_text,
)


def test_clean_nan_and_blank() -> None:
    assert clean("  24172 ") == "24172"
    assert clean("") is None
    assert clean("nan") is None  # 匯出空值顯示 nan
    assert clean("NaN") is None
    assert clean(None) is None


def test_unescape_text_html_entity() -> None:
    assert unescape_text("&#32784;&#29105;&#33184;&#24067;&#30772;&#25613;") == "耐熱膠布破損"
    assert unescape_text("Plain English") == "Plain English"  # no-op on ASCII
    assert unescape_text("") is None


def test_parse_date_two_and_four_digit() -> None:
    assert parse_date("05/23/26") == date(2026, 5, 23)
    assert parse_date("01/02/14") == date(2014, 1, 2)
    assert parse_date("") is None


def test_parse_time_formats() -> None:
    assert parse_time("11:38:29") == time(11, 38, 29)
    assert parse_time("09:00:00 AM") == time(9, 0, 0)
    assert parse_time("01:00:00 PM") == time(13, 0, 0)
    assert parse_time("08&#65306;20") == time(8, 20)  # 全形冒號
    assert parse_time("0:0:07") == time(0, 0, 7)
    assert parse_time("") is None
    assert parse_time("garbage") is None


def test_parse_assignto_split_and_bare_name() -> None:
    assert parse_assignto("CMB (Cara Lo)") == ("CMB", "Cara Lo")
    assert parse_assignto("Ben Yeh") == (None, "Ben Yeh")  # 裸名
    assert parse_assignto("") == (None, None)


def test_status_map() -> None:
    assert STATUS_MAP == {"O": "OPEN", "H": "CLOSED"}


def test_is_miscreated() -> None:
    assert is_miscreated({"miscreated": "T"}) is True
    assert is_miscreated({"miscreated": "t"}) is True
    assert is_miscreated({"miscreated": "F"}) is False
    assert is_miscreated({"miscreated": ""}) is False
    assert is_miscreated({}) is False


def test_productive_minutes_same_day() -> None:
    # 全落在生產時段(09:00 後):09:00–12:00 = 180
    assert productive_minutes(datetime(2026, 5, 20, 9, 0), datetime(2026, 5, 20, 12, 0)) == 180
    # 跨非生產起點:08:00–12:00 → 只計 09:00–12:00 = 180(08:00–09:00 不計)
    assert productive_minutes(datetime(2026, 5, 20, 8, 0), datetime(2026, 5, 20, 12, 0)) == 180
    # 全落在非生產(02:00–05:00)→ 0
    assert productive_minutes(datetime(2026, 5, 20, 2, 0), datetime(2026, 5, 20, 5, 0)) == 0


def test_productive_minutes_overnight() -> None:
    # day1 22:00 → day2 10:00:生產 = day1 22:00–24:00(120)+ day2 09:00–10:00(60)= 180
    assert productive_minutes(datetime(2026, 5, 20, 22, 0), datetime(2026, 5, 21, 10, 0)) == 180


def test_productive_minutes_multiday_interior() -> None:
    # day1 00:00 → day4 00:00:3 整日,各扣 9h 非生產 → 3*15h = 2700 分
    assert productive_minutes(datetime(2026, 5, 20, 0, 0), datetime(2026, 5, 23, 0, 0)) == 2700


def test_productive_minutes_nonpositive() -> None:
    assert productive_minutes(datetime(2026, 5, 20, 12, 0), datetime(2026, 5, 20, 12, 0)) == 0
    assert productive_minutes(datetime(2026, 5, 20, 12, 0), datetime(2026, 5, 20, 9, 0)) == 0


def test_to_taipei_naive() -> None:

    utc = datetime(2026, 5, 20, 1, 0, tzinfo=UTC)  # 01:00 UTC = 09:00 Taipei
    assert to_taipei_naive(utc) == datetime(2026, 5, 20, 9, 0)


def test_row_to_import_maps_status_and_unescapes() -> None:
    row = {
        "wo": "30167",
        "compid": "EID-70027",
        "brief_desc": "&#32784;&#29105;&#33184;&#24067;&#30772;&#25613;",
        "diag": "&#32791;&#26448;&#26356;&#25563;",
        "comments": "MRQ-4220",
        "date_wo": "05/20/26",
        "wo_type": "REACTIVE",
        "workstatus": "H",
        "miscreated": "F",
        "assignto": "CMA (Lin Hsu)",
        "time": "10:00:00",
        "editdate": "05/20/26",
        "edittime": "15:00:00",
        "edituser": "SAMWU99",
    }
    imp = row_to_import(row)
    assert imp.work_order_no == 30167
    assert imp.status == "CLOSED"  # H → CLOSED(#4b 映射)
    assert imp.brief_description == "耐熱膠布破損"
    assert imp.diagnosis == "耗材更換"
    assert imp.external_ref == "MRQ-4220"
    assert imp.assigned_vendor == "CMA" and imp.assigned_person == "Lin Hsu"
    # 歷史 downtime:開 05/20 10:00 → 關 05/20 15:00,全生產時段 = 300 分,estimated
    assert imp.downtime_minutes == 300
    assert imp.downtime_estimated is True
    assert imp.opened_at == datetime.fromisoformat("2026-05-20T10:00:00+08:00")
    assert imp.closed_at == datetime.fromisoformat("2026-05-20T15:00:00+08:00")
    assert not hasattr(imp, "miscreated")  # miscreated 不入 dataclass


def test_row_to_import_open_has_no_downtime() -> None:
    imp = row_to_import(
        {"wo": "1", "compid": "EID-1", "wo_type": "PM", "workstatus": "O", "date_wo": "01/01/20"}
    )
    assert imp.status == "OPEN"
    assert imp.downtime_minutes is None  # 未結案 → 無 downtime
    assert imp.closed_at is None


def test_row_to_import_requires_keys() -> None:
    base = {
        "wo": "1",
        "compid": "EID-1",
        "wo_type": "PM",
        "workstatus": "H",
        "date_wo": "01/01/20",
    }
    assert row_to_import(base).work_order_no == 1
    with pytest.raises(ValueError):
        row_to_import({**base, "wo": ""})
    with pytest.raises(ValueError):
        row_to_import({**base, "wo": "abc"})
    with pytest.raises(ValueError):
        row_to_import({**base, "compid": ""})
    with pytest.raises(ValueError):
        row_to_import({**base, "date_wo": ""})
