"""FailureVocabService — C2 失效詞彙的領域服務(唯一寫入路徑,ADR-001/003)。

讀取:兩軸 lookup 列表(admin 唯讀顯示 / 未來 reason_code rollup 用)。
寫入:載入器用的冪等 upsert。**只更新內容欄,絕不動 `is_active`** —— additive-only,
退役由 admin 治理(延 D6 切片);loader 重跑不得復活已退役的詞彙。
治理式 add/update(governed write)延後至 D6。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.base import DomainService
from cmms.domain.failure_vocab.models import EquipmentFailureCode, MesFailmode
from cmms.domain.failure_vocab.transform import (
    EquipmentFailureCodeImport,
    MesFailmodeImport,
)


class FailureVocabService(DomainService):
    # ---- 讀取(ADR-004)----

    async def list_mes_failmodes(self, station: str | None = None) -> list[MesFailmode]:
        """mfc 失效模式(可依 station 過濾)。唯讀。"""
        stmt = select(MesFailmode)
        if station is not None:
            stmt = stmt.where(MesFailmode.station == station)
        stmt = stmt.order_by(MesFailmode.station, MesFailmode.label)
        return list((await self.session.scalars(stmt)).all())

    async def list_equipment_failure_codes(
        self, station_hint: str | None = None
    ) -> list[EquipmentFailureCode]:
        """efc 設備故障碼(可依 station_hint 過濾;hint 為前綴推斷,非權威)。唯讀。"""
        stmt = select(EquipmentFailureCode)
        if station_hint is not None:
            stmt = stmt.where(EquipmentFailureCode.station_hint == station_hint)
        stmt = stmt.order_by(EquipmentFailureCode.code)
        return list((await self.session.scalars(stmt)).all())

    # ---- 寫入(經 self.write() 交易;載入器用,idempotent)----

    async def upsert_mes_failmode(self, data: MesFailmodeImport, actor: Actor) -> None:
        """冪等 upsert(自然鍵 (station, label));衝突只更新內容欄,**不動 is_active**。"""
        values = {
            "station": data.station,
            "label": data.label,
            "signal_id": data.signal_id,
            "entry_kind": data.entry_kind,
            "seg_class": data.seg_class,
            "mes_variable": data.mes_variable,
            "material_class": data.material_class,
            "semantic_zh": data.semantic_zh,
            "dominant_in_chronic": data.dominant_in_chronic,
            "source_adapter": data.source_adapter,
            "notes": data.notes,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        # 更新集排除自然鍵、created_by 與 is_active(additive-only:退役旗不由 loader 翻)。
        update_cols = {
            k: v for k, v in values.items() if k not in ("station", "label", "created_by")
        }
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(MesFailmode)
            .values(**values)
            .on_conflict_do_update(index_elements=["station", "label"], set_=update_cols)
        )
        await self.session.execute(stmt)

    async def upsert_equipment_failure_code(
        self, data: EquipmentFailureCodeImport, actor: Actor
    ) -> None:
        """冪等 upsert(自然鍵 code);衝突只更新內容欄,**不動 is_active**。"""
        values = {
            "code": data.code,
            "descr": data.descr,
            "station_hint": data.station_hint,
            "recency_status": data.recency_status,
            "source_actor": actor.value,
            "created_by": actor.value,
        }
        update_cols = {k: v for k, v in values.items() if k not in ("code", "created_by")}
        update_cols["updated_by"] = actor.value
        stmt = (
            pg_insert(EquipmentFailureCode)
            .values(**values)
            .on_conflict_do_update(index_elements=["code"], set_=update_cols)
        )
        await self.session.execute(stmt)
