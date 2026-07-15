"""CredentialVault DB 測試(ADR-022 §5;testcontainers)。無 Docker 自動 skip。

驗:封套加密(DB 存密文≠明文)、round-trip、限本人取用、撤銷後取回 None、換發撤舊、錯主鑰
解密失敗、無主鑰 fail-closed。identity 無跨切片 FK → create_all 只建 identity。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("testcontainers.postgres")
from cryptography.fernet import Fernet  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from testcontainers.postgres import PostgresContainer  # noqa: E402

from cmms.audit import Actor  # noqa: E402
from cmms.db import Base  # noqa: E402
from cmms.domain.identity import models as _identity_models  # noqa: E402, F401
from cmms.domain.identity import vault as vault_mod  # noqa: E402
from cmms.domain.identity.service import IdentityService  # noqa: E402
from cmms.domain.identity.vault import (  # noqa: E402
    CredentialVault,
    VaultError,
    VaultKeyInvalid,
    VaultKeyUnset,
)

KEY = Fernet.generate_key().decode()
BOB = Actor.human("bob")


@pytest.fixture
async def session(monkeypatch):
    monkeypatch.setattr(
        vault_mod, "get_settings", lambda: SimpleNamespace(credential_master_key=KEY)
    )
    with PostgresContainer("postgres:17") as pg:
        url = pg.get_connection_url().replace("+psycopg2", "+asyncpg")
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sm = async_sessionmaker(engine, expire_on_commit=False)
        async with sm() as s:
            await IdentityService(s).create_user(
                user_id="bob", username="bob", display_name="Bob", password="password8",
                org="plant", actor=Actor.human("cli"),
            )
            yield s
        await engine.dispose()


async def test_store_encrypts_and_roundtrips(session) -> None:
    v = CredentialVault(session)
    cid = await v.store_credential(
        user_id="bob", system="jira", secret="PAT-SECRET-123", actor=BOB, label="my jira"
    )
    row = (
        await session.execute(
            text("SELECT secret_ciphertext FROM user_external_credential WHERE id=:i"), {"i": cid}
        )
    ).one()
    assert "PAT-SECRET-123" not in row.secret_ciphertext  # DB 只存密文
    assert await v.get_plaintext(user_id="bob", system="jira", actor=BOB) == "PAT-SECRET-123"


async def test_get_plaintext_only_self(session) -> None:
    v = CredentialVault(session)
    await v.store_credential(user_id="bob", system="jira", secret="s" * 10, actor=BOB)
    with pytest.raises(VaultError):
        await v.get_plaintext(user_id="bob", system="jira", actor=Actor.human("mallory"))


async def test_revoke_then_none(session) -> None:
    v = CredentialVault(session)
    await v.store_credential(user_id="bob", system="jira", secret="s" * 10, actor=BOB)
    assert await v.revoke(user_id="bob", system="jira", actor=BOB) is True
    assert await v.get_plaintext(user_id="bob", system="jira", actor=BOB) is None
    assert await v.list_credentials("bob") == []


async def test_reissue_revokes_old(session) -> None:
    v = CredentialVault(session)
    await v.store_credential(user_id="bob", system="jira", secret="old-pat", actor=BOB)
    await v.store_credential(user_id="bob", system="jira", secret="new-pat", actor=BOB)
    assert await v.get_plaintext(user_id="bob", system="jira", actor=BOB) == "new-pat"
    assert len(await v.list_credentials("bob")) == 1  # 只一把現行(舊已撤)


async def test_wrong_master_key_decrypt_fails(session, monkeypatch) -> None:
    v = CredentialVault(session)
    await v.store_credential(user_id="bob", system="jira", secret="s" * 10, actor=BOB)
    monkeypatch.setattr(
        vault_mod,
        "get_settings",
        lambda: SimpleNamespace(credential_master_key=Fernet.generate_key().decode()),
    )
    with pytest.raises(VaultError):
        await v.get_plaintext(user_id="bob", system="jira", actor=BOB)


async def test_fail_closed_without_key(session, monkeypatch) -> None:
    v = CredentialVault(session)
    monkeypatch.setattr(
        vault_mod, "get_settings", lambda: SimpleNamespace(credential_master_key=None)
    )
    with pytest.raises(VaultKeyUnset):
        await v.store_credential(user_id="bob", system="jira", secret="s" * 10, actor=BOB)


async def test_invalid_key_raises_keyinvalid_without_leaking_value(session, monkeypatch) -> None:
    """主鑰有設但格式錯(誤用 token_urlsafe 產物)→ VaultKeyInvalid;訊息不含 key 值本身。"""
    import secrets

    bad_key = secrets.token_urlsafe(32)  # 43 字元、無 padding → 非合法 Fernet key
    monkeypatch.setattr(
        vault_mod, "get_settings", lambda: SimpleNamespace(credential_master_key=bad_key)
    )
    v = CredentialVault(session)
    with pytest.raises(VaultKeyInvalid) as ei:
        await v.store_credential(user_id="bob", system="jira", secret="s" * 10, actor=BOB)
    assert bad_key not in str(ei.value)  # 訊息絕不回顯 key 值
    assert isinstance(ei.value, VaultError)  # 仍是 VaultError 子類(呼叫端 fail-closed 相容)
