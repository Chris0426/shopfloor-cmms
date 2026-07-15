"""TelegramBridgeService — Telegram DM 綁定 / 冪等去重的 domain 半邊(續-15,唯一寫入路徑)。

- 綁定碼:`create_link_code`(產生一次性明文碼、雜湊落庫、作廢舊碼)/ `redeem_code`(驗
  hash+TTL+未用 → upsert link;綁定成功後由呼叫端回填通知 chat_id)。
- 解析:`resolve_user_by_chat`(chat_id → active user;webhook 判斷「誰在問」)。
- 解除:`unlink`(冪等)。
- webhook 冪等:`mark_update_seen`(INSERT ON CONFLICT DO NOTHING + opportunistic prune)。

治理:寫入皆經 `self.write(actor)`(交易邊界 + 稽核欄)。**綁定碼明文絕不落 log / 稽核**
(僅回呼叫端一次,比照 PAT / scoped token 慣例)。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.domain.base import DomainService
from cmms.domain.identity.models import UserAccount
from cmms.domain.telegram_bridge.models import (
    TelegramLink,
    TelegramLinkCode,
    TelegramUpdateSeen,
)

CODE_TTL_SECONDS = 600  # 綁定碼有效 10 分鐘


def hash_code(code: str) -> str:
    """綁定碼明文 → sha256 hex(明文不落庫;查碼一律以雜湊比對)。"""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class TelegramBridgeError(Exception):
    """Telegram 綁定 / 兌換錯誤(user 無效 / 碼無效或過期 / chat 撞他人)。"""


class TelegramBridgeService(DomainService):
    @staticmethod
    def _now() -> datetime:
        return datetime.now(UTC)

    async def create_link_code(self, user_id: str, actor: Actor) -> str:
        """為使用者產生一次性 Telegram 綁定碼(明文),TTL 10 分。回明文。

        - 驗使用者存在且 active(誠實錯誤)。
        - 作廢該使用者既有未用碼(一人同時只一組有效碼):直接刪除其所有未用碼列。
        - 落 `TelegramLinkCode(code_hash=sha256(碼))`;**明文只回呼叫端一次,絕不落 log / 稽核**。
        """
        user = await self.session.get(UserAccount, user_id)
        if user is None or not user.is_active:
            raise TelegramBridgeError(f"user {user_id!r} not found or inactive")
        code = secrets.token_urlsafe(9)
        now = self._now()
        async with self.write(actor):
            # 作廢舊碼:刪該 user 所有未用碼列(used_at IS NULL)。
            for row in (
                await self.session.scalars(
                    select(TelegramLinkCode).where(
                        TelegramLinkCode.user_id == user_id,
                        TelegramLinkCode.used_at.is_(None),
                    )
                )
            ).all():
                await self.session.delete(row)
            self.session.add(
                TelegramLinkCode(
                    code_hash=hash_code(code),
                    user_id=user_id,
                    expires_at=now + timedelta(seconds=CODE_TTL_SECONDS),
                    created_by=actor.value,
                    source_actor=actor.value,
                )
            )
        return code

    async def redeem_code(self, code: str, chat_id: str, actor: Actor) -> TelegramLink:
        """以綁定碼 + chat_id 建立 / 更新連結。成功回 `TelegramLink`。

        碼不存在 / 已用 / 過期 → 統一 `TelegramBridgeError("invalid or expired code")`
        (不區分,避免洩漏碼狀態)。chat_id 已綁**別人** → 誠實拒。同一交易內標碼已用 + upsert
        link(該 user 已有 link → 更新 chat_id〔REPLACE 自己那筆〕;沒有 → 建)。
        """
        chat_id = (chat_id or "").strip()
        if not chat_id:
            raise TelegramBridgeError("chat_id is required")
        now = self._now()
        row = await self.session.get(TelegramLinkCode, hash_code(code))
        if row is None or row.used_at is not None or row.expires_at <= now:
            raise TelegramBridgeError("invalid or expired code")
        existing_chat = await self.session.scalar(
            select(TelegramLink).where(TelegramLink.chat_id == chat_id)
        )
        if existing_chat is not None and existing_chat.user_id != row.user_id:
            raise TelegramBridgeError("chat already linked to another account")
        async with self.write(actor):
            row.used_at = now
            row.updated_by = actor.value
            row.source_actor = actor.value
            link = await self.session.get(TelegramLink, row.user_id)
            if link is None:
                link = TelegramLink(
                    user_id=row.user_id,
                    chat_id=chat_id,
                    created_by=actor.value,
                    source_actor=actor.value,
                )
                self.session.add(link)
            else:
                link.chat_id = chat_id
                link.updated_by = actor.value
                link.source_actor = actor.value
        return link

    async def get_link(self, user_id: str) -> TelegramLink | None:
        """該使用者的 Telegram 連結(未綁 → None)。讀取,不驗 admin(settings 頁顯示綁定狀態用)。"""
        return await self.session.get(TelegramLink, user_id)

    async def peek_code_user(self, code: str) -> str | None:
        """綁定碼 → 對應 user_id(碼存在 / 未用 / 未過期);否則 None。**不標已用**(純窺看)。

        webhook 用它決定 `redeem_code` 的 actor(=綁定者本人),不改任何狀態;真正的兌換 +
        標已用由 `redeem_code` 在單一交易內完成。碼無效不區分原因(避免洩漏碼狀態)。
        """
        row = await self.session.get(TelegramLinkCode, hash_code(code))
        if row is None or row.used_at is not None or row.expires_at <= self._now():
            return None
        return row.user_id

    async def resolve_user_by_chat(self, chat_id: str) -> UserAccount | None:
        """chat_id → 綁定的 active 使用者(inactive / 未綁 → None)。讀取,不驗 admin。"""
        return await self.session.scalar(
            select(UserAccount)
            .join(TelegramLink, TelegramLink.user_id == UserAccount.user_id)
            .where(TelegramLink.chat_id == chat_id, UserAccount.is_active.is_(True))
        )

    async def unlink(self, user_id: str, actor: Actor) -> bool:
        """解除該使用者的 Telegram 連結。冪等:未綁 → False;有刪 → True。"""
        link = await self.session.get(TelegramLink, user_id)
        if link is None:
            return False
        async with self.write(actor):
            await self.session.delete(link)
        return True

    async def mark_update_seen(self, update_id: int, *, actor: Actor | None = None) -> bool:
        """webhook 冪等守門:INSERT ON CONFLICT DO NOTHING。回「這次是否新插入」。

        False = Telegram 重送(呼叫端 skip 不重複處理)。同一交易 opportunistic prune
        (>7 天舊列)。`telegram_update_seen` 無稽核欄 → actor 僅用於交易邊界(不落任何欄);
        `Actor` 無 `system` kind,故預設 `scheduler`(webhook 端可覆寫),於此無稽核語意差。
        """
        who = actor or Actor.scheduler()
        async with self.write(who):
            result = await self.session.execute(
                pg_insert(TelegramUpdateSeen)
                .values(update_id=update_id)
                .on_conflict_do_nothing(index_elements=["update_id"])
            )
            await self.session.execute(
                text(
                    "DELETE FROM telegram_update_seen "
                    "WHERE received_at < now() - interval '7 days'"
                )
            )
        return bool(result.rowcount)
