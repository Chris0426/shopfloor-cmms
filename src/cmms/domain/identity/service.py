"""IdentityService — 帳號 / 認證 / session / RBAC(ADR-022,唯一寫入路徑)。

- 密碼 argon2id(永不存 / 回傳明文)。
- authenticate → 建 DB-backed session,回 opaque token(cookie 用)。
- resolve_user(token)→ 驗有效 / 未過期 / 未撤 / 帳號 active。
- logout / admin 撤銷 = 設 `revoked_at`(即時生效)。
- set_locale:per-user ui/jira locale 寫回 user_account(ADR-023,取代 scaffold 的 cookie 暫存)。
RBAC(admin/engineer)由 web 層 require_* 依 `role` 強制;不放寬 agent(ADR-022 決策 5)。
"""

from __future__ import annotations

import contextlib
import secrets
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy import func, select, update

from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.base import DomainService
from cmms.domain.identity.models import McpScopedToken, UserAccount, UserSession

_PH = PasswordHasher()
# 固定 dummy hash:帳號不存在時仍跑一次 verify,平衡計時、避免帳號枚舉。
_DUMMY_HASH = _PH.hash("cmms-timing-equalizer")

SESSION_TTL_DAYS = 14
# operator = iPad 產線共用帳號(只開 REACTIVE 報修 + 取消自己誤報,不做其他 cmms 業務)。
VALID_ROLES = frozenset({"admin", "engineer", "operator"})
MIN_PASSWORD_LEN = 8  # MVP 密碼政策(ADR-022 待決預設):最短 8,無複雜度/過期/歷史

# ---- /mcp transport 層驗證出的委派身分(agent 試點;ADR-020 決策 5)----
# api/auth.py 的 MCP bearer 閘門對每個 /mcp 請求 resolve_scoped_token 成功後 set 為
# (user_id, scope)、請求結束 reset。mcp/server.py 的委派寫入工具在 `scoped_token`
# 參數缺省時 fallback 至此(參數仍優先,向後相容)——agent(Codex / gateway)不必
# 在每次工具呼叫重複塞 token。per-request contextvar,anyio 子 task 自動繼承。
mcp_transport_identity: ContextVar[tuple[str, str] | None] = ContextVar(
    "mcp_transport_identity", default=None
)


class IdentityError(Exception):
    """帳號 / 身分錯誤。"""


class AuthenticationError(IdentityError):
    """登入失敗(帳密不符 / 停用)。訊息刻意不區分,避免帳號枚舉。"""


class AuthorizationError(IdentityError):
    """授權不足(非 admin 執行管理操作;ADR-022 決策 4,RBAC 在 domain service 強制)。"""


async def get_active_account(session, actor: Actor) -> UserAccount | None:
    """actor(human:<id>)→ 現行 active 的 user_account;非 human / 查無 / 停用 → None。

    跨 domain 共用的角色解析(review f14cf8d:RBAC 要在 domain 層強制,不能只靠
    route 藏按鈕 / caller 自報 as_admin)。"""
    if not actor.is_human():
        return None
    account = await session.get(UserAccount, actor.value.split(":", 1)[1])
    if account is None or not account.is_active:
        return None
    return account


async def is_active_admin(session, actor: Actor) -> bool:
    account = await get_active_account(session, actor)
    return account is not None and account.role == "admin"


async def assert_active_admin(session, actor: Actor) -> None:
    """actor 必須是「現行 active 的 admin」,否則 raise AuthorizationError(強制點在 domain)。"""
    if not await is_active_admin(session, actor):
        raise AuthorizationError(f"{actor.value} is not an active admin")


async def is_operator(session, actor: Actor) -> bool:
    """actor 是否為「現行 active 的 operator」(iPad 產線共用帳號)。

    human actor 且 active 帳號 role=="operator" 才 True;agent actor(scheduler/on-box/
    mes-pipeline)一律 False —— operator 白名單閘只限縮真人共用帳號,絕不影響自動化寫入路徑。"""
    account = await get_active_account(session, actor)
    return account is not None and account.role == "operator"


