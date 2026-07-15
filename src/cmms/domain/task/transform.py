"""tasks.csv → 匯入資料的純函式(無 DB,可單元測試)。

tasks.csv 為 utf-8-sig 乾淨檔,實際只有 2 欄(task_no / task_desc;表頭尾端有一個
多餘逗號形成的空欄,以表頭名對應即可忽略)。欄位以表頭名對應(非位置)。
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True, slots=True)
class TaskImport:
    """一筆待匯入 task(tasks.csv 提供的 2 欄)。"""

    task_no: str
    description: str


@dataclass(frozen=True, slots=True)
class TaskStepImport:
    """task_steps_parts.csv 一列:一個步驟 + 可選一個零件(item/replaceqty;migration 0016)。"""

    task_no: str
    proc_seq: int | None
    task_desc: str
    item_code: str | None
    replace_qty: Decimal | None


def clean(value: str | None) -> str | None:
    """去空白;空字串視為 None。"""
    if value is None:
        return None
    v = value.strip()
    return v or None


def _unescape(value: str | None) -> str | None:
    v = clean(value)
    return html.unescape(v) if v is not None else None


def parse_int(value: str | None) -> int | None:
    """整數序號;非整數 → None(依 id 排序;不因怪序號丟步驟)。"""
    v = clean(value)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def parse_decimal(value: str | None) -> Decimal | None:
    """更換量;空/非數 → None(造冊未清點 → 保持原狀,owner 再填)。"""
    v = clean(value)
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation:
        return None


def row_to_step_import(row: dict[str, str | None]) -> TaskStepImport:
    """單列 → TaskStepImport。缺 task_no/task_desc → raise(loader 計 malformed)。"""
    task_no = clean(row.get("task_no"))
    task_desc = _unescape(row.get("task_desc"))
    if not task_no or not task_desc:
        raise ValueError("task_step row missing task_no/task_desc")
    return TaskStepImport(
        task_no=task_no,
        proc_seq=parse_int(row.get("proc_seq")),
        task_desc=task_desc,
        item_code=clean(row.get("item")),  # 來源已大寫
        replace_qty=parse_decimal(row.get("replaceqty")),
    )


def row_to_import(row: dict[str, str | None]) -> TaskImport:
    """單列 CSV(dict,鍵為表頭)→ TaskImport。"""
    task_no = clean(row.get("task_no"))
    if not task_no:
        raise ValueError("row missing task_no")
    # description 必填;極端情況(空白描述)退回以 task_no 充當,確保非空且可追溯
    description = clean(row.get("task_desc")) or task_no
    return TaskImport(task_no=task_no, description=description)
