"""inventory.csv 載入器(migration 資料輸入)。

經 InventoryService 單一寫入路徑寫入(ADR-001),idempotent。
★ 編碼 cp1252(™ 等);★ 畸形行(欄位數≠20,descrip 內未跳脫英吋符號 ")先試自動修復、
回報修復 item code;真正不可救才跳過(A3b,Jordan 採選項 A,2026-06-21)。
asset_subtype canonical lookup 由 inventory + asset 兩來源 union 種子(A3);多值欄拆 junction,
孤兒邊(指向未載入品項)跳過。前置:asset 已載入(供 A3 子類型 union + 軟參照)。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.asset.models import Asset
from cmms.domain.inventory.models import AssetSubtype, ItemCategory
from cmms.domain.inventory.service import InventoryService
from cmms.domain.inventory.transform import (
    ParsedInventoryRow,
    canonical_subtype,
    repair_malformed_line,
    row_to_import,
)

MIGRATION_ACTOR = Actor.human("migration")

ITEM_CATEGORY_LABELS: dict[str, str] = {
    "ES": "ES (legacy prefix; mixed use, not a reliable spare/consumable class)",
    "EC": "EC (legacy prefix; mixed use, not a reliable spare/consumable class)",
}

_EXPECTED_COLS = 20  # 19 欄 + 尾端逗號空欄


@dataclass(frozen=True, slots=True)
class LoadResult:
    items: int
    skipped_rows: int  # 修復後仍畸形(欄位數≠20)跳過數
    item_categories: int
    asset_subtypes: int
    asset_subtype_links: int
    alt_links: int
    kit_links: int
    orphan_links_skipped: int  # alt/kit 指向未載入品項而跳過的邊數
    skipped_items: list[str] = field(default_factory=list)  # 仍跳過畸形行的 item code
    repaired_rows: int = 0  # 自動修復回來的畸形行數(A3b)
    repaired_items: list[str] = field(default_factory=list)  # 修復回來的 item code


def read_rows(
    path: Path,
) -> tuple[list[dict[str, str | None]], list[tuple[str, int]], list[str]]:
    """讀 inventory.csv(cp1252)。回 (乾淨列, 仍畸形列[(item, 欄位數)], 已修復 item codes)。

    欄位數≠20 的畸形行(descrip 內未跳脫英吋符號 ")先試 `repair_malformed_line` 修回 20 欄;
    成功併入乾淨列並記入 repaired,僅真正無法錨定者跳過(A3b,選項 A)。本檔無跨行欄位
    (已驗 1331 record == 1331 physical line),故逐物理行解析安全;item(第 0 欄)不受位移影響。
    """
    with path.open(encoding="cp1252", newline="") as fh:
        raw_lines = fh.readlines()
    header = next(csv.reader([raw_lines[0]]))
    valid: list[dict[str, str | None]] = []
    skipped: list[tuple[str, int]] = []
    repaired: list[str] = []
    for line in raw_lines[1:]:
        raw = line.rstrip("\r\n")
        if not raw.strip():
            continue
        row = next(csv.reader([raw]))
        if len(row) == len(header):
            valid.append(dict(zip(header, row, strict=True)))
            continue
        fixed = repair_malformed_line(raw)
        if fixed is not None and len(fixed) == len(header):
            valid.append(dict(zip(header, fixed, strict=True)))
            repaired.append(fixed[0].strip())
        else:
            skipped.append((row[0].strip() if row else "?", len(row)))
    return valid, skipped, repaired


async def _asset_subtypes_from_db(session: AsyncSession) -> set[str]:
    """既有 asset 表的 distinct 子類型(canonical 後)— 供 A3 lookup 完整涵蓋 asset 側。"""
    stmt = select(Asset.asset_subtype).where(Asset.asset_subtype.is_not(None)).distinct()
    return {canonical_subtype(v) for v in (await session.scalars(stmt)).all() if v}


async def load(
    rows: Iterable[dict[str, str | None]],
    session: AsyncSession,
    *,
    skipped: list[tuple[str, int]] | None = None,
    repaired: list[str] | None = None,
) -> LoadResult:
    parsed: list[ParsedInventoryRow] = [row_to_import(r) for r in rows]
    loaded_codes = {p.item.item_code for p in parsed}

    item_categories = sorted({p.item.item_category for p in parsed if p.item.item_category})
    inv_subtypes = {s for p in parsed for s in p.asset_subtypes}

    service = InventoryService(session)
    asset_subtypes = sorted(inv_subtypes | await _asset_subtypes_from_db(session))

    alt_links = kit_links = asset_subtype_links = orphan_skipped = 0
    async with service.write(MIGRATION_ACTOR):
        for code in item_categories:
            await service.upsert_lookup(ItemCategory, code, ITEM_CATEGORY_LABELS.get(code, code))
        for code in asset_subtypes:
            await service.upsert_lookup(AssetSubtype, code, code)  # label 待 ADR-007 enrich
        for p in parsed:
            await service.upsert_item(p.item, MIGRATION_ACTOR)

        # junction(品項全載入後;孤兒邊跳過)
        for p in parsed:
            for sub in p.asset_subtypes:  # subtype 已 canonical 且皆在 lookup
                await service.link_asset_subtype(p.item.item_code, sub, MIGRATION_ACTOR)
                asset_subtype_links += 1
            for alt in p.alternatives:
                if alt in loaded_codes and alt != p.item.item_code:
                    await service.link_alternative(p.item.item_code, alt, MIGRATION_ACTOR)
                    alt_links += 1
                else:
                    orphan_skipped += 1

        # BOM:union parnt_item(父→本) + child_item(本→子),去重、去孤兒、去自環
        kit_edges: set[tuple[str, str]] = set()
        for p in parsed:
            code = p.item.item_code
            kit_edges.update((par, code) for par in p.kit_parents)
            kit_edges.update((code, ch) for ch in p.kit_children)
        for parent, child in sorted(kit_edges):
            if parent in loaded_codes and child in loaded_codes and parent != child:
                await service.link_kit(parent, child, MIGRATION_ACTOR)
                kit_links += 1
            else:
                orphan_skipped += 1

    return LoadResult(
        items=len(parsed),
        skipped_rows=len(skipped) if skipped is not None else 0,
        item_categories=len(item_categories),
        asset_subtypes=len(asset_subtypes),
        asset_subtype_links=asset_subtype_links,
        alt_links=alt_links,
        kit_links=kit_links,
        orphan_links_skipped=orphan_skipped,
        skipped_items=[code for code, _ in (skipped or [])],
        repaired_rows=len(repaired) if repaired is not None else 0,
        repaired_items=list(repaired or []),
    )
