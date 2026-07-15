"""AssistantService — dock 助理對話的領域服務(唯一寫入路徑,ADR-001/003)。

所有方法 **user-scoped、擁有權在 domain 層強制**:每個讀寫都以 `user_id` 過濾 / 驗證,
non-owner 一律拿 None / 拒絕(AssistantError),不倚賴 route 守門。寫入經 `self.write()`
帶 `Actor.human(<user_id>)`(護欄 #1 + #4)。

- 惰性建會話:「新對話」按鈕不落 DB;首則訊息(add_user_message conversation_id=None)才建。
- **兩段式送出**(修「送出後、Hermes 回覆前轉跳 = 整輪蒸發」):
  · `add_user_message` —— phase 1,先落 user 訊息(惰性建會話),即刻回前端(不打 gateway)。
    這確保「in-flight 窗」內伺服器上會話 / user 訊息已存在,轉跳回來可續跑。
  · `add_assistant_message` —— phase 2,gateway 成功後才落 assistant 訊息。
  · gateway 失敗 → user 訊息**保留**(誠實、可重送),不落 assistant 訊息。
- 開啟中對話每人上限 `MAX_OPEN_CONVERSATIONS`,超過拒建(add_user_message 惰性路徑守門)。
"""

from __future__ import annotations

from sqlalchemy import func, select

from cmms.audit import Actor
from cmms.domain.assistant.models import AssistantConversation, AssistantMessage
from cmms.domain.base import DomainService

_TITLE_MAX = 60


class AssistantError(Exception):
    """助理對話領域錯誤(擁有權違反 / 已結束 / 超過上限)。"""


def _title_from(text: str) -> str:
    """首則使用者訊息 → 對話標題(壓平空白、截 ~60 字;空 → 佔位)。"""
    flat = " ".join((text or "").split())
    if not flat:
        return "…"
    return flat[:_TITLE_MAX]


