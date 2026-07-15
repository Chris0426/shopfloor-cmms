"""EmailSender — RFQ 詢價信寄送的 adapter port(ADR-026;mirror storage.py 可插拔)。

三者齊備(host+username+password)→ 真 `SmtpEmailSender`(stdlib smtplib,無新依賴);否則
`get_email_sender()` 退回 `InMemoryEmailSender`(dev/CI 友善,不真發、可檢視)。live 發送 = blocked
(待 workspace 郵件帳號 app-password + Fly secret,見 secrets-manifest)。
RFQ 落庫走 domain service;此檔只管「送」。
"""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage as _StdlibEmailMessage
from typing import Protocol, runtime_checkable

from cmms.config import get_settings


class EmailError(Exception):
    """寄信失敗(SMTP 連線/認證/送出錯誤)。"""


@runtime_checkable
class EmailSender(Protocol):
    async def send(
        self, *, to: str, subject: str, body: str, from_addr: str, reply_to: str | None = None
    ) -> str:
        """送一封純文字信,回 provider message id。"""
        ...


class NullEmailSender:
    """no-op(不送、不記;完全靜默)。"""

    async def send(
        self, *, to: str, subject: str, body: str, from_addr: str, reply_to: str | None = None
    ) -> str:
        return "null-msg"


class InMemoryEmailSender:
    """記憶體 fake(dev/測試):把信收進 `sent`,不真發。可檢視內容。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, str | None]] = []

    async def send(
        self, *, to: str, subject: str, body: str, from_addr: str, reply_to: str | None = None
    ) -> str:
        self.sent.append(
            {"to": to, "subject": subject, "body": body, "from": from_addr, "reply_to": reply_to}
        )
        return f"mem-{len(self.sent)}"


class SmtpEmailSender:
    """真 SMTP(implicit TLS / SMTP_SSL)。stdlib smtplib 於 asyncio.to_thread(不阻塞事件迴圈)。"""

    async def send(
        self, *, to: str, subject: str, body: str, from_addr: str, reply_to: str | None = None
    ) -> str:
        return await asyncio.to_thread(
            self._send_sync, to=to, subject=subject, body=body, from_addr=from_addr,
            reply_to=reply_to,
        )

    @staticmethod
    def _send_sync(
        *, to: str, subject: str, body: str, from_addr: str, reply_to: str | None
    ) -> str:
        s = get_settings()
        msg = _StdlibEmailMessage()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        if reply_to:
            msg["Reply-To"] = reply_to
        msg.set_content(body)
        try:
            with smtplib.SMTP_SSL(s.smtp_host or "", s.smtp_port) as smtp:
                if s.smtp_username and s.smtp_password:
                    smtp.login(s.smtp_username, s.smtp_password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as e:
            raise EmailError(str(e)) from e
        return msg.get("Message-ID") or "sent"


_sender: EmailSender | None = None


def smtp_configured() -> bool:
    """SMTP 三鍵(host+username+password)是否齊備 = 能真發信。

    web 一鍵 RFQ 據此誠實降級:未設 → create_rfq(dry_run=True) 只落 drafted、UI 提示未配置,
    不讓 InMemory fallback 假裝 sent(ADR-026)。
    """
    s = get_settings()
    return bool(s.smtp_host and s.smtp_username and s.smtp_password)


def get_email_sender() -> EmailSender:
    """回目前 email sender。SMTP 三鍵齊備 → SmtpEmailSender;否則 InMemory(fallback)。"""
    global _sender
    if _sender is None:
        s = get_settings()
        if s.smtp_host and s.smtp_username and s.smtp_password:
            _sender = SmtpEmailSender()
        else:
            _sender = InMemoryEmailSender()
    return _sender
