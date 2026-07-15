"""Asset ORM models(對應 docs/domain-model/01-assets.md §8)。

- lookup 表以 `code` 為 PK(asset_type / department / line / external_id_namespace)。
- `asset_subtype` 本切片存為 text(A3:受控 lookup 化延到 Inventory 切片)。
- `asset_external_id` 為 canonical 身分 crosswalk(ADR-015)。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class _CodeLabel:
    """lookup 共用:code(PK)+ label(人/agent 可讀,ADR-007)。"""

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class AssetType(_CodeLabel, Base):
    __tablename__ = "asset_type"  # Production/Support/Jig/Meter/Computer


class Department(_CodeLabel, Base):
    __tablename__ = "department"  # EQ/BD/QS/QA/SF/PE/SQE/PD/ME


class Line(_CodeLabel, Base):
    __tablename__ = "line"  # 1K/10K/Wet Loop/... 正規化後消除大小寫(0033:01K 更名 1K)


class ExternalIdNamespace(_CodeLabel, Base):
    __tablename__ = "external_id_namespace"  # mes_equipment / layer_b_sensor / ...


class Asset(AuditMixin, Base):
    __tablename__ = "asset"

    # 識別與分類
    asset_id: Mapped[str] = mapped_column(String, primary_key=True)  # EID-xxxxx,不可變(A4)
    description: Mapped[str] = mapped_column(String, nullable=False)
    parent_asset_id: Mapped[str | None] = mapped_column(ForeignKey("asset.asset_id"), nullable=True)
    asset_type: Mapped[str] = mapped_column(ForeignKey("asset_type.code"), nullable=False)
    asset_subtype: Mapped[str | None] = mapped_column(String, nullable=True)  # A3:text
    # 實務必填,但 legacy 1 筆(EID-70029)空 → nullable;Jordan 補後可再收緊
    department: Mapped[str | None] = mapped_column(ForeignKey("department.code"), nullable=True)
    process_segment_class: Mapped[str | None] = mapped_column(String, nullable=True)  # [UI]

    # 廠內位置
    line: Mapped[str | None] = mapped_column(ForeignKey("line.code"), nullable=True)
    site: Mapped[str] = mapped_column(String, nullable=False, default="PLANT-1")
    building: Mapped[str | None] = mapped_column(String, nullable=True)  # [UI]
    floor_level: Mapped[str | None] = mapped_column(String, nullable=True)  # [UI]
    room_space: Mapped[str | None] = mapped_column(String, nullable=True)  # [UI]

    # 製造商與識別
    manufacturer: Mapped[str | None] = mapped_column(String, nullable=True)  # [UI]
    model_no: Mapped[str | None] = mapped_column(String, nullable=True)
    serial_no: Mapped[str | None] = mapped_column(String, nullable=True)

    # 設備負責人(維修/保養工單 assignee 的單一事實來源)自 0031 起改由 `asset_owner`
    # 交叉表承載(多負責人);單一 owner 欄已移除。

    # 狀態旗標(A1e:兩欄並存)。server_default 讓 Core upsert(載入器)免帶值也安全
    available_for_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    up_down_tracking: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # [UI]

    # 整合與雜項([UI],暫空)
    host_name: Mapped[str | None] = mapped_column(String, nullable=True)
    asset_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    product: Mapped[str | None] = mapped_column(String, nullable=True)
    weblink: Mapped[str | None] = mapped_column(String, nullable=True)
    picture_url: Mapped[str | None] = mapped_column(String, nullable=True)
    comments: Mapped[str | None] = mapped_column(Text, nullable=True)


class AssetOwner(AuditMixin, Base):
    """設備負責人(多對一;0031)。難維護機台可有多位負責人,報修/PM 皆自動指派並通知全部。

    所有負責人平等(除 `position` 排序外無「主要」概念);`position` 只決定回填相容單值欄
    (分析平台 `assigned_person`=首位)與顯示順序。`person_name` = legacy 確切字串(非 FK,比照
    `work_order.assigned_person`)。複合 PK (asset_id, person_name) 防同機同人重複。
    """

    __tablename__ = "asset_owner"
    __table_args__ = (Index("ix_asset_owner_person", "person_name"),)

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.asset_id"), primary_key=True)
    person_name: Mapped[str] = mapped_column(String, primary_key=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class AssetExternalId(AuditMixin, Base):
    """canonical 身分 crosswalk(ADR-015)。一個外部 id 只對一台機器;一台機器可多個外部 id。"""

    __tablename__ = "asset_external_id"
    __table_args__ = (UniqueConstraint("asset_id", "namespace", "external_id"),)

    namespace: Mapped[str] = mapped_column(
        ForeignKey("external_id_namespace.code"), primary_key=True
    )
    external_id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.asset_id"), nullable=False)


class AssetRelationshipType(_CodeLabel, Base):
    __tablename__ = "asset_relationship_type"  # contains_module / shared_dependency


class AssetRelationship(AuditMixin, Base):
    """資產組成圖(ADR-018)。兩型,direction 依型別:
    - `contains_module`:`from`=機台、`to`=模組(1:N 嚴格無環樹)。
    - `shared_dependency`:`from`=共用資源、`to`=被服務機台(N:M 圖)。

    權威來源於本表;`asset.parent_asset_id` 為 `contains_module` 單親的 denormalized 快取。
    `valid_to IS NULL` = 現行;partial unique 防同型同對重複現行邊(保留歷史)。
    """

    __tablename__ = "asset_relationship"
    __table_args__ = (
        Index(
            "uq_asset_relationship_active",
            "from_asset_id",
            "to_asset_id",
            "relationship_type",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
        ),
        CheckConstraint("from_asset_id <> to_asset_id", name="ck_asset_relationship_no_self"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_asset_id: Mapped[str] = mapped_column(
        ForeignKey("asset.asset_id"), nullable=False, index=True
    )
    to_asset_id: Mapped[str] = mapped_column(
        ForeignKey("asset.asset_id"), nullable=False, index=True
    )
    relationship_type: Mapped[str] = mapped_column(
        ForeignKey("asset_relationship_type.code"), nullable=False
    )
    source: Mapped[str] = mapped_column(
        String, nullable=False
    )  # mes_dependent_equipment | cmms_curated
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
