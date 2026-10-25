"""Web console 時間顯示(純函式,可單元測試)。

規則:DB 一律存 UTC instant(timestamptz);console 畫面一律以廠區時區 Asia/Taipei 顯示。
JSON read API 不經此處(維持 UTC `Z`,golden fixtures 契約)。
- instant(`datetime`):naive 視為 UTC;aware 轉 Asia/Taipei 後依 `fmt` 格式化。
- 日曆日(`date`,如 `scheduled_date` / `next_due_date`):不是 instant,**不位移**,照存值格式化。
- None → `MISSING`("—",與模板既有缺值寫法一致)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from cmms.domain.work_order.transform import TAIPEI

MISSING = "—"
DEFAULT_DT_FMT = "%Y-%m-%d %H:%M"
DEFAULT_DATE_FMT = "%Y-%m-%d"


def to_plant_time(value: datetime) -> datetime:
    """instant → 廠區(Asia/Taipei,固定 +08:00、無 DST)aware datetime;naive 視為 UTC。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(TAIPEI)


def local_dt(value: datetime | date | None, fmt: str = DEFAULT_DT_FMT) -> str:
    """顯示用:instant 轉 Asia/Taipei 後格式化;`date`(非 instant)照存值格式化;None → "—"。"""
    if value is None:
        return MISSING
    if isinstance(value, datetime):
        return to_plant_time(value).strftime(fmt)
    if isinstance(value, date):
        return value.strftime(fmt)  # 日曆日不位移
    return str(value)


def local_date(value: datetime | date | None, fmt: str = DEFAULT_DATE_FMT) -> str:
    """instant 的廠區「當地日期」(06:58Z 前後跨日以 Taipei 為準);`date` 照存值。"""
    return local_dt(value, fmt)
