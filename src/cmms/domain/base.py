"""Domain service 基底 + 寫入情境(write context)。

所有 service 繼承 `DomainService`,經 `write()` 取得交易情境並帶上發起者。
這集中了:單一寫入路徑、稽核欄填寫、交易邊界。各切片的 service(如
`AssetService`、`WorkOrderService`)在此之上實作領域操作(`get_asset`、
`close_work_order`…),**不**暴露裸 SQL(ADR-003)。

兩階段外部確認(ADR-016)會在後續切片補上 `propose()` / `confirm()`:
propose 建立持久化 pending proposal(回 token、不立即執行),confirm 攜帶
已驗證的 `human:<id>` 經本寫入路徑落地。此處先定義介面契約,實作隨 WorkOrder
切片進來。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from cmms.audit import Actor


def clean_person_name(value: str | None) -> str | None:
    """人名欄位正規化:strip 後空字串 → None(「未指派」單一 DB 表徵,review f14cf8d)。"""
    cleaned = (value or "").strip()
    return cleaned or None


class DomainService:
    """領域服務基底。持有一個 session,提供受稽核的寫入情境。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    @asynccontextmanager
    async def write(self, actor: Actor) -> AsyncIterator[AsyncSession]:
        """受治理的寫入交易。

        - 成功則 commit,失敗則 rollback(交易邊界集中於此)。
        - `actor` 為發起者;呼叫端在實際 mutation 時把它寫入稽核欄
          (`source_actor` 等),由各 service 的操作負責填。
        """
        try:
            yield self._session
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
