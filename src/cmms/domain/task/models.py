"""Task ORM model(對應 docs/domain-model/05-tasks.md §8)。

Task 是與機台無關的保養任務範本;本體單純,就 tasks.csv 的 2 欄
(task_no / task_desc)+ 一個閒置旗標。Task 不掛任何 asset / sub-type —
與設備的關係是經 ScheduledActivity 衍生的(05-tasks §1)。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class Task(AuditMixin, Base):
    __tablename__ = "task"

    # opaque 結構化代碼(長度 4–10)。前綴≈機種、尾綴 DM/DR…、DS/NS/SS=日/夜/小夜班;
    # 但編碼是 description 的非正式縮寫(description 即人讀解碼,Jordan 2026-06-20,T2),
    # 非可機械解析的文法 → 當不可變 PK 文字搬,不拆解為結構化欄位。
    task_no: Mapped[str] = mapped_column(String, primary_key=True)
    description: Mapped[str] = mapped_column(String, nullable=False)  # task_desc;441 筆皆唯一

    # 閒置範本旗標(T3):false = 已淘汰(所屬設備棄用,或任務經驗證不需執行,Jordan 2026-06-20)。
    # 載入時一律 true;「未被 ScheduledActivity 引用者標 false」需 join SA,延到 SA 切片(#3)
    # 載入後實證標記並維持(那時關聯才 live)。比照 Asset.is_active 的 [UI]-暫 default-true 做法。
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )


class TaskStep(AuditMixin, Base):
    """保養任務的一個步驟(task_steps_parts.csv 一列 = 一步驟;migration 0016)。

    ★ 身分 = 合成 `id`(穩定),`proc_seq` 純為排序/溯源屬性(eMaint 原始序號,可重複、可空;
      如 CAL1DM 八步同標 seq 10)。UI 依 (proc_seq, id) 排序後枚舉顯示 1..N,不顯示原始 10/20/30。
    ★ 一步 0..N 個零件 → 另立 `task_part` 子表(Jordan 2026-07-02:多料步驟很可能,現在就正規化,
      避免日後 live 資料搬遷)。當下資料一步 ≤1 料,但模型天生支援多料。
    冪等:`idempotency_key` = taskstep:v1:<task_no>:<occurrence>(occurrence = task_no 內檔案順序)。
    """

    __tablename__ = "task_step"
    __table_args__ = (
        Index("ix_task_step_task", "task_no", "proc_seq", "id"),  # 有序讀取熱路徑
        Index("uq_task_step_idem", "idempotency_key", unique=True),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_no: Mapped[str] = mapped_column(ForeignKey("task.task_no"), nullable=False)
    proc_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 原始序號(可空/重複)
    task_desc: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 軟刪(0020,護欄 #4:刪除也要留誰/何時;讀取面過濾 deleted_at IS NULL)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String, nullable=True)


class TaskPart(AuditMixin, Base):
    """步驟用料(task_step 1:N task_part;migration 0016)。

    一步可掛多個零件(現況 ≤1,模型支援多)。`replace_qty` 可空(當初造冊未清點 → 保持原狀,
    owner 需要再填,Jordan 2026-07-02)。item 對不上庫存主檔者由 loader 跳過本列 + 記數
    (承 ADR-018 不鑄 phantom;步驟本身仍保留)。
    """

    __tablename__ = "task_part"
    __table_args__ = (
        UniqueConstraint("task_step_id", "item_code", name="uq_task_part_step_item"),
        Index("ix_task_part_step", "task_step_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_step_id: Mapped[int] = mapped_column(ForeignKey("task_step.id"), nullable=False)
    item_code: Mapped[str] = mapped_column(ForeignKey("inventory_item.item_code"), nullable=False)
    replace_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    # 軟刪(0020;同 (step,item) 再掛 = 復活既有列,unique 約束含軟刪列)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_by: Mapped[str | None] = mapped_column(String, nullable=True)
