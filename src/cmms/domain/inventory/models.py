"""Inventory ORM models(對應 docs/domain-model/04-inventory.md §8)。

- lookup:`item_category`(ES/EC,legacy 前綴)、`asset_subtype`(canonical,A3 共用)。
- 主表 `inventory_item`;3 個多值 junction(asset_subtype / alternative / kit BOM)。
- supplier 存 text(FK→company 延 Contacts 切片);uom 無資料不建。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class _CodeLabel:
    """lookup 共用:code(PK)+ label(人/agent 可讀,ADR-007)。"""

    code: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String, nullable=False)


class ItemCategory(_CodeLabel, Base):
    __tablename__ = "item_category"  # ES / EC(legacy 前綴,已混用,非可靠分類)


class AssetSubtype(_CodeLabel, Base):
    __tablename__ = "asset_subtype"  # canonical 子類型(A3;asset + inventory 共用參照)


class InventoryItem(AuditMixin, Base):
    __tablename__ = "inventory_item"

    item_code: Mapped[str] = mapped_column(String, primary_key=True)  # item
    item_category: Mapped[str | None] = mapped_column(
        ForeignKey("item_category.code"), nullable=True
    )  # ES/EC 前綴
    name: Mapped[str | None] = mapped_column(String, nullable=True)  # sf_desc
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # descrip(已清洗;防禦 nullable)
    vendor_part_no: Mapped[str | None] = mapped_column(String, nullable=True)  # vpartno

    quantity_on_hand: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 3), nullable=True
    )  # onhand
    reorder_point: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # orderpt
    # 再訂購量(ADR-026;Jordan 補匯 orderqty CSV。缺 → RFQ 退回 reorder_point − on_hand)
    reorder_quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    lead_time_weeks: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # lead_time(I3 待確認)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 5), nullable=True)  # cost
    currency: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'USD'")
    )  # I2:單一 USD

    bin_location: Mapped[str | None] = mapped_column(String, nullable=True)  # location
    supplier: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # 自由文字供應商名(legacy);解析後連 supplier_org_id
    # supplier→organization 連結(ADR-026;由 supplier 文字對 organization.name 解析,~95% 命中;
    # 對不上者留 NULL、RFQ-ineligible 待人工連)
    supplier_org_id: Mapped[str | None] = mapped_column(
        ForeignKey("organization.org_id"), nullable=True, index=True
    )
    weblink: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # photo(eMaint doc id)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_stocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )  # stock
    is_obsolete: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )  # obsol


class InventoryItemAssetSubtype(AuditMixin, Base):
    """N:M — 品項適用哪些設備子類型(A3 canonical)。"""

    __tablename__ = "inventory_item_asset_subtype"

    item_code: Mapped[str] = mapped_column(ForeignKey("inventory_item.item_code"), primary_key=True)
    asset_subtype: Mapped[str] = mapped_column(ForeignKey("asset_subtype.code"), primary_key=True)


class InventoryItemAlternative(AuditMixin, Base):
    """N:M(自參照)— 替代品。"""

    __tablename__ = "inventory_item_alternative"

    item_code: Mapped[str] = mapped_column(ForeignKey("inventory_item.item_code"), primary_key=True)
    alt_item_code: Mapped[str] = mapped_column(
        ForeignKey("inventory_item.item_code"), primary_key=True
    )


class InventoryItemKit(AuditMixin, Base):
    """套件 BOM(自參照)— parent 含 child。雙向(parnt_item / child_item)union 後去重。"""

    __tablename__ = "inventory_item_kit"

    parent_item_code: Mapped[str] = mapped_column(
        ForeignKey("inventory_item.item_code"), primary_key=True
    )
    child_item_code: Mapped[str] = mapped_column(
        ForeignKey("inventory_item.item_code"), primary_key=True
    )


class StorageBin(Base):
    """備品儲位受控詞彙(倉庫實體櫃位)。code 本身即人讀值(如 "02A" / "CMA" / "Drawer"),
    無 label。additive-only:退役由 admin 治理(is_active),不刪除;既有品項持 legacy 髒值
    不受此表約束(編輯時原樣放行,見 InventoryService._validate_bin_location)。"""

    __tablename__ = "storage_bin"

    code: Mapped[str] = mapped_column(String, primary_key=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )


class StockTxnKind(_CodeLabel, Base):
    __tablename__ = "stock_txn_kind"  # ISSUE / RETURN / ADJUST / RECEIVE


class StockTransaction(AuditMixin, Base):
    """庫存異動帳(ADR-005:庫存量不可裸 UPDATE,一律經此帳並調整 on_hand)。

    #4b 引入(Inventory 切片 I5/I6 延後項)。領料 = `issue_part_to_work_order` 開一筆
    `kind=ISSUE`、`qty_delta<0`、連結 work_order;`idempotency_key` 防重(ADR-006)。
    ★ 歷史回填(`backfill_part_issue`)亦寫此帳但 `adjust_on_hand=False`(不動 on_hand);
      故本帳**非完整異動史**(on_hand 由 eMaint snapshot 起算 + 僅部分回填)——
      `sum(qty_delta) ≠ on_hand`,**勿據本帳反推 on_hand**。
    ★ 領料歸屬(ADR-024):`kind='ISSUE'` 恰含 `work_order_no`(工單領料)xor
      `charge_target_asset_id`(非工單直領,歸屬設備);CHECK 守門、禁孤兒/雙重歸屬。
      非 ISSUE(RECEIVE/ADJUST/RETURN)不受此約束。
    """

    __tablename__ = "stock_transaction"
    # 與 migration 0015 對齊(ADR-024;否則 alembic check 漂移):
    # ISSUE 恰有一個歸屬(工單 xor 設備);非 ISSUE 不受限。
    __table_args__ = (
        CheckConstraint(
            "kind <> 'ISSUE' OR num_nonnulls(work_order_no, charge_target_asset_id) = 1",
            name="ck_stock_transaction_issue_charge",
        ),
    )

    txn_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    item_code: Mapped[str] = mapped_column(
        ForeignKey("inventory_item.item_code"), nullable=False, index=True
    )
    work_order_no: Mapped[int | None] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=True, index=True
    )
    # 直領(非工單)歸屬的設備 EID(ADR-024);工單領料留 NULL(asset 經工單解析)。index 名
    # ix_stock_transaction_charge_target_asset_id 與 migration 0015 對齊。
    charge_target_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset.asset_id"), nullable=True, index=True
    )
    qty_delta: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False)  # ISSUE 為負
    kind: Mapped[str] = mapped_column(ForeignKey("stock_txn_kind.code"), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(128), unique=True, nullable=True
    )  # 同 key 重送不重複扣帳(ADR-006)
