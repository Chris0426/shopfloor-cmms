"""CredentialVault — per-user 外部憑證(Jira PAT)封套加密保管(ADR-022 §5;唯一寫入路徑)。

主鑰(`CMMS_CREDENTIAL_MASTER_KEY`,Fernet key)**不入庫**;DB 只存密文。明文永不落 log / 稽核 /
API 回應。主鑰未設 → **fail-closed**(存/取皆 raise;無明文 fallback)。限本人取用
(`get_plaintext` 驗 user_id == actor)。
"""

from __future__ import annotations

import binascii
from datetime import UTC, datetime

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.base import DomainService
from cmms.domain.identity.models import UserExternalCredential


class VaultError(Exception):
    """憑證保管錯誤(主鑰未設 / 解密失敗 / 無權)。"""


class VaultKeyUnset(VaultError):
    """主鑰未設(CMMS_CREDENTIAL_MASTER_KEY)→ fail-closed(正式環境拒存/拒取)。"""


class VaultKeyInvalid(VaultError):
    """主鑰**有設但非合法 Fernet key**——需為 44 字元 urlsafe base64(結尾 `=`)。

    常見誤用 = `secrets.token_urlsafe(32)` 的產物(43 字元、無 padding),Fernet 建構會拒。
    正解:`cryptography.fernet.Fernet.generate_key()`。訊息**絕不含 key 值本身**。
    """


def _fernet() -> Fernet:
    key = get_settings().credential_master_key
    if not key:
        raise VaultKeyUnset("CMMS_CREDENTIAL_MASTER_KEY not set (vault fail-closed)")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, binascii.Error, TypeError) as e:
        # 格式無效(如誤用 token_urlsafe)。訊息只說格式與正確產生法,絕不回顯 key 值。
        raise VaultKeyInvalid(
            "CMMS_CREDENTIAL_MASTER_KEY is not a valid Fernet key "
            "(need 44-char urlsafe base64; generate with Fernet.generate_key())"
        ) from e


class CredentialVault(DomainService):
    async def _active(self, user_id: str, system: str) -> UserExternalCredential | None:
        return await self.session.scalar(
            select(UserExternalCredential).where(
                UserExternalCredential.user_id == user_id,
                UserExternalCredential.system == system,
                UserExternalCredential.revoked_at.is_(None),
            )
        )

    async def store_credential(
        self, *, user_id: str, system: str, secret: str, actor: Actor, label: str | None = None
    ) -> int:
        """存/換發一把憑證(封套加密)。先撤同 (user,system) 現行者,再存新的。回 credential id。

        明文只在記憶體加密、**絕不入庫/log**;`source_actor` 只記 who,不記值(ADR-022 §5)。
        """
        ciphertext = _fernet().encrypt(secret.encode()).decode()  # 明文→密文(明文不外流)
        async with self.write(actor):
            existing = await self._active(user_id, system)  # 換發:撤既有現行
            if existing is not None:
                existing.revoked_at = datetime.now(UTC)
                existing.updated_by = actor.value
            cred = UserExternalCredential(
                user_id=user_id,
                system=system,
                secret_ciphertext=ciphertext,
                key_version="v1",
                label=label,
                created_by=actor.value,
                source_actor=actor.value,
            )
            self.session.add(cred)
            await self.session.flush()
            cid = cred.id
        return cid

    async def get_plaintext(self, *, user_id: str, system: str, actor: Actor) -> str | None:
        """取本人現行憑證明文(**限本人**,ADR-022 §5)。無/已撤 → None;主鑰不符 → VaultError。

        解密在記憶體;呼叫端(轉發)用完即棄,絕不落庫/log。順手記 `last_used_at`。
        """
        if actor.value != Actor.human(user_id).value:
            raise VaultError("can only use your own credential")
        cred = await self._active(user_id, system)
        if cred is None:
            return None
        try:
            plaintext = _fernet().decrypt(cred.secret_ciphertext.encode()).decode()
        except InvalidToken as e:
            raise VaultError("decrypt failed (wrong master key?)") from e
        async with self.write(actor):
            cred.last_used_at = datetime.now(UTC)
        return plaintext

    async def list_credentials(self, user_id: str) -> list[UserExternalCredential]:
        """列本人現行憑證(**呼叫端只讀 metadata:system/label/last_used_at;不外洩密文/明文**)。"""
        stmt = (
            select(UserExternalCredential)
            .where(
                UserExternalCredential.user_id == user_id,
                UserExternalCredential.revoked_at.is_(None),
            )
            .order_by(UserExternalCredential.system)
        )
        return list((await self.session.scalars(stmt)).all())

    async def list_all_credentials(self) -> list[UserExternalCredential]:
        """列**全體使用者**的現行憑證 metadata(admin 總覽,ADR-022 §5)。

        **只讀 user_id/system/label/時間戳;密文與明文永不外洩**(不解密、不回 secret_ciphertext
        給呼叫端顯示)。呼叫端須先 require_admin。依 user_id、system 排序。
        """
        stmt = (
            select(UserExternalCredential)
            .where(UserExternalCredential.revoked_at.is_(None))
            .order_by(UserExternalCredential.user_id, UserExternalCredential.system)
        )
        return list((await self.session.scalars(stmt)).all())

    async def admin_revoke(self, credential_id: int, actor: Actor) -> bool:
        """admin 依 id 撤銷任一使用者的現行憑證(即時失效;冪等)。回是否有撤到。

        呼叫端須先 require_admin;`actor` = 執行的 admin(稽核記 who/when)。與 `revoke`
        (限本人 by user+system)區隔:此為治理面,可撤他人憑證。
        """
        async with self.write(actor):
            cred = await self.session.get(UserExternalCredential, credential_id)
            if cred is None or cred.revoked_at is not None:
                return False
            cred.revoked_at = datetime.now(UTC)
            cred.updated_by = actor.value
            cred.source_actor = actor.value
        return True

    async def revoke(self, *, user_id: str, system: str, actor: Actor) -> bool:
        """撤本人某系統的現行憑證(即時失效)。回是否有撤到。"""
        async with self.write(actor):
            cred = await self._active(user_id, system)
            if cred is None:
                return False
            cred.revoked_at = datetime.now(UTC)
            cred.updated_by = actor.value
        return True
