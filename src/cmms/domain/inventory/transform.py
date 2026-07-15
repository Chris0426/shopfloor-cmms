"""inventory.csv → 匯入資料的純函式(無 DB,可單元測試)。

loader 以 **cp1252** 讀檔(™ 等);此處 `clean_text` 再做 `html.unescape`(μ 等)。
畸形行(欄位數≠20)由 loader 先試 `repair_malformed_line` 修回,真正不可救才跳過(A3b)。
多值欄(asset_sub/alt/kit)拆 token。
"""

from __future__ import annotations

import csv
import html
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

# A3 canonical 整併(2026-06-21 recon 分析;保守 token-sort 唯一明顯變體)。
# 無法機械判斷的子類型差異列清單給 Jordan 逐個釐清後,再擴充本表。
ASSET_SUBTYPE_ALIASES: dict[str, str] = {
    "STA1 CALIBRATOR": "CALIBRATOR STA1",
}


# 畸形行修復(A3b,2026-06-21 Jordan 採選項 A:loader 自動修復,原始 CSV 不動、修復邏輯進 git)。
# 成因:descrip 內未跳脫的英吋符號 "(如 3/8"、.046")致 RFC-4180 欄位錯位(欄位數≠20)。
# 前 4 欄(item/asset_sub/sf_desc/vpartno,無內嵌引號)與 location 之後的數值/旗標區塊形狀固定,
# 以此二者為錨,把中間的 descrip 原樣接回(英吋符號保留;Jordan 指示 SUNRISE 等品名原樣載入)。
_LEFT4 = re.compile(r'^"([^"]*)","([^"]*)","([^"]*)","([^"]*)",')
_TAIL_ANCHOR = re.compile(
    r'","(?P<location>[^"]*)",'  # descrip 的(錯位)結束引號 + location
    r"(?P<orderpt>\d+\.\d+),(?P<onhand>\d+\.\d+),(?P<cost>\d+\.\d+),"  # 3 個小數
    r"(?P<lead_time>\d+),(?P<obsol>[FT]),(?P<stock>[FT]),"  # 整數 + 兩個 F/T 旗標
)


def repair_malformed_line(raw_line: str) -> list[str] | None:
    """欄位數≠20 的畸形行 → 修回 20 欄位清單;無法錨定回 None(真正不可救,交 loader 跳過)。

    `_TAIL_ANCHOR` 的「3 小數 + 整數 + 兩旗標」形狀極具辨識度,不會誤命中 descrip 內文;
    `descrip` = 前 4 欄之後、location 之前的全部文字,原樣保留。詳見 domain model 文件。
    """
    left = _LEFT4.match(raw_line)
    if left is None:
        return None
    rest = raw_line[left.end() :]  # 由 descrip 的起始引號開始
    anchor = _TAIL_ANCHOR.search(rest)
    if anchor is None:
        return None
    descrip = rest[1 : anchor.start()]  # 去 descrip 起始引號;到其錯位的結束引號前(原樣)
    tail = next(csv.reader([rest[anchor.end() :]]))  # supplier..comment + 尾端空欄(形狀正常)
    return [
        left.group(1),  # item
        left.group(2),  # asset_sub
        left.group(3),  # sf_desc
        left.group(4),  # vpartno
        descrip,
        anchor.group("location"),
        anchor.group("orderpt"),
        anchor.group("onhand"),
        anchor.group("cost"),
        anchor.group("lead_time"),
        anchor.group("obsol"),
        anchor.group("stock"),
        *tail,
    ]


@dataclass(frozen=True, slots=True)
class InventoryItemImport:
    """一筆待匯入品項本體(不含多值關聯)。"""

    item_code: str
    item_category: str | None
    name: str | None
    description: str | None
    vendor_part_no: str | None
    quantity_on_hand: Decimal | None
    reorder_point: Decimal | None
    lead_time_weeks: int | None
    unit_cost: Decimal | None
    bin_location: str | None
    supplier: str | None
    weblink: str | None
    photo_ref: str | None
    comment: str | None
    is_stocked: bool
    is_obsolete: bool


