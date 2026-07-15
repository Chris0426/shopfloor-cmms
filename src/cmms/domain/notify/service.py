"""NotificationService — 工單 open/close 通知(email + Telegram)的領域服務(Slice B,唯一寫入路徑)。

三職責:
- 收件人 CRUD(admin-governed;`assert_active_admin`)。收件人**不綁 user_account**(線管理者
  無帳號仍可收);email / telegram_chat_id 至少填一。
- `enqueue_work_order_notifications`(模組級函式,供 WorkOrderService 在其交易內呼叫,避免循環
  import):依事件解析收件人(廣播旗標 OR assignee_name 精確比對)× 每個非空通道 → pg_insert
  on_conflict_do_nothing(冪等唯一鍵)。
- `flush_outbox`(mirror JiraSyncService.flush_outbox):逐列獨立 try、失敗誠實記錄;渲染於 flush
  時載入 WO + Asset;**未配置通道 → 整列略過(留 pending,不燒 attempts)**,回
  {sent, failed, skipped_unconfigured}。

護欄 #1(單一寫入路徑):enqueue / 狀態更新皆經 domain session;email/telegram 送出為外部副作用
(冪等由唯一鍵 + flush 前 status=sent 跳過保證)。通知內文固定繁中(render.py,Jordan 指定)。
"""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.asset.models import Asset
from cmms.domain.base import DomainService, clean_person_name
from cmms.domain.identity.service import assert_active_admin
from cmms.domain.notify.models import NotificationOutbox, NotifyRecipient, NotifyWatch
from cmms.domain.notify.render import render_message
from cmms.domain.work_order.models import WorkOrder, WorkOrderAssignee
from cmms.email import EmailError, EmailSender, get_email_sender, smtp_configured
from cmms.telegram import (
    TelegramError,
    TelegramSender,
    get_telegram_sender,
    telegram_configured,
)

_MAX_ATTEMPTS = 5  # failed 列重試上限(flush 只撿 attempts<此值 的 failed)
_EVENTS = ("opened", "closed")


class NotifyError(Exception):
    """通知收件人寫入 / 驗證錯誤(name 空 / 無通道 / email 形狀 / 找不到列)。"""


async def enqueue_work_order_notifications(session, wo: WorkOrder, event: str) -> int:
    """為一張工單的某事件,對所有相關收件人 × 每個非空通道各排一列 notification_outbox。

    在**呼叫端 write() 交易內**執行(不自開交易;模組級函式避免與 WorkOrderService 循環 import)。
    收件人 = active AND (該事件廣播旗標為真 OR 本人定向(assignee_name == 該工單任一負責人)
    OR 關注(notify_watch 有一列 assignee_name == 該工單任一負責人))。關注對開/結案皆無條件生效
    (Jordan:開單結單都要通知)。0031:負責人來源為 work_order_assignee 交叉表(全部負責人)。
    通道 = 收件人填了的 email / telegram_chat_id(各一列)。唯一鍵 (wo, event, channel, recipient)
    → on_conflict_do_nothing(冪等,reopen→re-close 亦不重發;多規則命中同一人仍只一列)。
    零收件人 → 零列。回實際新排列數。
    """
    if event not in _EVENTS:
        raise ValueError(f"unknown notify event: {event!r}")
    flag = NotifyRecipient.notify_on_open if event == "opened" else NotifyRecipient.notify_on_close
    conds = [flag.is_(True)]
    # 機台負責人個人通知:精確比對該工單全部負責人(0031 多負責人;於呼叫端交易內載入)。
    assignee_names = [
        r[0]
        for r in (
            await session.execute(
                select(WorkOrderAssignee.person_name).where(
                    WorkOrderAssignee.work_order_no == wo.work_order_no
                )
            )
        ).all()
    ]
    if assignee_names:
        # ① 本人定向:收件人自身 assignee_name 命中該工單負責人。
        conds.append(
            and_(
                NotifyRecipient.assignee_name.is_not(None),
                NotifyRecipient.assignee_name.in_(assignee_names),
            )
        )
        # ② 關注:收件人有一列 notify_watch 指向該工單任一負責人(開/結案皆通知)。
        conds.append(
            select(NotifyWatch.recipient_id)
            .where(
                NotifyWatch.recipient_id == NotifyRecipient.id,
                NotifyWatch.assignee_name.in_(assignee_names),
            )
            .exists()
        )
    recipients = (
        await session.scalars(
            select(NotifyRecipient).where(
                NotifyRecipient.is_active.is_(True), or_(*conds)
            )
        )
    ).all()
    enqueued = 0
    for r in recipients:
        channels: list[str] = []
        if (r.email or "").strip():
            channels.append("email")
        if (r.telegram_chat_id or "").strip():
            channels.append("telegram")
        for channel in channels:
            result = await session.execute(
                pg_insert(NotificationOutbox)
                .values(
                    work_order_no=wo.work_order_no,
                    event=event,
                    channel=channel,
                    recipient_id=r.id,
                    status="pending",
                    created_by=wo.source_actor,
                    source_actor=wo.source_actor,
                )
                .on_conflict_do_nothing(
                    index_elements=["work_order_no", "event", "channel", "recipient_id"]
                )
            )
            enqueued += result.rowcount or 0
    return enqueued


