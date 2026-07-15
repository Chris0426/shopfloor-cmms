"""AssetService — Asset 切片的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:get_asset / list_assets / 身分解析(對外開放,ADR-004)。
寫入:upsert(載入器用)、register_external_id(ADR-015 身分綁定的 governed write)。
所有寫入經 `DomainService.write()` 交易並填稽核欄(source_actor 等,ADR-005)。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.asset.models import (
    Asset,
    AssetExternalId,
    AssetOwner,
    AssetRelationship,
    AssetRelationshipType,
    AssetType,
    Department,
    Line,
)
from cmms.domain.asset.transform import (
    CONTAINS_MODULE,
    SHARED_DEPENDENCY,
    AssetImport,
    line_sort_key,
)
from cmms.domain.base import DomainService, clean_person_name
from cmms.domain.identity.service import assert_active_admin


def _strip_or_none(v: str | None) -> str | None:
    """表單字串 → strip 後空字串轉 None(統一空值表徵)。"""
    v = (v or "").strip()
    return v or None


_EID_RE = re.compile(r"^EID-\d{5}$")  # 全 687 筆既有 EID 皆此形(A4:asset_id=EID=MES-EID)


def _clean_names(names: list[str] | None) -> list[str]:
    """負責人清單正規化:逐名 clean_person_name(strip 空 → 丟)、去重保序(供多負責人寫入)。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in names or []:
        n = clean_person_name(raw)
        if n is None or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


class AssetError(Exception):
    """asset 領域寫入治理錯誤(成環 / 多重容器等守門違反)。"""


class UnknownAssetError(AssetError):
    """Q6:組成邊端點 EID 不在 asset 主檔(不靜默建殘缺資產)。"""