class AssistantService(DomainService):
    # 開啟中對話每人上限(防無限累積;超過需先結束一個)
    MAX_OPEN_CONVERSATIONS = 10
    # gateway prompt 帶回的最近輪數(user+assistant 交錯 → 取 max_turns*2 則訊息)
    DEFAULT_HISTORY_TURNS = 8

    # ---- 讀取(全部 user-scoped)----

    async def get_conversation(
        self, user_id: str, conversation_id: int
    ) -> AssistantConversation | None:
        """單筆對話;非本人擁有 → None(擁有權強制,不洩漏他人對話存在與否)。"""
        conv = await self.session.get(AssistantConversation, conversation_id)
        if conv is None or conv.user_id != user_id:
            return None
        return conv

    async def list_open_conversations(self, user_id: str) -> list[AssistantConversation]:
        """本人開啟中(closed_at IS NULL)對話,依最近活動排序(updated_at 退回 created_at)。"""
        order = func.coalesce(
            AssistantConversation.updated_at, AssistantConversation.created_at
        ).desc()
        stmt = (
            select(AssistantConversation)
            .where(
                AssistantConversation.user_id == user_id,
                AssistantConversation.closed_at.is_(None),
            )
            .order_by(order)
        )
        return list((await self.session.scalars(stmt)).all())

    async def count_open_conversations(self, user_id: str) -> int:
        """本人開啟中對話數(上限守門用;不載列)。"""
        stmt = (
            select(func.count())
            .select_from(AssistantConversation)
            .where(
                AssistantConversation.user_id == user_id,
                AssistantConversation.closed_at.is_(None),
            )
        )
        return int((await self.session.scalar(stmt)) or 0)

    async def get_messages(self, user_id: str, conversation_id: int) -> list[AssistantMessage]:
        """對話全部訊息(依 id 遞增時序);非本人擁有 → 空清單(擁有權強制)。"""
        conv = await self.get_conversation(user_id, conversation_id)
        if conv is None:
            return []
        stmt = (
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation_id)
            .order_by(AssistantMessage.id)
        )
        return list((await self.session.scalars(stmt)).all())

    async def get_message(
        self, user_id: str, message_id: int
    ) -> AssistantMessage | None:
        """單則訊息(擁有權經其對話強制);非本人擁有 / 不存在 → None。

        phase 2 用:驗 hx-vals 帶回的 message_id 確屬本人某對話(且是 user 訊息)。
        """
        msg = await self.session.get(AssistantMessage, message_id)
        if msg is None:
            return None
        if await self.get_conversation(user_id, msg.conversation_id) is None:
            return None
        return msg

    async def next_message_after(
        self, user_id: str, conversation_id: int, message_id: int
    ) -> AssistantMessage | None:
        """該對話中 id > message_id 的**第一則**訊息;非本人 / 無 → None。

        phase 2 冪等/重放守門:若此則已是 assistant,代表該輪回覆早已落 DB
        (瀏覽器重送 / back-forward),直接回渲染既存回覆、不重打 gateway。
        """
        if await self.get_conversation(user_id, conversation_id) is None:
            return None
        stmt = (
            select(AssistantMessage)
            .where(
                AssistantMessage.conversation_id == conversation_id,
                AssistantMessage.id > message_id,
            )
            .order_by(AssistantMessage.id)
            .limit(1)
        )
        return (await self.session.scalars(stmt)).first()

    async def recent_history(
        self,
        user_id: str,
        conversation_id: int,
        max_turns: int | None = None,
        before_message_id: int | None = None,
    ) -> list[dict[str, str]]:
        """最近 max_turns 輪訊息 [{role, content}](供 gateway prompt);非本人 → 空。

        `before_message_id` 有值 → 只取 id < 該值的訊息(排除當輪 user 訊息本身,
        避免 gateway payload 的 `message` 參數 + history 重複同一句)。
        """
        conv = await self.get_conversation(user_id, conversation_id)
        if conv is None:
            return []
        turns = max_turns if max_turns is not None else self.DEFAULT_HISTORY_TURNS
        stmt = select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation_id
        )
        if before_message_id is not None:
            stmt = stmt.where(AssistantMessage.id < before_message_id)
        stmt = stmt.order_by(AssistantMessage.id.desc()).limit(max(0, turns) * 2)
        rows = list((await self.session.scalars(stmt)).all())
        rows.reverse()  # 由舊到新還原對話順序
        return [{"role": m.role, "content": m.content} for m in rows]

    # ---- 寫入(經 self.write() 交易;actor = Actor.human(user_id))----

    async def create_conversation(
        self, user_id: str, title: str, actor: Actor
    ) -> AssistantConversation:
        """建一則新對話(惰性:僅首則訊息時呼叫)。超過開啟中上限 → AssistantError。"""
        async with self.write(actor):
            return await self._create_locked(user_id, title, actor)

    async def _create_locked(
        self, user_id: str, title: str, actor: Actor
    ) -> AssistantConversation:
        """在既有 write 交易內建會話 + 上限守門 + flush 取 id(供 add_user_message 惰性路徑重用)。"""
        if await self.count_open_conversations(user_id) >= self.MAX_OPEN_CONVERSATIONS:
            raise AssistantError("open conversation limit reached")
        conv = AssistantConversation(
            user_id=user_id,
            title=_title_from(title),
            created_by=actor.value,
            source_actor=actor.value,
        )
        self.session.add(conv)
        await self.session.flush()  # 取得自增 id
        return conv

    async def _resolve_open_conversation(
        self, user_id: str, conversation_id: int
    ) -> AssistantConversation:
        """取本人開啟中對話(供兩段式續寫);不存在 / 非本人 / 已結束 → AssistantError。"""
        conv = await self.session.get(AssistantConversation, conversation_id)
        if conv is None or conv.user_id != user_id or conv.closed_at is not None:
            raise AssistantError("conversation not found, not owned, or closed")
        return conv

    async def add_user_message(
        self,
        *,
        user_id: str,
        conversation_id: int | None,
        content: str,
        actor: Actor,
    ) -> tuple[AssistantConversation, AssistantMessage]:
        """phase 1:落一則 user 訊息(不打 gateway),回 (對話, 訊息)。

        `conversation_id=None` → 惰性建會話(title = content 截短,套開啟中上限)。
        既有 conversation_id → 驗擁有權 + 未結束(否則 AssistantError,不寫)。
        觸碰 `updated_by` 推進 `updated_at`(切換列「最近活動」排序);flush 取 msg.id
        (phase 2 觸發器 hx-vals 需要)。
        """
        async with self.write(actor):
            if conversation_id is None:
                conv = await self._create_locked(user_id, content, actor)
            else:
                conv = await self._resolve_open_conversation(user_id, conversation_id)
            msg = AssistantMessage(
                conversation_id=conv.id,
                role="user",
                content=content,
                created_by=actor.value,
                source_actor=actor.value,
            )
            self.session.add(msg)
            conv.updated_by = actor.value  # 觸發 onupdate → updated_at(最近活動排序)
            await self.session.flush()  # 取 msg.id / conv.id
            return conv, msg

    async def add_assistant_message(
        self,
        *,
        user_id: str,
        conversation_id: int,
        content: str,
        actor: Actor,
    ) -> AssistantMessage:
        """phase 2:gateway 成功後落一則 assistant 訊息(驗擁有權 + 未結束)。

        觸碰 `updated_by` 推進 `updated_at`。失敗輪次由呼叫端**不呼叫本方法**
        (故 assistant 訊息不落 DB,user 訊息保留可重送)。
        """
        async with self.write(actor):
            conv = await self._resolve_open_conversation(user_id, conversation_id)
            msg = AssistantMessage(
                conversation_id=conv.id,
                role="assistant",
                content=content,
                created_by=actor.value,
                source_actor=actor.value,
            )
            self.session.add(msg)
            conv.updated_by = actor.value
            await self.session.flush()
            return msg

    async def close_conversation(
        self, user_id: str, conversation_id: int, actor: Actor
    ) -> None:
        """結束對話(設 closed_at;冪等)。非本人擁有 → AssistantError(不動他人資料)。"""
        conv = await self.session.get(AssistantConversation, conversation_id)
        if conv is None or conv.user_id != user_id:
            raise AssistantError("conversation not found or not owned")
        if conv.closed_at is not None:
            return  # 已結束:冪等 no-op
        async with self.write(actor):
            conv.closed_at = self._now()
            conv.updated_by = actor.value