class IdentityService(DomainService):
    async def create_user(
        self,
        *,
        user_id: str,
        username: str,
        display_name: str,
        password: str,
        org: str,
        actor: Actor,
        role: str = "engineer",
        emaint_assignee: str | None = None,
    ) -> str:
        """建帳號(admin / bootstrap)。密碼即刻 argon2id 雜湊。回 user_id。

        `emaint_assignee` = 此人在 legacy 工單/保養的指派名(assigned_person),供「我的」過濾。
        """
        if role not in VALID_ROLES:
            raise IdentityError(f"invalid role: {role}")
        user = UserAccount(
            user_id=user_id,
            username=username,
            display_name=display_name,
            password_hash=_PH.hash(password),
            org=org,
            role=role,
            emaint_assignee=(emaint_assignee.strip() if emaint_assignee else None),
            created_by=actor.value,
            source_actor=actor.value,
        )
        async with self.write(actor):
            self.session.add(user)
        return user_id

    async def authenticate(self, username: str, password: str) -> tuple[str, str]:
        """驗帳密 → 建 session。回 (user_id, session_token)。失敗 raise AuthenticationError。"""
        user = await self.session.scalar(
            select(UserAccount).where(UserAccount.username == username)
        )
        if user is None or not user.is_active:
            # 計時平衡:帳號不存在 / 停用時仍跑一次 verify,避免帳號枚舉的時間差
            with contextlib.suppress(VerifyMismatchError):
                _PH.verify(_DUMMY_HASH, password)
            raise AuthenticationError("invalid credentials")
        try:
            _PH.verify(user.password_hash, password)
        except VerifyMismatchError:
            raise AuthenticationError("invalid credentials") from None

        user_id = user.user_id
        token = secrets.token_urlsafe(32)
        sess = UserSession(
            session_token=token,
            user_id=user_id,
            expires_at=datetime.now(UTC) + timedelta(days=SESSION_TTL_DAYS),
        )
        async with self.write(Actor.human(user_id)):
            self.session.add(sess)
        return user_id, token

    async def resolve_user(self, token: str | None) -> UserAccount | None:
        """token → 有效使用者(未過期 / 未撤 / active)。無效回 None。"""
        if not token:
            return None
        sess = await self.session.get(UserSession, token)
        if sess is None or sess.revoked_at is not None or sess.expires_at <= datetime.now(UTC):
            return None
        user = await self.session.get(UserAccount, sess.user_id)
        if user is None or not user.is_active:
            return None
        return user

    async def logout(self, token: str | None) -> None:
        """撤銷 session(即時生效)。冪等:已撤 / 不存在皆 no-op。"""
        if not token:
            return
        sess = await self.session.get(UserSession, token)
        if sess is not None and sess.revoked_at is None:
            async with self.write(Actor.human(sess.user_id)):
                sess.revoked_at = datetime.now(UTC)

    async def set_locale(
        self,
        user_id: str,
        *,
        actor: Actor,
        ui_locale: str | None = None,
        jira_output_locale: str | None = None,
    ) -> None:
        """寫回 per-user locale(ADR-023)。只改傳入的欄位。"""
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        async with self.write(actor):
            if ui_locale is not None:
                user.ui_locale = ui_locale
            if jira_output_locale is not None:
                user.jira_output_locale = jira_output_locale
            user.updated_by = actor.value

    async def set_emaint_assignee(
        self, user_id: str, *, assignee: str | None, actor: Actor
    ) -> None:
        """設 legacy 指派名(= 工單/PM `assigned_person` 的確切字串,如 "Jordan Lee")。

        供「我的工單/保養」過濾(Slice 2;ADR-022 migration 0014)。空字串 / 空白 → None(清除,
        該用戶回到監督者語意:Mine 空、切 All 看全部)。governed write、記稽核。
        """
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        async with self.write(actor):
            user.emaint_assignee = assignee.strip() if assignee and assignee.strip() else None
            user.updated_by = actor.value

    # ---- 管理台(ADR-022 決策 4;RBAC 在 domain service 強制,非只藏 UI 按鈕)----

    async def _assert_admin(self, actor: Actor) -> None:
        """actor 必須是「現行 active 的 admin」,否則 raise AuthorizationError(強制點在此)。"""
        await assert_active_admin(self.session, actor)

    async def _active_admin_count(self) -> int:
        n = await self.session.scalar(
            select(func.count())
            .select_from(UserAccount)
            .where(UserAccount.role == "admin", UserAccount.is_active.is_(True))
        )
        return n or 0

    async def _revoke_user_sessions(self, user_id: str) -> None:
        """撤銷某用戶所有未撤現行 session(即時登出)。在呼叫端 write() 交易內執行。"""
        await self.session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def list_users(
        self, *, org: str | None = None, include_inactive: bool = True
    ) -> list[UserAccount]:
        """列帳號(讀取;admin 面,web 層 require_admin 把關)。"""
        stmt = select(UserAccount)
        if org is not None:
            stmt = stmt.where(UserAccount.org == org)
        if not include_inactive:
            stmt = stmt.where(UserAccount.is_active.is_(True))
        return list((await self.session.scalars(stmt.order_by(UserAccount.username))).all())

    async def deactivate_user(self, user_id: str, *, actor: Actor) -> None:
        """停用帳號(is_active=False → resolve_user 即時擋登入)。防停自己 / 停末位 admin。"""
        await self._assert_admin(actor)
        if actor.value == Actor.human(user_id).value:
            raise IdentityError("cannot deactivate your own account")
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        if user.role == "admin" and user.is_active and await self._active_admin_count() <= 1:
            raise IdentityError("cannot deactivate the last active admin")
        async with self.write(actor):
            user.is_active = False
            user.updated_by = actor.value
            user.source_actor = actor.value

    async def reactivate_user(self, user_id: str, *, actor: Actor) -> None:
        await self._assert_admin(actor)
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        async with self.write(actor):
            user.is_active = True
            user.updated_by = actor.value
            user.source_actor = actor.value

    async def set_role(self, user_id: str, role: str, *, actor: Actor) -> None:
        """改角色(admin/engineer)。防自我降級 + 防降末位 admin。"""
        await self._assert_admin(actor)
        if role not in VALID_ROLES:
            raise IdentityError(f"invalid role: {role}")
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        demoting_admin = role != "admin" and user.role == "admin" and user.is_active
        if demoting_admin:
            if actor.value == Actor.human(user_id).value:
                raise IdentityError("cannot demote your own admin account")
            if await self._active_admin_count() <= 1:
                raise IdentityError("cannot demote the last active admin")
        async with self.write(actor):
            user.role = role
            user.updated_by = actor.value
            user.source_actor = actor.value

    async def reset_password(self, user_id: str, new_password: str, *, actor: Actor) -> None:
        """admin 代重設密碼(argon2id 重雜湊)+ 撤銷該用戶現行 session(強制重登)。"""
        await self._assert_admin(actor)
        if len(new_password) < MIN_PASSWORD_LEN:
            raise IdentityError(f"password too short (min {MIN_PASSWORD_LEN})")
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        async with self.write(actor):
            user.password_hash = _PH.hash(new_password)
            user.updated_by = actor.value
            user.source_actor = actor.value
            await self._revoke_user_sessions(user_id)

    async def change_password(
        self, user_id: str, old_password: str, new_password: str, *, actor: Actor
    ) -> None:
        """自助改密碼(非 admin 面):驗舊密碼 → 重雜湊。actor 必須是本人(defense)。"""
        if actor.value != Actor.human(user_id).value:
            raise AuthorizationError("can only change your own password")
        user = await self.session.get(UserAccount, user_id)
        if user is None:
            raise IdentityError(f"user {user_id} not found")
        try:
            _PH.verify(user.password_hash, old_password)
        except VerifyMismatchError:
            raise AuthenticationError("old password incorrect") from None
        if len(new_password) < MIN_PASSWORD_LEN:
            raise IdentityError(f"password too short (min {MIN_PASSWORD_LEN})")
        async with self.write(actor):
            user.password_hash = _PH.hash(new_password)
            user.updated_by = actor.value
            user.source_actor = actor.value

    # ---- MCP scoped token(ADR-020 決策 5;MCP authN/authZ「最後一塊治理磚」)----

    async def _mint_scoped_token_row(
        self,
        *,
        user_id: str,
        agent: str,
        scope: str,
        ttl_seconds: int,
        at: datetime | None = None,
    ) -> tuple[str, datetime]:
        """落一列 mcp_scoped_token(共用核心)。回 (token 明文, expires_at)。

        ★ token 明文只回給呼叫端一次;**絕不落 log / 稽核**(比照 PAT 慣例)。
        """
        now = at or datetime.now(UTC)
        token = secrets.token_urlsafe(32)
        expires_at = now + timedelta(seconds=ttl_seconds)
        row = McpScopedToken(
            token=token,
            user_id=user_id,
            agent=agent,
            scope=scope,
            expires_at=expires_at,
        )
        async with self.write(Actor.human(user_id)):
            self.session.add(row)
        return token, expires_at

    async def mint_scoped_token(
        self,
        *,
        session_token: str | None,
        agent: str,
        scope: str,
        at: datetime | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """從有效 web session 鑄一個短命 MCP scoped token(帶委派 human:<id> + agent + scope)。

        TTL 預設 `config.scoped_token_ttl_seconds`(gateway 短票語意,行為零變);呼叫端可用
        `ttl_seconds` 覆蓋。session 無效/過期/撤 → raise。agent 以此 token 連 cmms MCP,
        cmms 據此把操作歸屬到「agent:<name> 代表 human:<id>」。
        """
        user = await self.resolve_user(session_token)
        if user is None:
            raise IdentityError("cannot mint scoped token from an invalid session")
        ttl = ttl_seconds if ttl_seconds is not None else get_settings().scoped_token_ttl_seconds
        token, _ = await self._mint_scoped_token_row(
            user_id=user.user_id, agent=agent, scope=scope, ttl_seconds=ttl, at=at
        )
        return token

    async def mint_scoped_token_for_user(
        self,
        *,
        username: str,
        agent: str,
        scope: str,
        ttl_seconds: int | None = None,
        at: datetime | None = None,
    ) -> tuple[str, datetime]:
        """operator 面(CLI `cmms mcp-token`)直接為使用者鑄 MCP token(agent 試點)。

        不經 web session(operator bootstrap provenance,比照 `user-create`);token 落
        mcp_scoped_token 表,與 session 衍生 token 同一撤銷面(`revoked_at` 即時失效、
        `revoke_scoped_tokens_for_user` / admin 可撤)。使用者必須存在且 active。
        TTL 預設 `config.mcp_pilot_token_ttl_seconds`(12h)。回 (token 明文, expires_at)。
        """
        user = await self.session.scalar(
            select(UserAccount).where(UserAccount.username == username)
        )
        if user is None or not user.is_active:
            raise IdentityError(f"user {username!r} not found or inactive")
        ttl = (
            ttl_seconds
            if ttl_seconds is not None
            else get_settings().mcp_pilot_token_ttl_seconds
        )
        return await self._mint_scoped_token_row(
            user_id=user.user_id, agent=agent, scope=scope, ttl_seconds=ttl, at=at
        )

    async def revoke_scoped_tokens_for_user(self, *, username: str) -> int:
        """撤銷使用者**全部**現行 MCP scoped token(設 revoked_at,即時失效)。回撤銷筆數。

        冪等:已撤 / 已過期不重複計;查無使用者 → raise(避免拼錯帳號誤以為已撤)。
        """
        user = await self.session.scalar(
            select(UserAccount).where(UserAccount.username == username)
        )
        if user is None:
            raise IdentityError(f"user {username!r} not found")
        now = datetime.now(UTC)
        async with self.write(Actor.human(user.user_id)):
            result = await self.session.execute(
                update(McpScopedToken)
                .where(
                    McpScopedToken.user_id == user.user_id,
                    McpScopedToken.revoked_at.is_(None),
                    McpScopedToken.expires_at > now,
                )
                .values(revoked_at=now)
            )
        return result.rowcount or 0

    async def resolve_scoped_token(self, token: str | None) -> tuple[str, str] | None:
        """scoped token → (user_id, scope)(有效/未過期/未撤/帳號 active);否則 None。"""
        if not token:
            return None
        st = await self.session.get(McpScopedToken, token)
        if st is None or st.revoked_at is not None or st.expires_at <= datetime.now(UTC):
            return None
        user = await self.session.get(UserAccount, st.user_id)
        if user is None or not user.is_active:
            return None
        return (st.user_id, st.scope)
