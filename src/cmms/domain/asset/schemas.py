"""Asset 讀取 DTO(pydantic v2)。API / MCP 回傳這些,不直接吐 ORM 物件。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AssetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    asset_id: str
    description: str
    asset_type: str
    asset_subtype: str | None
    department: str
    line: str | None
    site: str
    model_no: str | None
    serial_no: str | None
    # 設備負責人(0031 起多負責人)。`owners` = 全部(依 position);`owner` = 首位(back-compat,
    # 相容 0029 additive 欄)。ORM `Asset` 已無這兩個屬性 → 呼叫端(API route)以 owners_map
    # 填入(from_attributes 缺屬性時取以下 default,不報錯)。
    owner: str | None = None
    owners: list[str] = []
    available_for_service: bool
    is_active: bool


class ExternalIdRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    namespace: str
    external_id: str


class AssetIdentityRead(BaseModel):
    """canonical 身分解析結果(ADR-015):asset_id + 所有外部系統 id。"""

    asset_id: str
    external_ids: list[ExternalIdRead]


class AssetRelationshipRead(BaseModel):
    """資產組成邊(ADR-018)。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    from_asset_id: str
    to_asset_id: str
    relationship_type: str  # contains_module | shared_dependency
    source: str  # mes_dependent_equipment | cmms_curated
    valid_from: datetime | None
    valid_to: datetime | None


class AssetTreeRead(BaseModel):
    """機台組成子樹(ADR-018):機台 + 所有 contains_module 後代模組 + 現行關係邊。"""

    asset_id: str
    descendant_asset_ids: list[str]
    relationships: list[AssetRelationshipRead]
