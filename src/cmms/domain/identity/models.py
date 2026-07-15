"""Identity ORM models(ADR-022)。

- user_account:本地帳密(argon2id)+ 粗粒度 RBAC(admin/engineer)+ per-user locale(ADR-023)。
  `user_id` = `human:<id>` 的 `<id>`(稽核 / 授權 principal);`username` = 登入帳號。
- user_session:**DB-backed opaque token session**(cookie 只存 token,session 事實在 DB)。
  即時撤銷 = 設 `revoked_at` / 刪列;**無需簽章密鑰**(token 為隨機不可猜,查 DB 驗證)。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class UserAccount(AuditMixin, Base):
    __tablename__ = "user_account"

    user_id: Mapped[str] = mapped_column(String, primary_key=True)  # = human:<id> 的 <id>
    username: Mapped[str] = mapped_column(String, unique=True, nullable=False)  # 登入帳號
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)  # argon2id(永不存明文)
    org: Mapped[str] = mapped_column(String, nullable=False)  # plant / contractor(受控字串)
    # 粗粒度 RBAC(ADR-022 決策 4):MVP 兩級,在 domain service 層強制
    role: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'engineer'"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # per-user locale(ADR-023):登入沿用至再改
    ui_locale: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'en'"))
    jira_output_locale: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'en'")
    )
    # legacy 指派名橋(Slice 2):存 assigned_person 的確切字串(如 "Alice Fang")。
    # 「我的工單/保養」= assigned_person == 此值;null = 無指派(監督者)→ Mine 空、退回 All。
    # 不連 person_id —— 實測多數 assigned_person 對不上 contacts fullname,不可靠。
    emaint_assignee: Mapped[str | None] = mapped_column(String, nullable=True)


class UserSession(Base):
    __tablename__ = "user_session"

    # opaque token(secrets.token_urlsafe);cookie 存此、session 事實在 DB
    session_token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 即時撤銷(登出 / admin 撤):非空即失效
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UserExternalCredential(AuditMixin, Base):
    """per-user 外部系統憑證保管庫(ADR-022 §5;ADR-020 轉發用;初版 system='jira' PAT)。

    封套加密:`secret_ciphertext` = 主鑰(Fly secret,**不入庫**)加密後的 PAT;DB 只存密文。
    明文永不落 log / 稽核 / API 回應。可撤(`revoked_at` 即時);限本人取用。partial-unique
    (user_id, system) where revoked_at IS NULL → 一人一系統只一把現行憑證(換發前先撤)。
    """

    __tablename__ = "user_external_credential"
    __table_args__ = (
        Index(
            "uq_user_external_credential_active",
            "user_id",
            "system",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), nullable=False, index=True
    )
    system: Mapped[str] = mapped_column(String, nullable=False)  # 受控:jira
    secret_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet 密文(明文不入庫)
    key_version: Mapped[str] = mapped_column(String, nullable=False, server_default=text("'v1'"))
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class McpScopedToken(Base):
    """web session 衍生的 MCP scoped token(ADR-020 決策 5;MCP authN/authZ 的「最後一塊治理磚」)。

    opaque token(隨機不可猜)帶委派的 `human:<id>` + agent + scope;短命、可即時撤(revoked_at)。
    Hermes 以 agent:hermes 連 cmms MCP,但**必須攜此 token 帶入委派的人類身分**。mirror user_session
    (DB-backed、無簽章密鑰、查表驗證)。
    """

    __tablename__ = "mcp_scoped_token"

    token: Mapped[str] = mapped_column(String, primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user_account.user_id"), nullable=False, index=True
    )
    agent: Mapped[str] = mapped_column(String, nullable=False)  # agent:<name>(如 hermes)
    scope: Mapped[str] = mapped_column(String, nullable=False)  # 受控 scope 字串
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
