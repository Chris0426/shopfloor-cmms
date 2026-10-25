"""timefmt 純函式單元測試(無 DB):UTC instant → Asia/Taipei 顯示;日曆日不位移。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from cmms.web.timefmt import MISSING, local_date, local_dt, to_plant_time


def test_none_renders_missing_marker():
    assert local_dt(None) == MISSING == "—"
    assert local_date(None) == "—"


def test_naive_is_treated_as_utc():
    assert local_dt(datetime(2026, 10, 1, 6, 58, 54)) == "2026-10-01 14:58"


def test_aware_utc_converted_to_taipei():
    assert local_dt(datetime(2026, 10, 1, 6, 58, 54, tzinfo=UTC)) == "2026-10-01 14:58"


def test_aware_plus_eight_input_unchanged_wall_clock():
    tpe = timezone(timedelta(hours=8))
    assert local_dt(datetime(2026, 10, 1, 14, 58, tzinfo=tpe)) == "2026-10-01 14:58"


def test_other_offset_input_converted():
    est = timezone(timedelta(hours=-5))
    assert local_dt(datetime(2026, 10, 1, 1, 58, tzinfo=est)) == "2026-10-01 14:58"


def test_crosses_midnight_into_next_local_day():
    v = datetime(2026, 7, 3, 16, 0, tzinfo=UTC)
    assert local_dt(v) == "2026-07-04 00:00"
    assert local_date(v) == "2026-07-04"


def test_dst_free_fixed_offset_all_year():
    # Asia/Taipei 無 DST:一月與七月同為 +08:00
    for month in (1, 3, 7, 11):
        assert to_plant_time(datetime(2026, month, 15, 0, 0, tzinfo=UTC)).utcoffset() == timedelta(
            hours=8
        )
        assert local_dt(datetime(2026, month, 15, 0, 0, tzinfo=UTC), "%H:%M") == "08:00"


def test_format_passthrough():
    v = datetime(2026, 10, 1, 6, 58, 54, tzinfo=UTC)
    assert local_dt(v, "%m/%d %H:%M") == "10/01 14:58"
    assert local_dt(v, "%m-%d %H:%M") == "10-01 14:58"
    assert local_dt(v, "%m/%d") == "10/01"
    assert local_dt(v, "%Y-%m-%dT%H:%M:%S") == "2026-10-01T14:58:54"


def test_calendar_date_is_not_shifted():
    assert local_date(date(2026, 10, 1)) == "2026-10-01"
    assert local_dt(date(2026, 10, 1), "%m/%d") == "10/01"
