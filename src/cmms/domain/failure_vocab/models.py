"""failure_vocab ORM models(對應 docs/domain-model/08-failure-vocab.md §7)。

兩軸 lookup(詞彙來源方鐵則:永不合併):
- `mes_failmode`(mfc,product/yield 軸):唯一鍵 (station, label);signal_id 跨站碰撞,
  nullable(三分流列無 signal_id)。
- `equipment_failure_code`(efc,equipment 軸):唯一鍵 code。

additive-only:`is_active` 旗標退役,loader 永不翻(見 service upsert)。
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class MesFailmode(AuditMixin, Base):
    """mfc 軸:MES 失效模式(料為何被判退)。自然鍵 = (station, label)。"""

    __tablename__ = "mes_failmode"
    # 自然鍵 = (station, label) 複合唯一鍵 —— signal_id 跨站碰撞不可單獨當鍵(詞彙來源方鐵則);
    # loader 的 on_conflict 冪等 upsert 亦掛此鍵。station 熱路徑索引供 list 過濾。
    __table_args__ = (
        Index("uq_mes_failmode_station_label", "station", "label", unique=True),
        Index("ix_mes_failmode_station", "station"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    station: Mapped[str] = mapped_column(String, nullable=False)  # 站 key(sta1/prober/sta3/…)
    label: Mapped[str] = mapped_column(String, nullable=False)  # 短標籤(FAIL_FLAGS 第二元)
    signal_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # mes.failmode.<label 小寫>;三分流列留空(面值保存,不重算)
    entry_kind: Mapped[str] = mapped_column(
        String, nullable=False
    )  # 'fail_flag' | 'triage_category'
    seg_class: Mapped[str | None] = mapped_column(String, nullable=True)  # SegmentClassID
    mes_variable: Mapped[str | None] = mapped_column(String, nullable=True)  # 落檔旗標變數名
    material_class: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # assyModule / assyCarrier
    semantic_zh: Mapped[str | None] = mapped_column(Text, nullable=True)  # 白話語意(cmms 可改寫)
    dominant_in_chronic: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # chronic 主導旗(y+數字 / n / TODO(校準) / raw90d:<n>);面值保存,不解讀
    source_adapter: Mapped[str | None] = mapped_column(String, nullable=True)  # 出處 adapter 檔
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )  # 退役旗;loader 永不翻(additive-only,admin 治理)


class EquipmentFailureCode(AuditMixin, Base):
    """efc 軸:MES 設備故障碼(機台為何故障)。自然鍵 = code。僅詞彙層。"""

    __tablename__ = "equipment_failure_code"
    __table_args__ = (Index("uq_equipment_failure_code_code", "code", unique=True),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, nullable=False)  # efc 變數名(= FailureCode 值)
    descr: Mapped[str | None] = mapped_column(Text, nullable=True)  # FailureDescription 人讀說明
    station_hint: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # 前綴推斷站別(非權威);種子 'TODO' → None(4 SA 家族站別未解)
    recency_status: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # source_alive_2026-07(round-3 判活源)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )  # 退役旗;loader 永不翻(additive-only,admin 治理)