@dataclass(frozen=True, slots=True)
class ParsedInventoryRow:
    """品項本體 + 多值關聯 token(loader 據此建 junction)。"""

    item: InventoryItemImport
    asset_subtypes: list[str] = field(default_factory=list)  # canonical 後
    alternatives: list[str] = field(default_factory=list)  # alt_item
    kit_parents: list[str] = field(default_factory=list)  # parnt_item(本品項的父)
    kit_children: list[str] = field(default_factory=list)  # child_item(本品項的子)


def clean(value: str | None) -> str | None:
    """去空白;空字串 / `nan` 視為 None。"""
    if value is None:
        return None
    v = value.strip()
    if not v or v.lower() == "nan":
        return None
    return v


def clean_text(value: str | None) -> str | None:
    """文字欄:去空白 + HTML-entity 還原(`&#956;`→μ);cp1252 解碼已在 loader 讀檔完成。"""
    v = clean(value)
    return html.unescape(v) if v is not None else None


def parse_decimal(value: str | None) -> Decimal | None:
    v = clean(value)
    if v is None:
        return None
    try:
        return Decimal(v)
    except InvalidOperation as e:
        raise ValueError(f"bad decimal: {value!r}") from e


def parse_int(value: str | None) -> int | None:
    v = clean(value)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        # lead_time 偶有小數字串(如 "4.0")→ 取整數部分
        return int(float(v))


def parse_bool(value: str | None, *, default: bool) -> bool:
    """`T`→True、`F`→False、空→default。"""
    v = clean(value)
    if v is None:
        return default
    return v.upper() == "T"


def split_multi(value: str | None) -> list[str]:
    """逗號多值 → 去空白、去重(保序)的 token 清單。"""
    v = clean(value)
    if v is None:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for p in v.split(","):
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def canonical_subtype(value: str) -> str:
    """套用 A3 alias 整併(明顯變體 → canonical)。"""
    v = value.strip()
    return ASSET_SUBTYPE_ALIASES.get(v, v)


def _item_category(item_code: str) -> str | None:
    """ES/EC 前綴(legacy;已混用,僅作溯源)。"""
    prefix = item_code[:2].upper()
    return prefix if prefix in ("ES", "EC") else None


def row_to_import(row: dict[str, str | None]) -> ParsedInventoryRow:
    """單列 CSV(dict,鍵為表頭)→ ParsedInventoryRow。"""
    item_code = clean(row.get("item"))
    if not item_code:
        raise ValueError("row missing item (item_code)")

    item = InventoryItemImport(
        item_code=item_code,
        item_category=_item_category(item_code),
        name=clean_text(row.get("sf_desc")),
        description=clean_text(row.get("descrip")),
        vendor_part_no=clean(row.get("vpartno")),
        quantity_on_hand=parse_decimal(row.get("onhand")),
        reorder_point=parse_decimal(row.get("orderpt")),
        lead_time_weeks=parse_int(row.get("lead_time")),
        unit_cost=parse_decimal(row.get("cost")),
        bin_location=clean(row.get("location")),
        supplier=clean(row.get("supplier")),
        weblink=clean(row.get("weblink")),
        photo_ref=clean(row.get("photo")),
        comment=clean_text(row.get("comment")),
        is_stocked=parse_bool(row.get("stock"), default=True),
        is_obsolete=parse_bool(row.get("obsol"), default=False),
    )
    return ParsedInventoryRow(
        item=item,
        asset_subtypes=[canonical_subtype(s) for s in split_multi(row.get("asset_sub"))],
        alternatives=split_multi(row.get("alt_item")),
        kit_parents=split_multi(row.get("parnt_item")),
        kit_children=split_multi(row.get("child_item")),
    )