class NotificationService(DomainService):
    def __init__(
        self,
        session,
        *,
        email_sender: EmailSender | None = None,
        telegram_sender: TelegramSender | None = None,
    ) -> None:
        super().__init__(session)
        # 測試注入 InMemory sender(視為該通道「已配置」,不看 config);prod 缺省 → 依 config
        # 決定是否配置 + 用 get_*_sender()。
        self._email_sender = email_sender
        self._telegram_sender = telegram_sender
        self._email_injected = email_sender is not None
        self._telegram_injected = telegram_sender is not None

    # ---- 收件人 CRUD(admin-governed;RBAC 在 domain 強制)----

    async def list_recipients(self) -> list[NotifyRecipient]:
        """列所有收件人(讀取,開放;admin 頁 require_admin 把關)。依 name 排序。

        每列透明附掛 `.watches`(list[str],依 assignee_name)供顯示/稽核(0032;一查免 N+1)。
        """
        stmt = select(NotifyRecipient).order_by(NotifyRecipient.name, NotifyRecipient.id)
        recipients = list((await self.session.scalars(stmt)).all())
        wmap = await self._watches_map([r.id for r in recipients])
        for r in recipients:
            r.watches = wmap.get(r.id, [])  # 顯示層透明附掛(非 mapped 欄)
        return recipients

    async def _watches_map(self, recipient_ids: list[int]) -> dict[int, list[str]]:
        """批次取多收件人的關注清單(recipient_id → [assignee_name],依名),一查免 N+1。"""
        ids = [i for i in dict.fromkeys(recipient_ids) if i is not None]
        if not ids:
            return {}
        rows = await self.session.execute(
            select(NotifyWatch.recipient_id, NotifyWatch.assignee_name)
            .where(NotifyWatch.recipient_id.in_(ids))
            .order_by(NotifyWatch.recipient_id, NotifyWatch.assignee_name)
        )
        out: dict[int, list[str]] = {}
        for rid, name in rows:
            out.setdefault(rid, []).append(name)
        return out

    async def _replace_watches(
        self, recipient_id: int, assignees: list[str], *, self_assignee: str | None, actor: Actor
    ) -> None:
        """在呼叫端 write() 交易內,把一收件人的關注清單整組替換為 `assignees`(REPLACE 語意)。

        逐名 `clean_person_name` 正規化、去空、**去重保序**;關注自身 assignee_name(= 本人定向已
        涵蓋)靜默丟棄(冗餘無害)。差異更新:刪不在新清單的舊列、插新列。不自開交易 / 不驗 admin。
        """
        desired: list[str] = []
        seen: set[str] = set()
        for raw in assignees:
            name = clean_person_name(raw)
            # 關注自己 = 本人定向已涵蓋,靜默丟棄(冗餘無害)。
            if not name or name == self_assignee or name in seen:
                continue
            seen.add(name)
            desired.append(name)
        existing = {
            r.assignee_name: r
            for r in (
                await self.session.scalars(
                    select(NotifyWatch).where(NotifyWatch.recipient_id == recipient_id)
                )
            ).all()
        }
        for name, row in existing.items():
            if name not in seen:
                await self.session.delete(row)
        for name in desired:
            if name not in existing:
                self.session.add(
                    NotifyWatch(
                        recipient_id=recipient_id,
                        assignee_name=name,
                        created_by=actor.value,
                        source_actor=actor.value,
                    )
                )

    @staticmethod
    def _clean(value: str | None) -> str | None:
        v = (value or "").strip()
        return v or None

    def _validate(
        self, name: str | None, email: str | None, telegram_chat_id: str | None
    ) -> tuple[str, str | None, str | None]:
        """驗 name 必填 + 至少一通道 + email 輕量形狀檢查(含 '@')。回清理後 (name, email, tg)。"""
        name = self._clean(name)
        email = self._clean(email)
        tg = self._clean(telegram_chat_id)
        if not name:
            raise NotifyError("recipient name is required")
        if not email and not tg:
            raise NotifyError("at least one of email / telegram_chat_id is required")
        if email and "@" not in email:
            raise NotifyError(f"invalid email: {email!r}")
        return name, email, tg

    async def create_recipient(
        self,
        *,
        name: str,
        actor: Actor,
        email: str | None = None,
        telegram_chat_id: str | None = None,
        assignee_name: str | None = None,
        notify_on_open: bool = False,
        notify_on_close: bool = False,
        watch_assignees: list[str] | None = None,
    ) -> NotifyRecipient:
        await assert_active_admin(self.session, actor)
        name, email, tg = self._validate(name, email, telegram_chat_id)
        clean_assignee = self._clean(assignee_name)
        row = NotifyRecipient(
            name=name,
            email=email,
            telegram_chat_id=tg,
            assignee_name=clean_assignee,
            notify_on_open=notify_on_open,
            notify_on_close=notify_on_close,
            is_active=True,
            created_by=actor.value,
            source_actor=actor.value,
        )
        async with self.write(actor):
            self.session.add(row)
            await self.session.flush()
            rid = row.id
            await self._replace_watches(
                rid, watch_assignees or [], self_assignee=clean_assignee, actor=actor
            )
        return await self.session.get(NotifyRecipient, rid)

    async def update_recipient(
        self,
        recipient_id: int,
        *,
        actor: Actor,
        name: str,
        email: str | None = None,
        telegram_chat_id: str | None = None,
        assignee_name: str | None = None,
        notify_on_open: bool = False,
        notify_on_close: bool = False,
        watch_assignees: list[str] | None = None,
    ) -> NotifyRecipient:
        await assert_active_admin(self.session, actor)
        row = await self.session.get(NotifyRecipient, recipient_id)
        if row is None:
            raise NotifyError(f"recipient {recipient_id} not found")
        name, email, tg = self._validate(name, email, telegram_chat_id)
        clean_assignee = self._clean(assignee_name)
        async with self.write(actor):
            row.name = name
            row.email = email
            row.telegram_chat_id = tg
            row.assignee_name = clean_assignee
            row.notify_on_open = notify_on_open
            row.notify_on_close = notify_on_close
            row.updated_by = actor.value
            row.source_actor = actor.value
            # REPLACE 語意:表單一律送完整清單(空欄 = 清空)。
            await self._replace_watches(
                recipient_id, watch_assignees or [], self_assignee=clean_assignee, actor=actor
            )
        return row

    async def set_recipient_active(
        self, recipient_id: int, active: bool, *, actor: Actor
    ) -> NotifyRecipient:
        await assert_active_admin(self.session, actor)
        row = await self.session.get(NotifyRecipient, recipient_id)
        if row is None:
            raise NotifyError(f"recipient {recipient_id} not found")
        async with self.write(actor):
            row.is_active = active
            row.updated_by = actor.value
            row.source_actor = actor.value
        return row

    async def fill_telegram_chat_id(
        self, *, assignee_name: str | None, chat_id: str, actor: Actor
    ) -> bool:
        """自助綁定回填:把 telegram_chat_id 空缺、且 assignee_name 精確命中的收件人填上 chat_id。

        續-15:工程師對 bot 完成綁定後,以其 emaint_assignee 精確比對 `NotifyRecipient.assignee_name`
        (`clean_person_name` 正規化)且 `telegram_chat_id IS NULL` 的列 → 填 chat_id。**有值不覆蓋**
        (WHERE 保證只碰空欄)。**不驗 admin**:本人 actor 的自助路徑,風險面 = 零覆蓋(只填空欄、
        不能改任何既有值)。assignee_name None / 空 → False(不查)。回是否有填。
        """
        name = clean_person_name(assignee_name)
        if not name:
            return False
        rows = (
            await self.session.scalars(
                select(NotifyRecipient).where(
                    NotifyRecipient.assignee_name == name,
                    NotifyRecipient.telegram_chat_id.is_(None),
                )
            )
        ).all()
        if not rows:
            return False
        async with self.write(actor):
            for r in rows:
                r.telegram_chat_id = chat_id
                r.updated_by = actor.value
                r.source_actor = actor.value
        return True

    # ---- outbox 讀取(admin 頁近期列)----

    async def list_recent_outbox(self, limit: int = 20) -> list[NotificationOutbox]:
        stmt = select(NotificationOutbox).order_by(NotificationOutbox.id.desc()).limit(limit)
        return list((await self.session.scalars(stmt)).all())

    async def count_failed(self) -> int:
        n = await self.session.scalar(
            select(func.count())
            .select_from(NotificationOutbox)
            .where(NotificationOutbox.status == "failed")
        )
        return n or 0

    # ---- flush(送 email / telegram;逐列獨立)----

    def _email_from(self) -> str | None:
        s = get_settings()
        return s.notify_from or s.rfq_from

    def _email_available(self) -> bool:
        """email 通道是否可送:注入 sender(測試)→ 只需 from 位址;否則需 SMTP 三鍵 + from。"""
        if not self._email_from():
            return False
        return True if self._email_injected else smtp_configured()

    def _telegram_available(self) -> bool:
        return True if self._telegram_injected else telegram_configured()

    async def _mark(
        self,
        ob: NotificationOutbox,
        *,
        status: str,
        actor: Actor,
        error: str | None = None,
        provider_msg_id: str | None = None,
    ) -> None:
        async with self.write(actor):
            ob.status = status
            if status == "failed":
                ob.attempts = (ob.attempts or 0) + 1
                ob.last_error = (error or "")[:500]
            if provider_msg_id is not None:
                ob.provider_msg_id = provider_msg_id
                ob.last_error = None
            ob.updated_by = actor.value
            ob.source_actor = actor.value

    async def flush_outbox(self, *, actor: Actor | None = None, limit: int = 50) -> dict:
        """送出待處理通知(pending + failed 且 attempts<5)。逐列獨立 try、失敗誠實記錄。

        渲染於 flush 時進行(載入 WO + Asset)。**未配置通道 → 整列略過**(不改狀態、不燒 attempts,
        待配置後補送)。回 {sent, failed, skipped_unconfigured}。狀態更新走單一寫入路徑;
        email/telegram 送出為外部副作用。
        """
        who = actor or Actor.scheduler()
        stmt = (
            select(NotificationOutbox)
            .where(
                or_(
                    NotificationOutbox.status == "pending",
                    and_(
                        NotificationOutbox.status == "failed",
                        NotificationOutbox.attempts < _MAX_ATTEMPTS,
                    ),
                )
            )
            .order_by(NotificationOutbox.id)
            .limit(limit)
        )
        rows = list((await self.session.scalars(stmt)).all())
        base_url = get_settings().public_base_url
        email_ok = self._email_available()
        tg_ok = self._telegram_available()
        sent = failed = skipped = 0

        for ob in rows:
            # 未配置通道 → 略過(留 pending,不燒 attempts;配置後 flush 補送)
            if ob.channel == "email" and not email_ok:
                skipped += 1
                continue
            if ob.channel == "telegram" and not tg_ok:
                skipped += 1
                continue
            wo = await self.session.get(WorkOrder, ob.work_order_no)
            if wo is None:  # 理論上 FK 保證存在;誠實 failed
                await self._mark(ob, status="failed", actor=who, error="work order not found")
                failed += 1
                continue
            recipient = await self.session.get(NotifyRecipient, ob.recipient_id)
            if recipient is None:
                await self._mark(ob, status="failed", actor=who, error="recipient not found")
                failed += 1
                continue
            asset = await self.session.get(Asset, wo.asset_id)
            assignees = [
                r[0]
                for r in (
                    await self.session.execute(
                        select(WorkOrderAssignee.person_name)
                        .where(WorkOrderAssignee.work_order_no == wo.work_order_no)
                        .order_by(WorkOrderAssignee.position, WorkOrderAssignee.person_name)
                    )
                ).all()
            ]
            msg = render_message(ob.event, wo, asset, base_url=base_url, assignees=assignees)
            try:
                if ob.channel == "email":
                    pid = await self._send_email(recipient, msg)
                else:
                    pid = await self._send_telegram(recipient, msg)
            except (EmailError, TelegramError) as exc:
                await self._mark(ob, status="failed", actor=who, error=str(exc))
                failed += 1
                continue
            await self._mark(ob, status="sent", actor=who, provider_msg_id=pid)
            sent += 1

        return {"sent": sent, "failed": failed, "skipped_unconfigured": skipped}

    async def _send_email(self, recipient: NotifyRecipient, msg) -> str:
        sender = self._email_sender or get_email_sender()
        return await sender.send(
            to=recipient.email or "",
            subject=msg.subject,
            body=msg.body,
            from_addr=self._email_from() or "",
        )

    async def _send_telegram(self, recipient: NotifyRecipient, msg) -> str:
        sender = self._telegram_sender or get_telegram_sender()
        return await sender.send(
            chat_id=recipient.telegram_chat_id or "", text=msg.telegram_text
        )
