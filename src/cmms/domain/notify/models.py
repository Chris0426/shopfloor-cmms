"""notify ORM models(Slice B;對應 migration 0030 / 0032)。

- `NotifyRecipient`:通知收件人詞彙。**不綁 user_account** —— 線管理者無 cmms 帳號仍要收
  (Jordan 拍板)。email / telegram_chat_id 至少填一;`assignee_name` 精確比對
  `work_order.assigned_person` 供機台負責人「個人」通知;`notify_on_open` / `notify_on_close`
  為廣播旗標(工程團隊 / 線管理者群組)。admin 於 /admin/notify 維護。
- `NotifyWatch`(Slice D,0032):某收件人「關注」的負責人清單(一人多列)。工單負責人含被
  關注者時,該收件人於開單 AND 結案皆收到通知(主管 / 代班夥伴用)。assignee_name 精確比對
  `work_order_assignee.person_name`。
- `NotificationOutbox`:一列 = 一封待送通知(每 工單×事件×通道×收件人)。唯一鍵冪等 →
  同一組合只送一次(reopen→re-close 亦不重發;多規則命中同一人亦只一封)。逐列 flush
  (pending / failed<5 重試),未配置通道 → 略過(留 pending,不燒 attempts)。
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from cmms.audit import AuditMixin
from cmms.db import Base


class NotifyRecipient(AuditMixin, Base):
    __tablename__ = "notify_recipient"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    # 群組 chat_id 可為負數字串(bot 在群組)→ String,不存 Integer
    telegram_chat_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # 精確比對 work_order.assigned_person(legacy 確切字串,非 FK):機台負責人個人通知
    assignee_name: Mapped[str | None] = mapped_column(String, nullable=True)
    notify_on_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    notify_on_close: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class NotifyWatch(AuditMixin, Base):
    """某收件人關注的一位負責人(0032)。(recipient_id, assignee_name) 複合 PK,一人多列。

    工單負責人含 `assignee_name` 時,該收件人於開單/結案皆收通知(主管盯線 / 代班夥伴)。
    去重由 outbox 唯一鍵保證:命中多條規則(本人定向 / 關注 / 多負責人重疊)每人仍只一封。
    """

    __tablename__ = "notify_watch"
    __table_args__ = (Index("ix_notify_watch_assignee", "assignee_name"),)

    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("notify_recipient.id"), primary_key=True
    )
    # 精確比對 work_order_assignee.person_name(legacy 確切字串,非 FK)
    assignee_name: Mapped[str] = mapped_column(String, primary_key=True)


class NotificationOutbox(AuditMixin, Base):
    __tablename__ = "notification_outbox"
    __table_args__ = (
        Index(
            "uq_notification_outbox_key",
            "work_order_no",
            "event",
            "channel",
            "recipient_id",
            unique=True,
        ),
        Index("ix_notification_outbox_status", "status"),
        Index("ix_notification_outbox_wo", "work_order_no"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    work_order_no: Mapped[int] = mapped_column(
        ForeignKey("work_order.work_order_no"), nullable=False
    )
    event: Mapped[str] = mapped_column(String, nullable=False)  # opened | closed
    channel: Mapped[str] = mapped_column(String, nullable=False)  # email | telegram
    recipient_id: Mapped[int] = mapped_column(
        ForeignKey("notify_recipient.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'pending'")
    )  # pending | sent | failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_msg_id: Mapped[str | None] = mapped_column(String, nullable=True)
