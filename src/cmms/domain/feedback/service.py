"""FeedbackService — 說明中心回饋的 domain 半邊(續-16,唯一寫入路徑)。

- `create`:任何登入者留言(strip 後必非空、>2000 字元誠實拒)。回落庫列。
- `list_open` / `list_recent_resolved`:admin 頁讀取(開放中舊→新;近期已處理新→舊)。
- `mark_resolved`:**admin-only**(`assert_active_admin`),冪等(已處理直接回)。

治理:寫入皆經 `self.write(actor)`(交易邊界 + 稽核欄)。
"""

from __future__ import annotations

from sqlalchemy import select

from cmms.audit import Actor
from cmms.domain.base import DomainService
from cmms.domain.feedback.models import HelpFeedback
from cmms.domain.identity.service import assert_active_admin

MAX_MESSAGE_LEN = 2000  # 回饋留言長度上限(超過誠實拒,避免濫用)


class FeedbackError(Exception):
    """回饋錯誤(空留言 / 超長)。"""


class FeedbackService(DomainService):
    async def create(self, user_id: str, message: str, actor: Actor) -> HelpFeedback:
        """留一則說明中心回饋。strip 後必非空、長度 ≤ 2000。回落庫列。"""
        body = (message or "").strip()
        if not body:
            raise FeedbackError("message is required")
        if len(body) > MAX_MESSAGE_LEN:
            raise FeedbackError(f"message too long (max {MAX_MESSAGE_LEN} chars)")
        fb = HelpFeedback(
            user_id=user_id,
            message=body,
            created_by=actor.value,
            source_actor=actor.value,
        )
        async with self.write(actor):
            self.session.add(fb)
        return fb

    async def list_open(self) -> list[HelpFeedback]:
        """開放中(未處理)回饋,舊→新(先進先處理)。讀取,不驗 admin(route 已 require_admin)。"""
        return list(
            (
                await self.session.scalars(
                    select(HelpFeedback)
                    .where(HelpFeedback.resolved_at.is_(None))
                    .order_by(HelpFeedback.id.asc())
                )
            ).all()
        )

    async def list_recent_resolved(self, limit: int = 10) -> list[HelpFeedback]:
        """近期已處理回饋,新→舊。讀取,不驗 admin。

        次序鍵補 `id desc`:同一批一次處理完時 `resolved_at` 可能落在同一個時鐘 tick
        (時鐘解析度粗於迴圈速度)→ 只依 resolved_at 排序會 tie、顯示順序不定。
        """
        return list(
            (
                await self.session.scalars(
                    select(HelpFeedback)
                    .where(HelpFeedback.resolved_at.is_not(None))
                    .order_by(HelpFeedback.resolved_at.desc(), HelpFeedback.id.desc())
                    .limit(limit)
                )
            ).all()
        )

    async def mark_resolved(self, feedback_id: int, actor: Actor) -> HelpFeedback:
        """標記回饋為已處理(**admin-only**)。冪等:已處理直接回,不覆蓋原處理者 / 時間。

        找不到 → `FeedbackError`。
        """
        await assert_active_admin(self.session, actor)
        fb = await self.session.get(HelpFeedback, feedback_id)
        if fb is None:
            raise FeedbackError(f"feedback {feedback_id} not found")
        if fb.resolved_at is not None:
            return fb  # 冪等:已處理不重設
        async with self.write(actor):
            fb.resolved_at = self._now()
            fb.resolved_by = actor.value
            fb.updated_by = actor.value
            fb.source_actor = actor.value
        return fb
