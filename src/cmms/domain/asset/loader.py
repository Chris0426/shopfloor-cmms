"""assets.csv 載入器(migration 資料輸入)。

經 AssetService 單一寫入路徑寫入(ADR-001),idempotent(可重跑)。
lookup(asset_type / department / line)由資料中的相異值動態種子(label 先＝code,
之後再以 Jordan 提供的中文意思 enrich,ADR-007)。
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor
from cmms.domain.asset.models import AssetType, Department, Line
from cmms.domain.asset.service import AssetError, AssetService, UnknownAssetError
from cmms.domain.asset.transform import (
    CONTAINS_MODULE,
    RELATIONSHIP_TYPES,
    SOURCE_MES_DEP,
    AssetImport,
    classify_dependent_equipment,
    clean,
    row_to_import,
)

# 一次性 migration 匯入：人工執行的資料載入
MIGRATION_ACTOR = Actor.human("migration")

# 部門代碼 → label(ADR-007 可讀 metadata)。code 找不到時 fallback＝code 原樣。
# label 為通用示意名(demo 用);正式環境由各廠自行維護對照。
DEPARTMENT_LABELS: dict[str, str] = {
    "EQ": "Equipment Engineering",
    "QA": "Quality Assurance",
    "PE": "Process Engineering",
    "BD": "Business Development",
    "QS": "Quality Systems",
    "SQE": "Supplier Quality",
    "PD": "Product Development",
    "ME": "Manufacturing Engineering",
    "SF": "Shopfloor (in-house)",
}


@dataclass(frozen=True, slots=True)
class LoadResult:
    assets: int
    asset_types: int
    departments: int
    lines: int


def read_rows(path: Path) -> list[dict[str, str | None]]:
    """讀 assets.csv(utf-8-sig)。以表頭名對應欄位。"""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


async def load(rows: Iterable[dict[str, str | None]], session: AsyncSession) -> LoadResult:
    imports: list[AssetImport] = [row_to_import(r) for r in rows]

    asset_types = sorted({i.asset_type for i in imports})
    departments = sorted({i.department for i in imports if i.department})
    lines = sorted({i.line for i in imports if i.line})

    service = AssetService(session)
    async with service.write(MIGRATION_ACTOR):
        for code in asset_types:
            await service.upsert_lookup(AssetType, code, code)
        for code in departments:
            await service.upsert_lookup(Department, code, DEPARTMENT_LABELS.get(code, code))
        for code in lines:
            await service.upsert_lookup(Line, code, code)
        for imp in imports:
            await service.upsert_asset(imp, MIGRATION_ACTOR)

    return LoadResult(
        assets=len(imports),
        asset_types=len(asset_types),
        departments=len(departments),
        lines=len(lines),
    )


# ---- 資產組成圖載入(ADR-018)----

# relationship_type → label(ADR-007 可讀 metadata)。
RELATIONSHIP_TYPE_LABELS: dict[str, str] = {
    "contains_module": "機台內含模組(containment,1:N 樹)",
    "shared_dependency": "共用資源服務機台(N:M 圖)",
}


# MES dependent-equipment 匯出欄位(Analytics 提供;見 下游契約登記)。
DEP_EXPORT_PARENT_COL = "parent_eid"
DEP_EXPORT_CHILD_COL = "child_eid"

# 策展排除:少數非生產 IT 資產(如報表 DB)(非生產 IT 資產;D6 裁決排除,2026-06-27)。實測這 3 個
# EID 本就不在 asset 主檔(skip「實測多餘」)→ 帶上只是把涉及它們的邊乾淨歸為 curated
# 而非 unknown,稽核更清楚。CLI 預設套用;測試與其他呼叫端可覆寫。
CURATED_NONPRODUCTION_SKIP: frozenset[str] = frozenset({"EID-70007", "EID-70008", "EID-70011"})


def read_dependent_equipment_rows(path: Path) -> list[tuple[str, str]]:
    """讀 Analytics `MES-dependent-equipment-export.csv` → (parent_eid, child_eid) 邊。

    欄位:parent_eqpPK/parent_eid/parent_desc/child_eqpPK/child_eid/child_desc/edge_class。
    只取 parent_eid / child_eid;分類由 `classify_dependent_equipment` 依基數重判(不靠 edge_class)。
    """
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return [
            (clean(r.get(DEP_EXPORT_PARENT_COL)) or "", clean(r.get(DEP_EXPORT_CHILD_COL)) or "")
            for r in reader
        ]


@dataclass(frozen=True, slots=True)
class RelationshipLoadResult:
    raw_edges: int  # 原始邊數(CSV 讀入,含重複/self-loop/null)
    classified: int  # 分類去重後的相異邊數(= bound + 三類 skip 的總和)
    relationship_types: int
    contains_module: int
    shared_dependency: int
    skipped_unknown_eid: int  # 端點 EID 不在 asset 主檔(Q6),跳過並記錄
    skipped_guard: int  # 守門違反(成環/多重容器,多為 raw 雙向邊),跳過並記錄
    skipped_curated: int  # 策展排除(如非生產 IT 資產),跳過並記錄

    @property
    def bound(self) -> int:
        """實際綁定的邊(contains + shared)。"""
        return self.contains_module + self.shared_dependency

    @property
    def dropped(self) -> int:
        """未綁定的邊(unknown + guard + curated)。"""
        return self.skipped_unknown_eid + self.skipped_guard + self.skipped_curated


async def load_relationships(
    edges: list[tuple[str, str]],
    session: AsyncSession,
    *,
    skip_asset_ids: set[str] | None = None,
) -> RelationshipLoadResult:
    """把 MES 相依設備匯出的 (parent, child) 邊分類後載入 `asset_relationship`。

    分類規則見 `classify_dependent_equipment`(同 child 多 parent → shared_dependency;
    self-loop / 重複邊已在該函式去除)。**idempotent**(重跑不重複現行邊)。
    跳過並計數三類(不阻擋整批):
    - `skipped_unknown_eid`:端點 EID 不在主檔(Q6,不靜默建資產)。
    - `skipped_guard`:成環 / 多重容器(raw 雙向邊的產物;分析平台下游交付 已預警)。
    - `skipped_curated`:`skip_asset_ids` 指定排除(如非生產 IT 資產 MES DB Server;
      production-filter 策展政策待 Jordan,本函式只提供機制)。
    """
    skip = skip_asset_ids or set()
    classified = classify_dependent_equipment(edges)
    service = AssetService(session)
    cm = sd = unknown = guard = curated = 0
    async with service.write(MIGRATION_ACTOR):
        for code in RELATIONSHIP_TYPES:
            await service.upsert_relationship_type(code, RELATIONSHIP_TYPE_LABELS[code])
        for imp in classified:
            if imp.from_asset_id in skip or imp.to_asset_id in skip:
                curated += 1
                continue
            try:
                if imp.relationship_type == CONTAINS_MODULE:
                    await service.link_containment(
                        imp.from_asset_id, imp.to_asset_id, MIGRATION_ACTOR, source=SOURCE_MES_DEP
                    )
                    cm += 1
                else:
                    await service.link_shared_dependency(
                        imp.from_asset_id, imp.to_asset_id, MIGRATION_ACTOR, source=SOURCE_MES_DEP
                    )
                    sd += 1
            except UnknownAssetError:
                unknown += 1  # Q6:端點不在主檔
            except AssetError:
                guard += 1  # 成環 / 多重容器(雙向邊產物)

    return RelationshipLoadResult(
        raw_edges=len(edges),
        classified=len(classified),
        relationship_types=len(RELATIONSHIP_TYPES),
        contains_module=cm,
        shared_dependency=sd,
        skipped_unknown_eid=unknown,
        skipped_guard=guard,
        skipped_curated=curated,
    )
