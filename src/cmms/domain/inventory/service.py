"""InventoryService — Inventory 切片的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:get / list(多種過濾)+ 關聯查詢(適用子類型 / 替代品 / 套件)。
寫入:upsert + link(載入器用,idempotent)。庫存異動(receive/issue/adjust)等
governed write 延後(本切片讀取為主)。所有寫入經 `DomainService.write()`。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.asset.models import Asset
from cmms.domain.base import DomainService
from cmms.domain.contacts.models import Organization
from cmms.domain.identity.service import (
    AuthorizationError,
    assert_active_admin,
    is_operator,
)
from cmms.domain.inventory.models import (
    AssetSubtype,
    InventoryItem,
    InventoryItemAlternative,
    InventoryItemAssetSubtype,
    InventoryItemKit,
    StockTransaction,
    StockTxnKind,
    StorageBin,
)
from cmms.domain.inventory.transform import InventoryItemImport


class InventoryError(Exception):
    """庫存寫入錯誤(找不到設備/品項、歸屬不合法等)。"""


_UNSET = object()  # update_item 的 supplier_org_id 哨兵:「未提供」≠「清除(None)」

# storage_bin code 格式:首字母數字 + 其後字母數字/底線/連字號,總長 1–20(如 "02A"/"CMA"/"Drawer")
_STORAGE_BIN_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$")

# 歷史回填領料的 source_actor(= work_order.service.BACKFILL_ACTOR.value = "human:data-migration")。
# 回填帳以 adjust_on_hand=False 記帳、**從未扣 on_hand**;取消/改量會 RETURN 反灌 → 灌爆庫存。
# 硬編字串(而非 import,避免 inventory→work_order 循環引入);單一真相在 work_order.service。
_BACKFILL_SOURCE_ACTOR = "human:data-migration"


def _assert_not_backfill_issue(txn: StockTransaction) -> None:
    """回填領料帳(從未扣 on_hand)不可取消/改量,否則 RETURN 反灌會憑空灌爆庫存。"""
    if txn.source_actor == _BACKFILL_SOURCE_ACTOR:
        raise InventoryError(
            f"direct issue txn {txn.txn_id} is a historical backfill "
            "(never adjusted on_hand; cannot cancel/amend)"
        )


class InventoryService(DomainService):
    async def _assert_not_operator(self, actor: Actor, op: str) -> None:
        """operator(iPad 產線共用帳號)不得領料(直領 / 改量 / 取消);領料非其白名單職責。

        RBAC 在 domain 強制(route 藏表單不算授權);agent/scheduler/on-box 路徑非 operator,
        不受影響。`op` 帶入操作名供測試斷言與稽核。"""
        if await is_operator(self.session, actor):
            raise AuthorizationError(f"operator role cannot perform {op}")

    # ---- 讀取(ADR-004)----

    async def get_item(self, item_code: str) -> InventoryItem | None:
        return await self.session.get(InventoryItem, item_code)

    async def list_stock_txn_kinds(self) -> list[StockTxnKind]:
        """庫存異動類別受控詞彙(唯讀,ADR-004)。admin 詞彙頁純顯示。"""
        stmt = select(StockTxnKind).order_by(StockTxnKind.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_storage_bins(self, include_inactive: bool = False) -> list[StorageBin]:
        """備品儲位受控詞彙(唯讀,ADR-004)。編輯表單下拉 = active only;admin 詞彙頁全列。"""
        stmt = select(StorageBin)
        if not include_inactive:
            stmt = stmt.where(StorageBin.is_active.is_(True))
        stmt = stmt.order_by(StorageBin.code)
        return list((await self.session.scalars(stmt)).all())

    async def list_items(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        item_category: str | None = None,
        supplier: str | None = None,
        is_stocked: bool | None = None,
        is_obsolete: bool | None = None,
        below_reorder: bool = False,
        asset_subtype: str | None = None,
        search: str | None = None,
    ) -> list[InventoryItem]:
        stmt = select(InventoryItem)
        if search:
            like = f"%{search.strip()}%"
            stmt = stmt.where(
                or_(
                    InventoryItem.item_code.ilike(like),
                    InventoryItem.name.ilike(like),
                    InventoryItem.description.ilike(like),
                    InventoryItem.vendor_part_no.ilike(like),
                )
            )
        if item_category is not None:
            stmt = stmt.where(InventoryItem.item_category == item_category)
        if supplier is not None:
            stmt = stmt.where(InventoryItem.supplier == supplier)
        if is_stocked is not None:
            stmt = stmt.where(InventoryItem.is_stocked == is_stocked)
        if is_obsolete is not None:
            stmt = stmt.where(InventoryItem.is_obsolete == is_obsolete)
        if below_reorder:
            # 應補貨:在庫 < 再訂購點(兩者皆有值時)
            stmt = stmt.where(
                InventoryItem.quantity_on_hand.is_not(None),
                InventoryItem.reorder_point.is_not(None),
                InventoryItem.quantity_on_hand < InventoryItem.reorder_point,
            )
        if asset_subtype is not None:
            stmt = stmt.join(
                InventoryItemAssetSubtype,
                InventoryItemAssetSubtype.item_code == InventoryItem.item_code,
            ).where(InventoryItemAssetSubtype.asset_subtype == asset_subtype)
        stmt = stmt.order_by(InventoryItem.item_code).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def get_applicable_subtypes(self, item_code: str) -> list[str]:
        stmt = (
            select(InventoryItemAssetSubtype.asset_subtype)
            .where(InventoryItemAssetSubtype.item_code == item_code)
            .order_by(InventoryItemAssetSubtype.asset_subtype)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_alternatives(self, item_code: str) -> list[str]:
        stmt = (
            select(InventoryItemAlternative.alt_item_code)
            .where(InventoryItemAlternative.item_code == item_code)
            .order_by(InventoryItemAlternative.alt_item_code)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_kit_children(self, item_code: str) -> list[str]:
        stmt = (
            select(InventoryItemKit.child_item_code)
            .where(InventoryItemKit.parent_item_code == item_code)
            .order_by(InventoryItemKit.child_item_code)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_parent_kits(self, item_code: str) -> list[str]:
        """反查:哪些套件(parent kit)含本品(#7e)。kit 邊是有向的(parent→child),
        故此為 `get_kit_children` 的反向面。"""
        stmt = (
            select(InventoryItemKit.parent_item_code)
            .where(InventoryItemKit.child_item_code == item_code)
            .order_by(InventoryItemKit.parent_item_code)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_all_asset_subtypes(self) -> list[AssetSubtype]:
        """canonical 設備子類型 lookup 全表(A3;#7d 適用機種複選編輯的選項來源,唯讀)。"""
        stmt = select(AssetSubtype).order_by(AssetSubtype.code)
        return list((await self.session.scalars(stmt)).all())

    # ---- storage_bin 受控詞彙 governed 編輯(admin 面;增 + 啟停,不刪)----

    async def add_storage_bin(self, code: str, *, actor: Actor) -> StorageBin:
        """新增一個儲位代號(admin-only;/admin/vocab 與備品編輯內 quick-add 共用)。

        `code` strip 後驗格式(^[A-Za-z0-9][A-Za-z0-9_-]{0,19}$)、**大小寫不敏感查重**
        (含已停用者;撞已停用者提示到 /admin/vocab 重新啟用,不靜默復活)。admin 限定在
        domain 強制(guardrail #RBAC 縱深;比照 add_hold_reason)。governed(單一寫入路徑)。
        """
        code = (code or "").strip()
        if not _STORAGE_BIN_CODE.match(code):
            raise InventoryError(
                "storage bin code must be alphanumeric (with -/_), 1–20 chars (e.g. 02A)"
            )
        await assert_active_admin(self.session, actor)
        existing = await self.session.scalar(
            select(StorageBin).where(func.upper(StorageBin.code) == code.upper())
        )
        if existing is not None:
            if existing.is_active:
                raise InventoryError(f"storage bin {existing.code} already exists")
            raise InventoryError(
                f"storage bin {existing.code} exists but is deactivated "
                "— re-enable it in /admin/vocab"
            )
        async with self.write(actor):
            self.session.add(StorageBin(code=code, is_active=True))
        return await self.session.get(StorageBin, code)

    async def set_storage_bin_active(
        self, code: str, *, is_active: bool, actor: Actor
    ) -> StorageBin:
        """啟用 / 停用一個儲位(admin-only;governed toggle)。停用 = 不再供選,既有品項不受影響、
        不刪除。不存在 → error。"""
        code = (code or "").strip()
        await assert_active_admin(self.session, actor)
        row = await self.session.get(StorageBin, code)
        if row is None:
            raise InventoryError(f"storage bin {code} not found")
        async with self.write(actor):
            row.is_active = is_active
        return row

    async def _validate_bin_location(
        self, value: str | None, *, current: str | None = None
    ) -> str | None:
        """儲位寫入路徑驗證:空/None → None;等於品項現值 → 原樣放行(legacy 髒值不擋無關編輯);
        否則大小寫不敏感比對 **active** storage_bin → 回 canonical code(正規化大小寫);查無 → 拒。

        `current` = 品項現值(update 傳 item.bin_location;create 傳 None)。現庫有 CSV 位移
        垃圾值與裸 "08" 等 legacy 髒值,不得因此擋掉改別欄的無關編輯 —— 故現值原樣放行。
        """
        v = (value or "").strip()
        if not v:
            return None
        cur = (current or "").strip()
        if cur and v == cur:
            return value  # 現值原樣放行(可能是 legacy 非成員髒值)
        row = await self.session.scalar(
            select(StorageBin).where(
                func.upper(StorageBin.code) == v.upper(), StorageBin.is_active.is_(True)
            )
        )
        if row is None:
            raise InventoryError(f"bin_location {v!r} is not a registered storage bin")
        return row.code

    # ---- 寫入(經 self.write() 交易)----

    async def upsert_lookup(self, model: type, code: str, label: str) -> None:
        stmt = (
            pg_insert(model)
            .values(code=code, label=label)
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await self.session.execute(stmt)

    async def upsert_item(self, data: InventoryItemImport, actor: Actor) -> None:
        """載入器用:依 item_code upsert。re-run 不重複(idempotent)。currency 固定 USD(I2)。"""
        values = {
            "item_code": data.item_code,
            "item_category": data.item_category,
            "name": data.name,
            "description": data.description,
            "vendor_part_no": data.vendor_part_no,
            "quantity_on_hand": data.quantity_on_hand,
            "reorder_point": data.reorder_point,
            "lead_time_weeks": data.lead_time_weeks,
            "unit_cost": data.unit_cost,
            "currency": "USD",
            "bin_location": data.bin_location,
            "supplier": data.supplier,
            "weblink": data.weblink,
            "photo_ref": data.photo_ref,
            "comment": data.comment,
            "is_stocked": data.is_stocked,
            "is_obsolete": data.is_obsolete,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("item_code", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(InventoryItem)
            .values(**values)
            .on_conflict_do_update(index_elements=["item_code"], set_=update_cols)
        )
        await self.session.execute(stmt)

    async def link_asset_subtype(self, item_code: str, asset_subtype: str, actor: Actor) -> None:
        await self._link(
            InventoryItemAssetSubtype,
            {"item_code": item_code, "asset_subtype": asset_subtype},
            ["item_code", "asset_subtype"],
            actor,
        )

    async def link_alternative(self, item_code: str, alt_item_code: str, actor: Actor) -> None:
        await self._link(
            InventoryItemAlternative,
            {"item_code": item_code, "alt_item_code": alt_item_code},
            ["item_code", "alt_item_code"],
            actor,
        )

    async def link_kit(self, parent_item_code: str, child_item_code: str, actor: Actor) -> None:
        await self._link(
            InventoryItemKit,
            {"parent_item_code": parent_item_code, "child_item_code": child_item_code},
            ["parent_item_code", "child_item_code"],
            actor,
        )

    async def _link(self, model: type, keys: dict, pk: list[str], actor: Actor) -> None:
        """idempotent 建立 junction 一列(稽核 source_actor)。"""
        stmt = (
            pg_insert(model)
            .values(**keys, source_actor=actor.value, created_by=actor.value)
            .on_conflict_do_nothing(index_elements=pk)
        )
        await self.session.execute(stmt)

    async def post_stock_transaction(
        self,
        *,
        item_code: str,
        qty_delta: Decimal,
        kind: str,
        actor: Actor,
        work_order_no: int | None = None,
        charge_target_asset_id: str | None = None,
        reason: str | None = None,
        occurred_at: datetime | None = None,
        idempotency_key: str | None = None,
        adjust_on_hand: bool = True,
    ) -> bool:
        """記一筆庫存異動帳並(預設)連動調整 on_hand(ADR-005:不裸 UPDATE 庫存量)。

        於呼叫端的 `write()` 交易內執行(不自行 commit)。回傳 True=新帳已記;
        False=`idempotency_key` 已存在 → 跳過(不重複扣帳,ADR-006)。

        `adjust_on_hand=False`:只記帳、**不**動 on_hand。供歷史領料回填用(eMaint onhand
        snapshot 已反映歷史扣減,再扣會雙重扣帳)。帳仍留痕供稽核;此情境下帳的累加和 ≠ on_hand
        (snapshot 起算),屬可接受的反向語意——ADR-005 精神是「on_hand 不可**無帳**裸 UPDATE」,
        此處為「**有帳、無 on_hand 變動**」。

        `charge_target_asset_id`(ADR-024):領料歸屬設備(非工單直領)。ISSUE 的歸屬不變量
        (工單 xor 設備)由 DB CHECK `ck_stock_transaction_issue_charge` 守門;呼叫端負責恰傳其一。
        """
        if idempotency_key is not None:
            existing = await self.session.scalar(
                select(StockTransaction.txn_id).where(
                    StockTransaction.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                return False
        self.session.add(
            StockTransaction(
                item_code=item_code,
                work_order_no=work_order_no,
                charge_target_asset_id=charge_target_asset_id,
                qty_delta=qty_delta,
                kind=kind,
                reason=reason,
                occurred_at=occurred_at or datetime.now(UTC),
                idempotency_key=idempotency_key,
                source_actor=actor.value,
                created_by=actor.value,
            )
        )
        if adjust_on_hand:
            await self.session.execute(
                update(InventoryItem)
                .where(InventoryItem.item_code == item_code)
                .values(
                    quantity_on_hand=func.coalesce(InventoryItem.quantity_on_hand, 0) + qty_delta,
                    updated_by=actor.value,
                    source_actor=actor.value,
                )
            )
        return True

    async def issue_to_asset(
        self,
        *,
        asset_id: str,
        item_code: str,
        quantity: Decimal | str | int,
        actor: Actor,
        reason: str | None = None,
        idempotency_key: str | None = None,
        at: datetime | None = None,
    ) -> bool:
        """非工單直領(ADR-024):領料歸屬設備、連動扣 on_hand;**不**建 work_order_part(無工單)。

        歸屬 = `charge_target_asset_id`(工單領料則走 `WorkOrderService.issue_part_to_work_order`);
        DB CHECK 保證 ISSUE 恰有一個歸屬。走單一寫入路徑 + 全稽核 + 冪等(ADR-001/005/006)。
        回 True=已領;False=`idempotency_key` 命中 → 跳過(不重複扣帳)。
        未知 EID / 未登記 item → raise `InventoryError`。
        """
        await self._assert_not_operator(actor, "issue_to_asset")
        qty = self._positive_qty(quantity)
        async with self.write(actor):
            if await self.session.get(Asset, asset_id) is None:
                raise InventoryError(f"asset {asset_id} not found")
            if await self.session.get(InventoryItem, item_code) is None:
                raise InventoryError(f"inventory_item {item_code} not found")
            posted = await self.post_stock_transaction(
                item_code=item_code,
                qty_delta=-qty,
                kind="ISSUE",
                actor=actor,
                charge_target_asset_id=asset_id,
                reason=reason,
                occurred_at=at or datetime.now(UTC),
                idempotency_key=idempotency_key,
            )
        return posted

    @staticmethod
    def _positive_qty(value: Decimal | str | int) -> Decimal:
        """數量輸入守門(review f14cf8d:負數會反向加庫存、0 只留垃圾帳)。"""
        try:
            qty = Decimal(str(value))
        except ArithmeticError as e:
            raise InventoryError(f"invalid quantity: {value!r}") from e
        if qty <= 0:
            raise InventoryError(f"quantity must be positive: {value!r}")
        return qty

    @staticmethod
    def _non_negative(field: str, value: Decimal | int | None) -> None:
        """主檔數值欄位不得為負(review f14cf8d:負 reorder_quantity 會流進 RFQ 報量、
        負 reorder_point 讓品項永遠不出現在低庫存清單)。"""
        if value is not None and value < 0:
            raise InventoryError(f"{field} cannot be negative: {value}")

    async def create_item(
        self,
        item_code: str,
        *,
        actor: Actor,
        name: str,
        description: str | None = None,
        vendor_part_no: str | None = None,
        bin_location: str | None = None,
        reorder_point: Decimal | None = None,
        reorder_quantity: Decimal | None = None,
        lead_time_weeks: int | None = None,
        unit_cost: Decimal | None = None,
        supplier: str | None = None,
        supplier_org_id: str | None = None,
        weblink: str | None = None,
        comment: str | None = None,
        is_stocked: bool = True,
        is_obsolete: bool = False,
    ) -> InventoryItem:
        """建立新備品主檔(admin-only,治理寫入;比照 `AssetService.create_asset`)。

        用途 = admin 登記 eMaint 尚未有的新料號。`item_code` = 主鍵,自由格式(非 EID 樣板),
        strip + upper 後儲存(全 route 以 `item_code.upper()` 查詢,大小寫需一致);建立後不可變。
        已存在 → InventoryError(create 非 upsert,絕不靜默覆蓋;idempotent 載入走 upsert)。

        ★ 不在此設 `quantity_on_hand`(庫存量不可裸改,ADR-005;留 NULL,期初盤點走
        `adjust_on_hand` 記 ADJUST 帳)、不設 `currency`(server_default 'USD')、
        `item_category`、`photo_ref`(媒體走 attachment 服務)。

        `name` 必填非空;數值欄不得為負;`supplier_org_id` 給值須為既存 org;
        `supplier` 給值須為既存 organization 名或既存 supplier 值(#7b,不硬鑄新供應商)。
        其餘文字欄空 → None。走 self.write() 單一寫入路徑,三稽核欄 = actor.value。
        """
        await assert_active_admin(self.session, actor)

        code = (item_code or "").strip().upper()
        if not code:
            raise InventoryError("item_code cannot be empty")
        if len(code) > 50:
            raise InventoryError(f"item_code too long: {code!r}")
        # item_code 是 URL path segment + 主鍵:擋空白與會破壞路徑/查詢的字元。
        if any(c.isspace() for c in code) or any(c in code for c in "/?#%\\"):
            raise InventoryError(f"item_code has illegal characters: {item_code!r}")
        if await self.session.get(InventoryItem, code) is not None:
            raise InventoryError(f"inventory_item {code} already exists")

        clean_name = (name or "").strip()
        if not clean_name:
            raise InventoryError("item name cannot be empty")
        self._non_negative("reorder_point", reorder_point)
        self._non_negative("reorder_quantity", reorder_quantity)
        self._non_negative("lead_time_weeks", lead_time_weeks)
        self._non_negative("unit_cost", unit_cost)

        new_org = (supplier_org_id or "").strip() or None
        if new_org is not None and await self.session.get(Organization, new_org) is None:
            raise InventoryError(f"organization {new_org} not found")
        clean_supplier = (supplier or "").strip() or None
        if clean_supplier is not None and not await self._supplier_value_known(clean_supplier):
            raise InventoryError(
                f"supplier {clean_supplier!r} is not a known organization or supplier"
            )
        # 儲位受控詞彙:給值須為 active storage_bin(正規化大小寫);空 → None。create 無現值。
        clean_bin = await self._validate_bin_location(bin_location, current=None)

        async with self.write(actor):
            item = InventoryItem(
                item_code=code,
                name=clean_name,
                description=(description or "").strip() or None,
                vendor_part_no=(vendor_part_no or "").strip() or None,
                bin_location=clean_bin,
                reorder_point=reorder_point,
                reorder_quantity=reorder_quantity,
                lead_time_weeks=lead_time_weeks,
                unit_cost=unit_cost,
                supplier=clean_supplier,
                supplier_org_id=new_org,
                weblink=(weblink or "").strip() or None,
                comment=(comment or "").strip() or None,
                is_stocked=is_stocked,
                is_obsolete=is_obsolete,
                created_by=actor.value,
                updated_by=actor.value,
                source_actor=actor.value,
            )
            self.session.add(item)
        return item

    async def update_item(
        self,
        item_code: str,
        *,
        actor: Actor,
        name: str | None,
        description: str | None,
        vendor_part_no: str | None,
        bin_location: str | None,
        reorder_point: Decimal | None,
        reorder_quantity: Decimal | None,
        lead_time_weeks: int | None,
        unit_cost: Decimal | None,
        supplier: str | None,
        weblink: str | None,
        comment: str | None,
        is_stocked: bool,
        is_obsolete: bool,
        supplier_org_id: str | None | object = _UNSET,
    ) -> InventoryItem:
        """品項主檔編輯(governed;web admin 面)。整組覆寫表單欄位(空 → None)。

        ★ 不含 `quantity_on_hand`(庫存量不可裸改,走 `adjust_on_hand` 記 ADJUST 帳,ADR-005)。
        `supplier_org_id`(RFQ 收件 org)可一併帶入**同一交易**處理(review f14cf8d:先前
        主檔與連結分兩段交易,後段失敗會顯示「儲存失敗」但主檔其實已寫入):
        - 未提供(預設哨兵)→ 不動既有連結
        - None / 空 → **清除連結**(先前無任何 unlink 路徑,錯的 RFQ 收件者永遠拿不掉)
        - 有值 → 驗 org 存在後連結
        """
        async with self.write(actor):
            item = await self._update_item_impl(
                item_code,
                actor=actor,
                name=name,
                description=description,
                vendor_part_no=vendor_part_no,
                bin_location=bin_location,
                reorder_point=reorder_point,
                reorder_quantity=reorder_quantity,
                lead_time_weeks=lead_time_weeks,
                unit_cost=unit_cost,
                supplier=supplier,
                weblink=weblink,
                comment=comment,
                is_stocked=is_stocked,
                is_obsolete=is_obsolete,
                supplier_org_id=supplier_org_id,
            )
        return item

    async def _update_item_impl(
        self,
        item_code: str,
        *,
        actor: Actor,
        name: str | None,
        description: str | None,
        vendor_part_no: str | None,
        bin_location: str | None,
        reorder_point: Decimal | None,
        reorder_quantity: Decimal | None,
        lead_time_weeks: int | None,
        unit_cost: Decimal | None,
        supplier: str | None,
        weblink: str | None,
        comment: str | None,
        is_stocked: bool,
        is_obsolete: bool,
        supplier_org_id: str | None | object = _UNSET,
    ) -> InventoryItem:
        """品項編輯核心(無自有交易;由 `update_item` 或提案 confirm 各自 write() 內呼叫)。"""
        item = await self.session.get(InventoryItem, item_code)
        if item is None:
            raise InventoryError(f"inventory_item {item_code} not found")
        self._non_negative("reorder_point", reorder_point)
        self._non_negative("reorder_quantity", reorder_quantity)
        self._non_negative("lead_time_weeks", lead_time_weeks)
        self._non_negative("unit_cost", unit_cost)
        new_org: str | None | object = supplier_org_id
        if new_org is not _UNSET and new_org is not None:
            new_org = str(new_org).strip() or None
            if new_org is not None and await self.session.get(Organization, new_org) is None:
                raise InventoryError(f"organization {new_org} not found")
        # 供應商欄限現有值(#7b):給定的 supplier 文字必須對得上既存 organization.name,或已是
        # 既存的 supplier 文字值(涵蓋 legacy 未連 org 的名稱、以及品項本身現值)。查無 → 拒。
        if supplier is not None and not await self._supplier_value_known(supplier):
            raise InventoryError(
                f"supplier {supplier!r} is not a known organization or supplier"
            )
        # 儲位受控詞彙:給值須為 active storage_bin;等於現值 → 原樣放行(legacy 髒值不擋無關編輯)。
        # 此核心同被 admin 直改與提案 confirm 走 → 提案 confirm 自動受驗。
        clean_bin = await self._validate_bin_location(bin_location, current=item.bin_location)
        item.name = name
        item.description = description
        item.vendor_part_no = vendor_part_no
        item.bin_location = clean_bin
        item.reorder_point = reorder_point
        item.reorder_quantity = reorder_quantity
        item.lead_time_weeks = lead_time_weeks
        item.unit_cost = unit_cost
        item.supplier = supplier
        item.weblink = weblink
        item.comment = comment
        item.is_stocked = is_stocked
        item.is_obsolete = is_obsolete
        if new_org is not _UNSET:
            item.supplier_org_id = new_org
        item.updated_by = actor.value
        item.source_actor = actor.value
        await self.session.flush()
        return item

    async def adjust_on_hand(
        self,
        item_code: str,
        *,
        new_quantity: Decimal | str,
        reason: str,
        actor: Actor,
        idempotency_key: str | None = None,
    ) -> bool:
        """盤點調整在庫量(governed):記一筆 ADJUST 帳並連動 on_hand(不裸 UPDATE,ADR-005)。

        以「新數量」表意(差額由此算),必附 reason、不得為負(review f14cf8d)。
        回 True=已調;False=數量未變/冪等命中。
        """
        if not reason or not reason.strip():
            raise InventoryError("on-hand adjustment requires a reason")
        try:
            qty = Decimal(str(new_quantity))
        except ArithmeticError as e:
            raise InventoryError(f"invalid quantity: {new_quantity!r}") from e
        if qty < 0:
            raise InventoryError(f"on-hand cannot be negative: {new_quantity!r}")
        async with self.write(actor):
            item = await self.session.get(InventoryItem, item_code)
            if item is None:
                raise InventoryError(f"inventory_item {item_code} not found")
            delta = qty - (item.quantity_on_hand or Decimal(0))
            if delta == 0:
                return False
            posted = await self.post_stock_transaction(
                item_code=item_code,
                qty_delta=delta,
                kind="ADJUST",
                actor=actor,
                reason=reason.strip(),
                idempotency_key=idempotency_key,
            )
        return posted

    async def link_supplier_org(self, item_code: str, org_id: str, actor: Actor) -> None:
        """連結品項的供應商 organization(ADR-026;governed)。品項/org 不存在 → raise。"""
        item = await self.session.get(InventoryItem, item_code)
        if item is None:
            raise InventoryError(f"inventory_item {item_code} not found")
        if await self.session.get(Organization, org_id) is None:
            raise InventoryError(f"organization {org_id} not found")
        async with self.write(actor):
            item.supplier_org_id = org_id
            item.updated_by = actor.value
            item.source_actor = actor.value

    async def _supplier_value_known(self, supplier: str) -> bool:
        """supplier 文字是否為既存值(#7b):對得上 organization.name(不分大小寫)或既存
        inventory_item.supplier(涵蓋 legacy 未連 org 名稱 + 品項自身現值)。空 → 視為 OK(清除)。"""
        low = supplier.strip().lower()
        if not low:
            return True
        if await self.session.scalar(
            select(Organization.org_id).where(func.lower(Organization.name) == low).limit(1)
        ):
            return True
        return bool(
            await self.session.scalar(
                select(InventoryItem.item_code)
                .where(func.lower(InventoryItem.supplier) == low)
                .limit(1)
            )
        )

    async def set_applicable_subtypes(
        self, item_code: str, subtypes: list[str], actor: Actor
    ) -> list[str]:
        """設品項適用機種(#7d;admin-only)。整組覆寫 `inventory_item_asset_subtype` junction:
        不在新集合的舊列刪除、新的加入(idempotent)。每個 subtype 須為既存 canonical lookup code。
        回最終生效的 subtype 清單(去重、排序)。"""
        await assert_active_admin(self.session, actor)
        if await self.session.get(InventoryItem, item_code) is None:
            raise InventoryError(f"inventory_item {item_code} not found")
        wanted: list[str] = []
        seen: set[str] = set()
        for raw in subtypes:
            s = (raw or "").strip()
            if not s or s in seen:
                continue
            if await self.session.get(AssetSubtype, s) is None:
                raise InventoryError(f"asset_subtype {s} not found")
            seen.add(s)
            wanted.append(s)
        async with self.write(actor):
            del_stmt = delete(InventoryItemAssetSubtype).where(
                InventoryItemAssetSubtype.item_code == item_code
            )
            if wanted:
                del_stmt = del_stmt.where(
                    InventoryItemAssetSubtype.asset_subtype.not_in(wanted)
                )
            await self.session.execute(del_stmt)
            existing = set(
                (
                    await self.session.scalars(
                        select(InventoryItemAssetSubtype.asset_subtype).where(
                            InventoryItemAssetSubtype.item_code == item_code
                        )
                    )
                ).all()
            )
            for s in wanted:
                if s not in existing:
                    self.session.add(
                        InventoryItemAssetSubtype(
                            item_code=item_code,
                            asset_subtype=s,
                            created_by=actor.value,
                            source_actor=actor.value,
                        )
                    )
        return sorted(wanted)

    async def set_reorder_quantity(self, item_code: str, quantity, actor: Actor) -> bool:
        """設再訂購量(ADR-026;load-orderqty 用)。品項不存在 → False。"""
        item = await self.session.get(InventoryItem, item_code)
        if item is None:
            return False
        async with self.write(actor):
            item.reorder_quantity = Decimal(str(quantity))
            item.updated_by = actor.value
            item.source_actor = actor.value
        return True

    async def autolink_suppliers(self, actor: Actor) -> tuple[int, int]:
        """把有 supplier 文字但未連 org 的品項,依 organization.name 精確(不分大小寫)配對連結。

        回 (linked, unmatched)。對不上者留 NULL(RFQ-ineligible,待人工連;ADR-026 決策 b)。
        """
        items = list(
            (
                await self.session.scalars(
                    select(InventoryItem).where(
                        InventoryItem.supplier.is_not(None),
                        InventoryItem.supplier_org_id.is_(None),
                    )
                )
            ).all()
        )
        linked = unmatched = 0
        async with self.write(actor):
            for it in items:
                org = await self.session.scalar(
                    select(Organization).where(
                        func.lower(Organization.name) == (it.supplier or "").strip().lower()
                    )
                )
                if org is None:
                    unmatched += 1
                    continue
                it.supplier_org_id = org.org_id
                it.updated_by = actor.value
                it.source_actor = actor.value
                linked += 1
        return (linked, unmatched)

    async def list_asset_part_usage(
        self, asset_ids: list[str], *, limit: int = 50
    ) -> list[StockTransaction]:
        """單機零件消耗(ADR-024 讀取面):給定機台(含後代模組)的領料史,新到舊。

        聯集兩種歸屬:直領(`charge_target_asset_id` ∈ ids)∪ 工單領料(`work_order_no` 屬
        這些機台的工單)。含 ISSUE(領出,qty_delta<0)與 RETURN(回庫/取消,qty_delta>0)
        兩類帳(#9 領料改量/取消後補償帳誠實呈現;渲染端依 qty_delta 正負區分)。純讀取,開放。
        """
        if not asset_ids:
            return []
        from cmms.domain.work_order.models import WorkOrder  # 延遲匯入避免循環

        wo_sub = select(WorkOrder.work_order_no).where(WorkOrder.asset_id.in_(asset_ids))
        stmt = (
            select(StockTransaction)
            .where(
                StockTransaction.kind.in_(("ISSUE", "RETURN")),
                or_(
                    StockTransaction.charge_target_asset_id.in_(asset_ids),
                    StockTransaction.work_order_no.in_(wo_sub),
                ),
            )
            .order_by(StockTransaction.occurred_at.desc(), StockTransaction.txn_id.desc())
            .limit(limit)
        )
        return list((await self.session.scalars(stmt)).all())

    @staticmethod
    def _asset_issue_cancel_key(txn_id: int) -> str:
        """直領取消的**決定性**冪等鍵:每筆 ISSUE 帳一生只允許一筆取消 RETURN。

        直領無 work_order_part 摘要列可標記狀態(ledger append-only,護欄 #4 不改舊帳);
        以決定性 key 讓「重複取消/雙擊/重送」永遠冪等,並可反查「此帳已取消」。
        """
        return f"assetissuecancel:v1:{txn_id}"

    async def cancelled_asset_issue_ids(self, txn_ids: list[int]) -> set[int]:
        """回傳給定直領 ISSUE 帳中「已被取消(或被改量 supersede)」者的 txn_id 集合。
        讀取面:UI 據以隱藏已取消列的改量/取消按鈕。"""
        if not txn_ids:
            return set()
        keys = {self._asset_issue_cancel_key(i): i for i in txn_ids}
        rows = await self.session.scalars(
            select(StockTransaction.idempotency_key).where(
                StockTransaction.idempotency_key.in_(list(keys))
            )
        )
        return {keys[k] for k in rows}

    async def _get_live_asset_issue(self, asset_id: str, txn_id: int) -> StockTransaction:
        """取一筆「該設備的、未被取消的」直領 ISSUE 帳(歸屬 + 取消狀態守門)。"""
        txn = await self.session.get(StockTransaction, txn_id)
        if txn is None or txn.kind != "ISSUE" or txn.charge_target_asset_id != asset_id:
            raise InventoryError(f"direct issue txn {txn_id} not found on asset {asset_id}")
        _assert_not_backfill_issue(txn)
        if await self.session.scalar(
            select(StockTransaction.txn_id).where(
                StockTransaction.idempotency_key == self._asset_issue_cancel_key(txn_id)
            )
        ):
            raise InventoryError(f"direct issue txn {txn_id} already cancelled/superseded")
        return txn

    async def update_asset_issue_quantity(
        self,
        *,
        asset_id: str,
        txn_id: int,
        new_quantity: Decimal | str | int,
        actor: Actor,
        idempotency_key: str | None = None,
    ) -> bool:
        """改設備直領數量(ADR-024 + Jordan 2026-07-05 #9)。

        直領無摘要列、ledger append-only,故「改量」= **取消原帳 + 以新量重開**(同一交易):
        ① RETURN 原量回庫(決定性取消鍵 → 原帳從此標記為 superseded,防之後重複取消/改量疊帳)
        ② ISSUE 新量扣庫(`idempotency_key` = 呼叫端 nonce 鍵,重送冪等)。淨效果 = 差額連動;
        兩筆補償帳誠實留痕。增量超過現有庫存 → 拒絕。回 True=已改;False=數量未變/冪等命中。
        """
        await self._assert_not_operator(actor, "update_asset_issue_quantity")
        new_qty = self._positive_qty(new_quantity)
        async with self.write(actor):
            # 重送冪等:同 nonce 鍵的 reissue 已存在(前次已成功)→ 直接冪等命中,
            # 不再走「已 superseded」的拒絕路徑(否則 retry 會被誤判為錯誤)。
            if idempotency_key is not None and await self.session.scalar(
                select(StockTransaction.txn_id).where(
                    StockTransaction.idempotency_key == idempotency_key
                )
            ):
                return False
            txn = await self._get_live_asset_issue(asset_id, txn_id)
            issued = -txn.qty_delta  # 原領出量(正)
            if new_qty == issued:
                return False
            if new_qty > issued:
                # 淨增量 = new - issued;先回 issued 再扣 new,需 on_hand ≥ 淨增量
                item = await self.session.get(InventoryItem, txn.item_code)
                on_hand = (item.quantity_on_hand or Decimal(0)) if item is not None else Decimal(0)
                if on_hand < new_qty - issued:
                    raise InventoryError(
                        f"insufficient stock for {txn.item_code}: "
                        f"have {on_hand}, need {new_qty - issued} more"
                    )
            when = datetime.now(UTC)
            await self.post_stock_transaction(
                item_code=txn.item_code, qty_delta=issued, kind="RETURN", actor=actor,
                charge_target_asset_id=asset_id,
                reason=f"amend direct issue (supersede txn {txn_id})",
                occurred_at=when, idempotency_key=self._asset_issue_cancel_key(txn_id),
            )
            posted = await self.post_stock_transaction(
                item_code=txn.item_code, qty_delta=-new_qty, kind="ISSUE", actor=actor,
                charge_target_asset_id=asset_id,
                reason=f"amend direct issue (reissue for txn {txn_id})",
                occurred_at=when, idempotency_key=idempotency_key,
            )
        return posted

    async def cancel_asset_issue(
        self,
        *,
        asset_id: str,
        txn_id: int,
        actor: Actor,
    ) -> bool:
        """取消設備直領(Jordan 2026-07-05 #9):RETURN 原領出量回庫(ledger append-only,不刪帳)。

        以 `txn_id` 定位(須為該設備的 ISSUE 直領帳)。冪等鍵 = **決定性取消鍵**(每帳一生一次;
        雙擊/重送/重複取消永遠安全)。原 ISSUE 與補償 RETURN 皆留帳,usage 讀取端一併呈現。
        回 True=已取消;False=先前已取消(冪等命中)。
        """
        await self._assert_not_operator(actor, "cancel_asset_issue")
        async with self.write(actor):
            txn = await self.session.get(StockTransaction, txn_id)
            if txn is None or txn.kind != "ISSUE" or txn.charge_target_asset_id != asset_id:
                raise InventoryError(f"direct issue txn {txn_id} not found on asset {asset_id}")
            _assert_not_backfill_issue(txn)
            posted = await self.post_stock_transaction(
                item_code=txn.item_code, qty_delta=-txn.qty_delta, kind="RETURN", actor=actor,
                charge_target_asset_id=asset_id, reason=f"cancel direct issue (txn {txn_id})",
                occurred_at=datetime.now(UTC),
                idempotency_key=self._asset_issue_cancel_key(txn_id),
            )
        return posted

    async def list_recent_transactions(
        self, *, limit: int = 50, actor_like: str | None = None
    ) -> list[StockTransaction]:
        """近期庫存異動(ISSUE/RECEIVE/ADJUST/RETURN;稽核 feed,讀取開放 ADR-004)。
        `actor_like` = source_actor 子字串過濾。依 occurred_at 新到舊。"""
        stmt = select(StockTransaction)
        if actor_like:
            stmt = stmt.where(StockTransaction.source_actor.ilike(f"%{actor_like}%"))
        stmt = stmt.order_by(
            StockTransaction.occurred_at.desc(), StockTransaction.txn_id.desc()
        ).limit(limit)
        return list((await self.session.scalars(stmt)).all())
