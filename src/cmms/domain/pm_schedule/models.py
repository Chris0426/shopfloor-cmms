"""PmSchedule ORM models(對應 docs/domain-model/03-scheduled-activity.md §8)。

- lookup:`freq_unit`(Months/Weeks/Days)、`vendor`(CMA/CMB,與 WorkOrder 共用)。
- 反正規化欄(line_no/comp_desc/task_desc/pm_type)依 §5.1 全部 DROP,不建。
- `calendar_freq_type` 本切片存 text(S1 未定值域,不做受控 lookup;守護欄 #8)。
- `assigned_person` 存 text(FK→person 延到 Contacts 切片)。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    Numeric,
    String,
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


class FreqUnit(_CodeLabel, Base):
    __tablename__ = "freq_unit"  # Months / Weeks / Days


class Vendor(_CodeLabel, Base):
    __tablename__ = "vendor"  # CMA / CMB(承包商集合,WorkOrder 切片共用)


class PmSchedule(AuditMixin, Base):
    __tablename__ = "pm_schedule"
    # 天然唯一鍵:一台機器 + 一任務只能定義一次(§1.1,984 筆零重複)
    __table_args__ = (UniqueConstraint("asset_id", "task_id"),)

    # 識別與關聯
    pm_id: Mapped[str] = mapped_column(String, primary_key=True)  # pmid,eMaint 代理鍵
    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.asset_id"), nullable=False)  # compid
    task_id: Mapped[str] = mapped_column(ForeignKey("task.task_no"), nullable=False)  # task_no

    # 週期(frequency_interval=0 → 不週期 / 單次;此時 frequency_unit 為 null)
    frequency_interval: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    frequency_unit: Mapped[str | None] = mapped_column(ForeignKey("freq_unit.code"), nullable=True)
    # [UI] S1 未定值域(Shadow/Fixed)→ 先存 text、不做受控 lookup(守護欄 #8)
    calendar_freq_type: Mapped[str | None] = mapped_column(String, nullable=True)
    skip_weekends_holidays: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # [UI]

    # 排程時間軸
    next_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_pm_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # soft ref → work_order(WO 切片才有該表;7% 指向已清除舊工單,故不設 FK 約束)
    last_work_order_no: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_window_days: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)

    # 工時(S5:工時歸屬 ScheduledActivity;standard 與 estlabor 在 123/984 筆不同,兩欄並存)
    standard_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    estimated_labor_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)

    # 指派(assignto 拆解;person 延後 FK→Contacts 切片)
    assigned_vendor: Mapped[str | None] = mapped_column(ForeignKey("vendor.code"), nullable=True)
    assigned_person: Mapped[str | None] = mapped_column(String, nullable=True)
    pm_group: Mapped[str | None] = mapped_column(String, nullable=True)  # [UI]

    # 狀態(suppress 時不自動產生工單)
    is_suppressed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
