"""跨實體共用的稽核基礎(ADR-005,ADR-016 擴充)。

每筆 mutation 必須可追溯、可歸因(LLM governance 的核心)。
- 一般寫入:單一 `source_actor`(human / agent:<name> / mes-pipeline)。
- 兩階段外部確認寫入(ADR-016):另記 `proposed_by` + `confirmed_by`;
  最終 `source_actor` 取**確認者**(最終問責歸屬按下確認的人)。

`source_actor` 的字串慣例由 `Actor` 統一產生,避免各處手拼字串。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column


@dataclass(frozen=True, slots=True)
class Actor:
    """寫入發起者。序列化成 `source_actor` / `proposed_by` / `confirmed_by` 字串。

    例:`Actor.human("jordan.lee")` -> "human:jordan.lee"
        `Actor.agent("analytics")`    -> "agent:analytics"
        `Actor.mes_pipeline()`      -> "mes-pipeline"
        `Actor.scheduler()`         -> "scheduler"
    """

    value: str

    @classmethod
    def human(cls, user_id: str) -> Actor:
        return cls(f"human:{user_id}")

    @classmethod
    def agent(cls, name: str) -> Actor:
        return cls(f"agent:{name}")

    @classmethod
    def mes_pipeline(cls) -> Actor:
        return cls("mes-pipeline")

    @classmethod
    def scheduler(cls) -> Actor:
        """內部時鐘 / 排程器驅動的自動寫入(ADR-021 time-based PM)。

        與 `mes-pipeline` 區隔:後者是 MES 生產數 ingest 驅動;`scheduler` 是純內部時鐘
        驅動、與 MES 無關。誠實標示時鐘來源,避免稽核誤判;可重用於未來任何內部排程自動寫入。
        """
        return cls("scheduler")

    def is_human(self) -> bool:
        return self.value.startswith("human:")

    def is_agent(self) -> bool:
        return self.value.startswith("agent:")


class AuditMixin:
    """稽核欄位 mixin。所有可寫實體繼承之(ARCHITECTURE.md §5)。

    寫入一律經 domain service;這些欄位由 domain service 填,不由 thin client 自填。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)

    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)

    # 一般寫入的發起者;兩階段時 = 確認者(ADR-005/016)
    source_actor: Mapped[str | None] = mapped_column(String, nullable=True)

    # 兩階段外部確認(ADR-016);直接寫入留空
    proposed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String, nullable=True)