class AssetService(DomainService):
    # ---- 讀取(ADR-004:讀取低風險,直接開放)----

    async def get_asset(self, asset_id: str) -> Asset | None:
        return await self.session.get(Asset, asset_id)

    async def list_assets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        department: str | None = None,
        asset_type: str | None = None,
        line: str | None = None,
        available: bool | None = None,
        search: str | None = None,
    ) -> list[Asset]:
        """列設備主檔(ADR-004)。`search` = EID / 描述 ilike(設備查詢自由文字,Slice 3)。"""
        stmt = select(Asset)
        if search and search.strip():
            like = f"%{search.strip()}%"
            stmt = stmt.where(or_(Asset.asset_id.ilike(like), Asset.description.ilike(like)))
        if department is not None:
            stmt = stmt.where(Asset.department == department)
        if asset_type is not None:
            stmt = stmt.where(Asset.asset_type == asset_type)
        if line is not None:
            stmt = stmt.where(Asset.line == line)
        if available is not None:
            stmt = stmt.where(Asset.available_for_service == available)
        stmt = stmt.order_by(Asset.asset_id).limit(limit).offset(offset)
        return list((await self.session.scalars(stmt)).all())

    async def descriptions_map(self, asset_ids: list[str]) -> dict[str, str]:
        """批次取資產敘述(EID → description),一查免 N+1(工單清單卡標機台名用)。"""
        ids = [a for a in dict.fromkeys(asset_ids) if a]  # 去重、去空
        if not ids:
            return {}
        rows = await self.session.execute(
            select(Asset.asset_id, Asset.description).where(Asset.asset_id.in_(ids))
        )
        return {aid: desc for aid, desc in rows}

    # ---- 設備負責人(多負責人,0031;asset_owner 交叉表)----

    async def get_owners(self, asset_id: str) -> list[str]:
        """一台設備的所有負責人(依 position 排序;讀取,開放 ADR-004)。無 → 空清單。"""
        rows = await self.session.execute(
            select(AssetOwner.person_name)
            .where(AssetOwner.asset_id == asset_id)
            .order_by(AssetOwner.position, AssetOwner.person_name)
        )
        return [r[0] for r in rows]

    async def owners_map(self, asset_ids: list[str]) -> dict[str, list[str]]:
        """批次取多台設備的負責人(asset_id → [names],依 position),一查免 N+1(清單/匯出用)。"""
        ids = [a for a in dict.fromkeys(asset_ids) if a]
        if not ids:
            return {}
        rows = await self.session.execute(
            select(AssetOwner.asset_id, AssetOwner.person_name)
            .where(AssetOwner.asset_id.in_(ids))
            .order_by(AssetOwner.asset_id, AssetOwner.position, AssetOwner.person_name)
        )
        out: dict[str, list[str]] = {}
        for aid, name in rows:
            out.setdefault(aid, []).append(name)
        return out

    async def _replace_owners(
        self, asset_id: str, owners: list[str], actor: Actor
    ) -> bool:
        """在呼叫端 write() 交易內,把一台設備的負責人整組替換為 `owners`(已正規化清單)。

        差異更新:刪除不在新清單的舊列、插入新列、更新既有列的 position;內容全同 → no-op
        回 False(不動稽核欄,冪等)。回是否有變更。**不自開交易 / 不驗 admin**(呼叫端負責)。
        """
        existing = {
            r.person_name: r
            for r in (
                await self.session.scalars(
                    select(AssetOwner).where(AssetOwner.asset_id == asset_id)
                )
            ).all()
        }
        desired = {name: pos for pos, name in enumerate(owners)}
        changed = False
        for name, row in list(existing.items()):
            if name not in desired:
                await self.session.delete(row)
                changed = True
        for name, pos in desired.items():
            row = existing.get(name)
            if row is None:
                self.session.add(
                    AssetOwner(
                        asset_id=asset_id,
                        person_name=name,
                        position=pos,
                        created_by=actor.value,
                        source_actor=actor.value,
                    )
                )
                changed = True
            elif row.position != pos:
                row.position = pos
                row.updated_by = actor.value
                row.source_actor = actor.value
                changed = True
        return changed

    async def set_owners(
        self, asset_id: str, owners: list[str], actor: Actor
    ) -> list[str]:
        """設定一台設備的負責人清單(admin-only,治理寫入;0031)。整組替換。

        逐名 `clean_person_name` 正規化、去空、去重保序;空清單 = 清除該設備所有負責人。
        全未變 → no-op(不動稽核欄)。設備不存在 → UnknownAssetError。回正規化後的負責人清單。
        用於設備編輯 / 建立 / 批次指定頁共用同一寫入邏輯(`_replace_owners`)。
        """
        await assert_active_admin(self.session, actor)
        if await self.session.get(Asset, asset_id) is None:
            raise UnknownAssetError(f"asset {asset_id} not in master")
        clean = _clean_names(owners)
        async with self.write(actor):
            await self._replace_owners(asset_id, clean, actor)
        return clean

    async def list_for_owner_admin(
        self, *, search: str | None = None, only_missing: bool = True, limit: int = 300
    ) -> list[Asset]:
        """批次指定負責人頁的資產清單(唯讀,ADR-004)。

        只列在冊(is_active)資產;`only_missing`(0031)→ 僅**無任何負責人**者(NOT EXISTS in
        asset_owner,聚焦待補的機台);`search` = EID / 描述 ilike。排序 description → asset_id
        (同型號機台相鄰,便於整批勾選)。回傳列另透明附掛 `owners`(list[str])供顯示,避免 N+1。
        `limit` 上限(呼叫端多取 1 判斷是否截斷)。
        """
        stmt = select(Asset).where(Asset.is_active.is_(True))
        if only_missing:
            stmt = stmt.where(
                ~select(AssetOwner.person_name)
                .where(AssetOwner.asset_id == Asset.asset_id)
                .exists()
            )
        if search and search.strip():
            like = f"%{search.strip()}%"
            stmt = stmt.where(or_(Asset.asset_id.ilike(like), Asset.description.ilike(like)))
        stmt = stmt.order_by(Asset.description, Asset.asset_id).limit(limit)
        assets = list((await self.session.scalars(stmt)).all())
        omap = await self.owners_map([a.asset_id for a in assets])
        for a in assets:
            a.owners = omap.get(a.asset_id, [])  # 顯示層透明附掛(非 mapped 欄)
        return assets

    async def pm_counts(self, asset_ids: list[str]) -> dict[str, int]:
        """批次取每台資產的 PM 排程數(asset_id → count),一查免 N+1(owner 頁資訊欄)。"""
        from cmms.domain.pm_schedule.models import PmSchedule  # 延遲匯入避免循環

        ids = [a for a in dict.fromkeys(asset_ids) if a]
        if not ids:
            return {}
        rows = await self.session.execute(
            select(PmSchedule.asset_id, func.count())
            .where(PmSchedule.asset_id.in_(ids))
            .group_by(PmSchedule.asset_id)
        )
        return {aid: n for aid, n in rows}

    async def set_owner_bulk(
        self, *, asset_ids: list[str], owners: list[str], actor: Actor
    ) -> int:
        """批次**替換**一組資產的負責人清單(admin-only,治理寫入;0031 多負責人)。

        `owners`(設備負責人清單)= 維修/保養工單 assignee 的事實來源。逐名 `clean_person_name`
        正規化、去空、去重保序;**空清單 → 清除**選定資產的所有負責人(某人離職 / 待重指派)。
        語意 = REPLACE(每台選定資產的負責人整組換成此清單),非追加。

        `asset_ids` 每筆 strip + upper 後去重(保序);空清單 → AssetError。全部一次載入(單一
        in_ 查詢);**任一 EID 不在主檔 → AssetError 列出缺項(all-or-nothing,不靜默部分成功)**。
        單一 self.write() 交易:與現況相同的資產跳過(冪等,不動稽核欄)。回實際變更的資產台數。
        """
        await assert_active_admin(self.session, actor)
        ids = [e for e in dict.fromkeys((i or "").strip().upper() for i in asset_ids) if e]
        if not ids:
            raise AssetError("no assets selected")
        clean = _clean_names(owners)
        rows = (await self.session.scalars(select(Asset).where(Asset.asset_id.in_(ids)))).all()
        by_id = {a.asset_id: a for a in rows}
        missing = [e for e in ids if e not in by_id]
        if missing:
            raise UnknownAssetError(f"assets not in master: {', '.join(missing)}")
        changed = 0
        async with self.write(actor):
            for eid in ids:
                if await self._replace_owners(eid, clean, actor):
                    changed += 1
        return changed

    async def resolve_by_external_id(self, namespace: str, external_id: str) -> Asset | None:
        """身分解析(ADR-015):用任一外部系統 id 反查同一台機器。"""
        row = await self.session.get(AssetExternalId, (namespace, external_id))
        if row is None:
            return None
        return await self.session.get(Asset, row.asset_id)

    async def list_external_ids(self, asset_id: str) -> list[AssetExternalId]:
        stmt = (
            select(AssetExternalId)
            .where(AssetExternalId.asset_id == asset_id)
            .order_by(AssetExternalId.namespace, AssetExternalId.external_id)
        )
        return list((await self.session.scalars(stmt)).all())

    # ---- lookup 受控詞彙(唯讀,ADR-004:admin 主檔編輯表單的下拉選項來源)----

    async def list_asset_types(self) -> list[AssetType]:
        return list((await self.session.scalars(select(AssetType).order_by(AssetType.code))).all())

    async def list_departments(self) -> list[Department]:
        return list(
            (await self.session.scalars(select(Department).order_by(Department.code))).all()
        )

    async def list_lines(self) -> list[Line]:
        # 自然排序(1K < 10K < EOL < Wet Loop):SQL 字串排序會把 10K 排在 1K 前,
        # 故取全部後於 Python 用 line_sort_key 排(見 transform.line_sort_key docstring)。
        lines = list((await self.session.scalars(select(Line))).all())
        return sorted(lines, key=lambda line: line_sort_key(line.code))

    # ---- 寫入(經 self.write() 交易;此處只下語句,交易邊界在呼叫端)----

    async def upsert_lookup(self, model: type, code: str, label: str) -> None:
        """idempotent 種子 lookup(asset_type / department / line / namespace)。"""
        stmt = (
            pg_insert(model)
            .values(code=code, label=label)
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await self.session.execute(stmt)

    async def upsert_asset(self, data: AssetImport, actor: Actor) -> None:
        """載入器用:依 asset_id upsert。re-run 不重複(idempotent migration)。"""
        values = {
            "asset_id": data.asset_id,
            "description": data.description,
            "asset_type": data.asset_type,
            "asset_subtype": data.asset_subtype,
            "department": data.department,
            "line": data.line,
            "site": "PLANT-1",
            "model_no": data.model_no,
            "serial_no": data.serial_no,
            "available_for_service": data.available_for_service,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("asset_id", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(Asset)
            .values(**values)
            .on_conflict_do_update(index_elements=["asset_id"], set_=update_cols)
        )
        await self.session.execute(stmt)

    async def set_asset_active(self, asset_id: str, active: bool, actor: Actor) -> Asset:
        """啟用/停用設備主檔 is_active(admin-only,治理寫入)。

        is_active = 在冊/退役旗標。**已退役(is_active=false)會擋 REACTIVE 報修
        (`_open_impl`)與 PM 工單生成(`_generate_pm_impl`,Jordan 2026-07-05 裁決)**;
        領料 / 歷史查詢不受影響。清單另以 `available` 純資訊性過濾。admin 限定在 domain
        強制。冪等:同值 → no-op(不動稽核欄)。
        """
        await assert_active_admin(self.session, actor)
        asset = await self.session.get(Asset, asset_id)
        if asset is None:
            raise UnknownAssetError(f"asset {asset_id} not in master")
        if asset.is_active == active:
            return asset  # 冪等 no-op
        async with self.write(actor):
            asset.is_active = active
            asset.updated_by = actor.value
            asset.source_actor = actor.value
        return asset

    async def set_available_for_service(
        self, asset_id: str, available: bool, actor: Actor
    ) -> Asset:
        """設「可服務」旗標 available_for_service(admin-only,治理寫入)。

        available_for_service = 設備清單 `available` 過濾的資料源(in/out of service);
        **資訊性**,不阻擋開單/領料。admin 限定在 domain 強制。冪等:同值 → no-op。
        """
        await assert_active_admin(self.session, actor)
        asset = await self.session.get(Asset, asset_id)
        if asset is None:
            raise UnknownAssetError(f"asset {asset_id} not in master")
        if asset.available_for_service == available:
            return asset  # 冪等 no-op
        async with self.write(actor):
            asset.available_for_service = available
            asset.updated_by = actor.value
            asset.source_actor = actor.value
        return asset

    # ---- 主檔編輯(admin-only;Jordan 2026-07-06。比照 ContactsService.update_organization。
    #      engineer 唯讀 —— 設備工程主檔的 engineer 提案面〔propose_update_asset〕未被要求,不做)----

    async def update_asset(
        self,
        asset_id: str,
        *,
        actor: Actor,
        description: str,
        asset_type: str,
        asset_subtype: str | None,
        department: str | None,
        line: str | None,
        site: str,
        model_no: str | None,
        serial_no: str | None,
        manufacturer: str | None,
        host_name: str | None,
        asset_ref: str | None,
        product: str | None,
        weblink: str | None,
        comments: str | None,
        process_segment_class: str | None,
        owners: list[str] | None = None,
    ) -> Asset:
        """設備主檔編輯(admin-only,治理寫入;顯示名 SoR=MES vw_equipment,D6c)。

        `owners`(設備負責人清單,0031)= 維修/保養工單 assignee 的事實來源;逐名正規化、去重
        保序,經 `asset_owner` 交叉表整組替換。`None` = **不動**負責人;`[]` = 清除所有負責人。
        改動後 PM/REACTIVE 未明確指派時由負責人清單衍生。

        ★ 不在此改:
        - `asset_id`(EID,PK):A4 禁 Key Change,且為 WO/relationship/external_id FK 目標。
        - `parent_asset_id`:組成圖權威在 `asset_relationship`(走 /admin/relationships),
          legacy 快取欄不開放直接編輯(改動會與關係表脫鉤)。
        - `is_active` / `available_for_service` / `up_down_tracking`:另有 governed flag 路由。
        - `picture_url`:媒體走 attachment 服務。

        `asset_type` 必填且須為既存 lookup code;`department` / `line` 給值須為既存 lookup code
        (查無 → AssetError,不靜默鑄新詞彙)。`description` 必填非空(主檔顯示名)。其餘文字欄
        空 → None。內容全未變 → no-op 不污染稽核。走 self.write() 單一寫入路徑(ADR-001/003)。
        """
        await assert_active_admin(self.session, actor)
        asset = await self.session.get(Asset, asset_id)
        if asset is None:
            raise UnknownAssetError(f"asset {asset_id} not in master")

        clean_desc = (description or "").strip()
        if not clean_desc:
            raise AssetError("asset description cannot be empty")
        clean_type = (asset_type or "").strip()
        if not clean_type:
            raise AssetError("asset_type is required")
        if await self.session.get(AssetType, clean_type) is None:
            raise AssetError(f"asset_type {clean_type} not found")
        clean_site = (site or "").strip()
        if not clean_site:
            raise AssetError("site cannot be empty")

        new_dept = _strip_or_none(department)
        if new_dept is not None and await self.session.get(Department, new_dept) is None:
            raise AssetError(f"department {new_dept} not found")
        new_line = _strip_or_none(line)
        if new_line is not None and await self.session.get(Line, new_line) is None:
            raise AssetError(f"line {new_line} not found")

        values = {
            "description": clean_desc,
            "asset_type": clean_type,
            "asset_subtype": _strip_or_none(asset_subtype),
            "department": new_dept,
            "line": new_line,
            "site": clean_site,
            "model_no": _strip_or_none(model_no),
            "serial_no": _strip_or_none(serial_no),
            "manufacturer": _strip_or_none(manufacturer),
            "host_name": _strip_or_none(host_name),
            "asset_ref": _strip_or_none(asset_ref),
            "product": _strip_or_none(product),
            "weblink": _strip_or_none(weblink),
            "comments": _strip_or_none(comments),
            "process_segment_class": _strip_or_none(process_segment_class),
        }
        clean_owners = _clean_names(owners) if owners is not None else None
        fields_changed = not all(getattr(asset, k) == v for k, v in values.items())
        # 冪等:主檔欄位與負責人皆未變 → no-op(不動稽核欄)。owners=None → 不比對負責人。
        if not fields_changed and clean_owners is None:
            return asset
        async with self.write(actor):
            if fields_changed:
                for k, v in values.items():
                    setattr(asset, k, v)
                asset.updated_by = actor.value
                asset.source_actor = actor.value
            if clean_owners is not None:
                await self._replace_owners(asset_id, clean_owners, actor)
        return asset

    async def create_asset(
        self,
        asset_id: str,
        *,
        actor: Actor,
        description: str,
        asset_type: str,
        asset_subtype: str | None = None,
        department: str | None = None,
        line: str | None = None,
        site: str = "PLANT-1",
        model_no: str | None = None,
        serial_no: str | None = None,
        manufacturer: str | None = None,
        host_name: str | None = None,
        asset_ref: str | None = None,
        product: str | None = None,
        weblink: str | None = None,
        comments: str | None = None,
        process_segment_class: str | None = None,
        owners: list[str] | None = None,
    ) -> Asset:
        """建立新設備主檔(admin-only,治理寫入;比照 update_asset 校驗,ADR-001/003)。

        `owners`(設備負責人清單,0031)逐名正規化、去重保序,經 `asset_owner` 交叉表建立;
        None / 空清單 = 不設負責人。

        用途 = admin 註冊 eMaint 尚未登記的新 EID(如新導入的機械手臂單元)。
        `asset_id` = EID = MES-EID(A4),由呼叫端提供、建立後不可變(PK / FK 目標)。
        `asset_id`:strip + upper 後須合 `^EID-\\d{5}$`(全 687 既有 EID 皆此形),否則 AssetError;
        已存在 → AssetError(這是 create 非 upsert,絕不靜默覆蓋;idempotent 載入走 upsert_asset)。

        ★ 不在此設(比照 update_asset docstring 同理):
        - `parent_asset_id`:組成圖權威在 `asset_relationship`(走 link_containment)。
        - `is_active` / `available_for_service` / `up_down_tracking`:另有 governed flag 路由;
          新資產取 model 預設(is_active=available_for_service=True)。
        - `picture_url`:媒體走 attachment 服務。

        `description` / `site` 必填非空;`asset_type` 必填且須為既存 lookup code;
        `department` / `line` 給值須為既存 lookup code(查無 → AssetError,不靜默鑄新詞彙)。
        其餘文字欄空 → None。走 self.write() 單一寫入路徑。
        """
        await assert_active_admin(self.session, actor)

        eid = (asset_id or "").strip().upper()
        if not _EID_RE.match(eid):
            raise AssetError(f"asset_id {eid!r} must match EID-xxxxx (5 digits)")
        if await self.session.get(Asset, eid) is not None:
            raise AssetError(f"asset {eid} already exists")

        clean_desc = (description or "").strip()
        if not clean_desc:
            raise AssetError("asset description cannot be empty")
        clean_type = (asset_type or "").strip()
        if not clean_type:
            raise AssetError("asset_type is required")
        if await self.session.get(AssetType, clean_type) is None:
            raise AssetError(f"asset_type {clean_type} not found")
        clean_site = (site or "").strip()
        if not clean_site:
            raise AssetError("site cannot be empty")

        new_dept = _strip_or_none(department)
        if new_dept is not None and await self.session.get(Department, new_dept) is None:
            raise AssetError(f"department {new_dept} not found")
        new_line = _strip_or_none(line)
        if new_line is not None and await self.session.get(Line, new_line) is None:
            raise AssetError(f"line {new_line} not found")

        async with self.write(actor):
            asset = Asset(
                asset_id=eid,
                description=clean_desc,
                asset_type=clean_type,
                asset_subtype=_strip_or_none(asset_subtype),
                department=new_dept,
                line=new_line,
                site=clean_site,
                model_no=_strip_or_none(model_no),
                serial_no=_strip_or_none(serial_no),
                manufacturer=_strip_or_none(manufacturer),
                host_name=_strip_or_none(host_name),
                asset_ref=_strip_or_none(asset_ref),
                product=_strip_or_none(product),
                weblink=_strip_or_none(weblink),
                comments=_strip_or_none(comments),
                process_segment_class=_strip_or_none(process_segment_class),
                created_by=actor.value,
                updated_by=actor.value,
                source_actor=actor.value,
            )
            self.session.add(asset)
            await self.session.flush()  # asset PK 落地後才能掛 asset_owner FK
            await self._replace_owners(eid, _clean_names(owners), actor)
        return asset

    async def register_external_id(
        self, asset_id: str, namespace: str, external_id: str, actor: Actor
    ) -> None:
        """綁定一個外部系統 id 到 asset(ADR-015 身分服務的 governed write)。"""
        stmt = (
            pg_insert(AssetExternalId)
            .values(
                asset_id=asset_id,
                namespace=namespace,
                external_id=external_id,
                source_actor=actor.value,
                created_by=actor.value,
            )
            .on_conflict_do_update(
                index_elements=["namespace", "external_id"],
                set_={"asset_id": asset_id, "updated_by": actor.value},
            )
        )
        await self.session.execute(stmt)

    # ---- 資產組成圖(ADR-018)----
    # 讀取(對外開放,ADR-004):關係列舉 + 含括後代 + WO rollup。

    async def list_relationships(
        self,
        asset_id: str,
        *,
        relationship_type: str | None = None,
        direction: str = "both",  # "from" | "to" | "both"
        active_only: bool = True,
    ) -> list[AssetRelationship]:
        stmt = select(AssetRelationship)
        if direction == "from":
            stmt = stmt.where(AssetRelationship.from_asset_id == asset_id)
        elif direction == "to":
            stmt = stmt.where(AssetRelationship.to_asset_id == asset_id)
        else:
            stmt = stmt.where(
                or_(
                    AssetRelationship.from_asset_id == asset_id,
                    AssetRelationship.to_asset_id == asset_id,
                )
            )
        if relationship_type is not None:
            stmt = stmt.where(AssetRelationship.relationship_type == relationship_type)
        if active_only:
            stmt = stmt.where(AssetRelationship.valid_to.is_(None))
        return list((await self.session.scalars(stmt.order_by(AssetRelationship.id))).all())

    async def list_relationships_all(
        self, *, active_only: bool = True
    ) -> list[AssetRelationship]:
        """全部關係邊(admin 維護頁統計用;唯讀,ADR-004)。預設只列現行(valid_to IS NULL)。"""
        stmt = select(AssetRelationship)
        if active_only:
            stmt = stmt.where(AssetRelationship.valid_to.is_(None))
        return list((await self.session.scalars(stmt.order_by(AssetRelationship.id))).all())

    async def get_contained_descendants(self, machine_id: str) -> list[str]:
        """BFS 沿現行 `contains_module` 邊,回所有後代模組 asset_id(不含自身)。

        只走 `contains_module`(樹);`shared_dependency` 不捲入(ADR-018 決策 7)。
        seen 防環(資料層即使誤入環也不會無限迴圈)。
        """
        result: list[str] = []
        seen: set[str] = {machine_id}
        frontier: list[str] = [machine_id]
        while frontier:
            stmt = select(AssetRelationship.to_asset_id).where(
                AssetRelationship.from_asset_id.in_(frontier),
                AssetRelationship.relationship_type == CONTAINS_MODULE,
                AssetRelationship.valid_to.is_(None),
            )
            next_frontier: list[str] = []
            for c in (await self.session.scalars(stmt)).all():
                if c not in seen:  # 立即標記:同層多父(菱形)時去重,避免後代重複
                    seen.add(c)
                    result.append(c)
                    next_frontier.append(c)
            frontier = next_frontier
        return result

    async def rollup_work_orders(
        self, machine_id: str, *, limit: int = 100, offset: int = 0
    ) -> list:
        """機台維護 rollup:自身 + `contains_module` 後代的工單(新到舊)。

        WO 維持掛開單當下的 EID(機台/模組混存);此處走樹把後代模組的 WO 一併收上來。
        """
        from cmms.domain.work_order.models import WorkOrder  # 延遲匯入避免循環

        ids = [machine_id, *await self.get_contained_descendants(machine_id)]
        stmt = (
            select(WorkOrder)
            .where(WorkOrder.asset_id.in_(ids))
            .order_by(WorkOrder.work_order_no.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.scalars(stmt)).all())

    # 寫入(governed,經 self.write() 交易;單一寫入路徑 ADR-001/003)。

    async def upsert_relationship_type(self, code: str, label: str) -> None:
        stmt = (
            pg_insert(AssetRelationshipType)
            .values(code=code, label=label)
            .on_conflict_do_nothing(index_elements=["code"])
        )
        await self.session.execute(stmt)

    async def _assert_exists(self, asset_id: str) -> None:
        """Q6 安全網:組成邊的端點 EID 必須在 asset 主檔,否則拒(不靜默建殘缺資產)。"""
        if await self.session.get(Asset, asset_id) is None:
            raise UnknownAssetError(f"asset {asset_id} not in master (cannot link unknown EID)")

    async def _active_edge(self, from_id: str, to_id: str, rtype: str) -> AssetRelationship | None:
        stmt = select(AssetRelationship).where(
            AssetRelationship.from_asset_id == from_id,
            AssetRelationship.to_asset_id == to_id,
            AssetRelationship.relationship_type == rtype,
            AssetRelationship.valid_to.is_(None),
        )
        return (await self.session.scalars(stmt)).first()

    async def _active_containment_parent(self, module_id: str) -> str | None:
        stmt = select(AssetRelationship.from_asset_id).where(
            AssetRelationship.to_asset_id == module_id,
            AssetRelationship.relationship_type == CONTAINS_MODULE,
            AssetRelationship.valid_to.is_(None),
        )
        return (await self.session.scalars(stmt)).first()

    async def _would_create_cycle(self, machine_id: str, module_id: str) -> bool:
        """若 module 已是 machine 的(經 contains_module）祖先,則新增 machine⊃module 會成環。"""
        seen: set[str] = set()
        cur: str | None = machine_id
        while cur is not None and cur not in seen:
            if cur == module_id:
                return True
            seen.add(cur)
            cur = await self._active_containment_parent(cur)
        return False

    async def link_containment(
        self,
        machine_id: str,
        module_id: str,
        actor: Actor,
        *,
        source: str = "cmms_curated",
        valid_from: datetime | None = None,
    ) -> AssetRelationship:
        """機台⊃模組(contains_module)。嚴格無環樹:模組同時只一個現行容器。idempotent。"""
        if machine_id == module_id:
            raise AssetError("containment self-loop not allowed")
        await self._assert_exists(machine_id)
        await self._assert_exists(module_id)
        existing = await self._active_edge(machine_id, module_id, CONTAINS_MODULE)
        if existing is not None:
            return existing  # idempotent
        parent = await self._active_containment_parent(module_id)
        if parent is not None and parent != machine_id:
            raise AssetError(
                f"module {module_id} already contained by {parent}; unlink before re-parenting"
            )
        if await self._would_create_cycle(machine_id, module_id):
            raise AssetError(f"containment cycle: {machine_id} is a descendant of {module_id}")
        rel = AssetRelationship(
            from_asset_id=machine_id,
            to_asset_id=module_id,
            relationship_type=CONTAINS_MODULE,
            source=source,
            valid_from=valid_from,
            source_actor=actor.value,
            created_by=actor.value,
        )
        self.session.add(rel)
        module = await self.session.get(Asset, module_id)
        if module is not None:  # denormalized 單親快取(權威=本表)
            module.parent_asset_id = machine_id
            module.updated_by = actor.value
        await self.session.flush()
        return rel

    async def link_shared_dependency(
        self,
        resource_id: str,
        machine_id: str,
        actor: Actor,
        *,
        source: str = "cmms_curated",
        valid_from: datetime | None = None,
    ) -> AssetRelationship:
        """共用資源→被服務機台(shared_dependency,N:M)。不寫 parent_asset_id。idempotent。"""
        if resource_id == machine_id:
            raise AssetError("shared_dependency self-loop not allowed")
        await self._assert_exists(resource_id)
        await self._assert_exists(machine_id)
        existing = await self._active_edge(resource_id, machine_id, SHARED_DEPENDENCY)
        if existing is not None:
            return existing  # idempotent
        rel = AssetRelationship(
            from_asset_id=resource_id,
            to_asset_id=machine_id,
            relationship_type=SHARED_DEPENDENCY,
            source=source,
            valid_from=valid_from,
            source_actor=actor.value,
            created_by=actor.value,
        )
        self.session.add(rel)
        await self.session.flush()
        return rel

    async def unlink_relationship(
        self, rel_id: int, actor: Actor, *, at: datetime | None = None
    ) -> None:
        """soft-unlink:設 valid_to(保留歷史)。contains_module 連帶清除單親快取。idempotent。"""
        rel = await self.session.get(AssetRelationship, rel_id)
        if rel is None or rel.valid_to is not None:
            return
        rel.valid_to = at or datetime.now(UTC)
        rel.updated_by = actor.value
        if rel.relationship_type == CONTAINS_MODULE:
            module = await self.session.get(Asset, rel.to_asset_id)
            if module is not None and module.parent_asset_id == rel.from_asset_id:
                module.parent_asset_id = None
                module.updated_by = actor.value
        await self.session.flush()
