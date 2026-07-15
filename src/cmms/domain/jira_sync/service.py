"""JiraSyncService — 工單→Jira MRQ 轉發 + note 自動同步的執行層(ADR-020 決策 1 修訂,2026-07-06)。

cmms 直呼 Jira REST(`HttpJiraForwarder`),用**連結建立者**的 per-user PAT(ADR-022 vault)。兩職責:
- `forward_work_orders_to_mrq`:初次把多張工單綜合成**一張 MRQ**(summary/description 由呼叫端/Hermes
  生成)+ 所有工單的全部 note 跨工單按 occurred_at **全域時序**逐則 comment。
- `flush_outbox`:送出佇列中的 note→comment(`add_note` enqueue 或 forward 排入的);逐列獨立、
  失敗誠實記錄(status/attempts/last_error),CLI `jira-flush-outbox` 兜底重試。

護欄 #1(單一寫入路徑):record link / 排 outbox / 更新 outbox 狀態皆經 domain session;Jira REST
呼叫為外部副作用(冪等由 outbox 保證:唯一鍵 (note_id, external_key) + flush 前 status=sent 跳過)。
comment 內文 = note **原文忠實不翻譯**(自動同步無 LLM,翻了會前後語言錯亂;原文留 SoR,ADR-023)。
更正 / 軟刪 note **不回寫**(v1 只同步新增;軟刪 note 於 flush 標 note-deleted 不送)。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from cmms.audit import Actor
from cmms.config import get_settings
from cmms.domain.attachment.models import Attachment
from cmms.domain.base import DomainService
from cmms.domain.identity.vault import (
    CredentialVault,
    VaultError,
    VaultKeyInvalid,
    VaultKeyUnset,
)
from cmms.domain.work_order.models import (
    JiraOutbox,
    WorkOrder,
    WorkOrderExternalLink,
    WorkOrderNote,
)
from cmms.domain.work_order.service import WorkOrderService
from cmms.domain.work_order.transform import to_taipei_naive
from cmms.jira_forwarder import JiraForwarder, JiraForwardError, build_jira_forwarder
from cmms.storage import StorageBackend, StorageObjectNotFound, get_storage_backend

_MAX_ATTEMPTS = 5  # failed 列重試上限(flush 只撿 attempts<此值 的 failed)
ForwarderFactory = Callable[[str], JiraForwarder | None]


class JiraSyncError(Exception):
    """轉發前置驗證失敗(工單缺 / summary 空 / config 或 PAT 未備)。"""


@dataclass(frozen=True, slots=True)
class ForwardWoSummary:
    work_order_no: int
    note_count: int
    photo_count: int = 0  # 該工單全部未軟刪 note 附帶的照片總數(dry-run 預覽)


@dataclass(frozen=True, slots=True)
class FlushResult:
    processed: int
    sent: int
    failed: int
    errors: list[str] = field(default_factory=list)  # 截斷診斷(不含 PAT)


@dataclass(frozen=True, slots=True)
class ForwardResult:
    dry_run: bool
    external_key: str | None  # dry_run → None(尚未建);already_forwarded → 既有 key
    work_orders: list[ForwardWoSummary]
    total_comments: int
    total_photos: int
    summary: str
    description: str
    pat_ready: bool
    config_ready: bool
    already_forwarded: bool = False
    warnings: list[str] = field(default_factory=list)
    flush: FlushResult | None = None

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "external_key": self.external_key,
            "work_orders": [
                {
                    "work_order_no": w.work_order_no,
                    "note_count": w.note_count,
                    "photo_count": w.photo_count,
                }
                for w in self.work_orders
            ],
            "total_comments": self.total_comments,
            "total_photos": self.total_photos,
            "summary": self.summary,
            "description": self.description,
            "pat_ready": self.pat_ready,
            "config_ready": self.config_ready,
            "already_forwarded": self.already_forwarded,
            "warnings": self.warnings,
            "flush": (
                None
                if self.flush is None
                else {
                    "processed": self.flush.processed,
                    "sent": self.flush.sent,
                    "failed": self.flush.failed,
                    "errors": self.flush.errors,
                }
            ),
        }


def _display_author(actor_value: str | None) -> str:
    """`human:jordan.lee` / `agent:hermes` → `jordan.lee` / `hermes`(comment 標頭可讀)。"""
    if actor_value and ":" in actor_value:
        return actor_value.split(":", 1)[1]
    return actor_value or "?"


def _attachment_filename(note: WorkOrderNote, att: Attachment) -> str:
    """送 Jira 的附件檔名(防碰撞):`wo<no>-note<id>-<原檔名>`。

    同一 MRQ 下多筆 note 可能有同名照片(如相機預設 IMG_0001.jpg)—— 前綴 wo/note 避免互蓋。
    無 original_filename → 退回 `att<id>`(仍可辨識、不碰撞)。
    """
    base = att.original_filename or f"att{att.id}"
    return f"wo{note.work_order_no}-note{note.id}-{base}"


def _comment_body(note: WorkOrderNote, photo_names: list[str] | None = None) -> str:
    """comment 模板:`[WO <no> · <台北 YYYY-MM-DD HH:MM> · <author>]\\n<原文>`(原文忠實不翻譯)。

    有照片時,在原文後空一行,每張一行 Jira wiki 內嵌 `!<檔名>|thumbnail!`(DC comment 顯示
    **縮圖**、點擊看原圖——避免大圖洗版),末行 `(photos: N)` 中性標注(comment 標頭已是中性
    格式,故英文即可、不 i18n)。
    """
    ts = to_taipei_naive(note.occurred_at).strftime("%Y-%m-%d %H:%M")
    header = f"[WO {note.work_order_no} · {ts} · {_display_author(note.author)}]"
    body = f"{header}\n{note.body}"
    if photo_names:
        embeds = "\n".join(f"!{name}|thumbnail!" for name in photo_names)
        body = f"{body}\n\n{embeds}\n(photos: {len(photo_names)})"
    return body


class JiraSyncService(DomainService):
    def __init__(
        self,
        session,
        *,
        forwarder_factory: ForwarderFactory | None = None,
        storage: StorageBackend | None = None,
    ) -> None:
        super().__init__(session)
        # per-user PAT → 每次轉發依 PAT 建 forwarder;測試注入 fake factory(回同一 InMemory 實例)
        self._forwarder_factory: ForwarderFactory = forwarder_factory or build_jira_forwarder
        # 照片同步:從 R2 下載 note 附件 bytes → 上傳 MRQ;測試注入 InMemory backend
        self._storage: StorageBackend = storage or get_storage_backend()

    # ---- readiness / PAT 取用 ----

    async def pat_ready(self, user_id: str) -> bool:
        """公開 readiness 查詢(web 連結同步勾選用):同 `_pat_ready` 實作、不解密、不記 last_used。"""
        return await self._pat_ready(user_id)

    async def _pat_ready(self, user_id: str) -> bool:
        """dry-run 誠實回報:該 user 有現行 jira PAT 且主鑰已設(不解密、不記 last_used)。"""
        if not get_settings().credential_master_key:
            return False
        creds = await CredentialVault(self.session).list_credentials(user_id)
        return any(c.system == "jira" for c in creds)

    async def _get_pat(self, user_id: str) -> str | None:
        """取 user 的 jira PAT 明文(用完即棄);無現行憑證 → None;主鑰未設 → VaultKeyUnset。"""
        return await CredentialVault(self.session).get_plaintext(
            user_id=user_id, system="jira", actor=Actor.human(user_id)
        )

    # ---- flush(送 comment;逐列獨立)----

    async def _mark(
        self, ob: JiraOutbox, *, status: str, actor: Actor,
        error: str | None = None, comment_id: str | None = None,
    ) -> None:
        async with self.write(actor):
            ob.status = status
            if status == "failed":
                ob.attempts = (ob.attempts or 0) + 1
                ob.last_error = (error or "")[:500]
            if comment_id is not None:
                ob.sent_comment_id = comment_id
                ob.last_error = None
            ob.updated_by = actor.value
            ob.source_actor = actor.value

    async def _mark_attachments_uploaded(self, ob: JiraOutbox, *, actor: Actor) -> None:
        """附件全數上傳成功後落定旗標(單一寫入路徑);此後重試只重送 comment、不重上附件。"""
        async with self.write(actor):
            ob.attachments_uploaded = True
            ob.updated_by = actor.value
            ob.source_actor = actor.value

    async def _note_attachments(self, note_ids: list[int]) -> dict[int, list[Attachment]]:
        """批次取一群 note 的未軟刪附件(owner_type='work_order_note'),依上傳序。

        回 {note_id: [att…]}。"""
        if not note_ids:
            return {}
        stmt = (
            select(Attachment)
            .where(
                Attachment.owner_type == "work_order_note",
                Attachment.owner_id.in_([str(n) for n in note_ids]),
                Attachment.is_deleted.is_(False),
            )
            .order_by(Attachment.owner_id, Attachment.id)
        )
        out: dict[int, list[Attachment]] = {}
        for att in (await self.session.scalars(stmt)).all():
            out.setdefault(int(att.owner_id), []).append(att)
        return out

    async def flush_outbox(
        self, *, limit: int = 50, actor: Actor | None = None
    ) -> FlushResult:
        """送出待處理的 note→comment(pending + failed 且 attempts<5)。逐列獨立 try、失敗誠實。

        依 (external_key, note.occurred_at) 排序 → 同一 MRQ 的 comment 按時序追加。每 user 的 PAT
        本批快取一次(避免重複解密)。狀態更新走單一寫入路徑;Jira 呼叫為外部副作用。

        照片:送 comment 前先把該 note 的未軟刪附件逐張上傳到 MRQ,全部成功才落定
        `attachments_uploaded`,最後才送 comment。**v1 取捨**:上傳到一半某張失敗 → 旗標不落,
        重試會重上已成功那幾張 = Jira 可能出現重複附件;取「寧可重複不可漏」。附件下載失敗
        (R2 缺物件)→ 該列誠實 failed,不假成功。
        """
        who = actor or Actor.scheduler()
        stmt = (
            select(JiraOutbox, WorkOrderNote)
            .join(WorkOrderNote, WorkOrderNote.id == JiraOutbox.note_id)
            .where(
                or_(
                    JiraOutbox.status == "pending",
                    and_(JiraOutbox.status == "failed", JiraOutbox.attempts < _MAX_ATTEMPTS),
                )
            )
            .order_by(JiraOutbox.external_key, WorkOrderNote.occurred_at, WorkOrderNote.id)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).all())
        att_map = await self._note_attachments([note.id for _ob, note in rows])
        processed = sent = failed = 0
        errors: list[str] = []
        pat_cache: dict[str, str | None] = {}

        def _err(ob: JiraOutbox, reason: str) -> None:
            errors.append(f"outbox {ob.id} (note {ob.note_id}→{ob.external_key}): {reason}")

        for ob, note in rows:
            processed += 1
            if ob.status == "sent":  # 防禦(理論上查詢集已排除)
                continue
            if note.deleted_at is not None:  # v1 只同步新增:軟刪 note 不送
                await self._mark(ob, status="failed", actor=who, error="note-deleted")
                failed += 1
                _err(ob, "note-deleted")
                continue
            # 取 PAT(每 user 快取一次;主鑰未設 / 無 PAT / 解密失敗 → 誠實 failed)
            user = ob.on_behalf_user
            if user not in pat_cache:
                try:
                    pat_cache[user] = await self._get_pat(user)
                except VaultKeyUnset:
                    pat_cache[user] = None
                    await self._mark(ob, status="failed", actor=who, error="master-key-unset")
                    failed += 1
                    _err(ob, "master-key-unset")
                    continue
                except VaultKeyInvalid:
                    pat_cache[user] = None
                    await self._mark(ob, status="failed", actor=who, error="master-key-invalid")
                    failed += 1
                    _err(ob, "master-key-invalid")
                    continue
                except VaultError as exc:
                    pat_cache[user] = None
                    await self._mark(ob, status="failed", actor=who, error=f"vault: {exc}")
                    failed += 1
                    _err(ob, "vault-error")
                    continue
            pat = pat_cache[user]
            if pat is None:
                await self._mark(ob, status="failed", actor=who, error="pat-missing")
                failed += 1
                _err(ob, "pat-missing")
                continue
            forwarder = self._forwarder_factory(pat)
            if forwarder is None:
                await self._mark(ob, status="failed", actor=who, error="config-missing")
                failed += 1
                _err(ob, "config-missing")
                continue
            # 附件同步:送 comment 前先把該 note 未軟刪照片上傳 MRQ(attachments_uploaded 防重)。
            atts = att_map.get(note.id, [])
            photo_names: list[str] = []
            if atts and not ob.attachments_uploaded:
                try:
                    for att in atts:
                        data = await self._storage.get_object(
                            bucket=att.r2_bucket, key=att.r2_key
                        )
                        stored = await forwarder.upload_attachment(
                            external_key=ob.external_key,
                            filename=_attachment_filename(note, att),
                            data=data,
                            content_type=att.content_type,
                        )
                        photo_names.append(stored)
                except (StorageObjectNotFound, JiraForwardError) as exc:
                    # 任一張失敗 → 該列 failed、旗標不落。重試會重上已成功那幾張(v1 取捨:
                    # 寧可 Jira 出現重複附件也不漏;見類別 docstring)。
                    await self._mark(ob, status="failed", actor=who, error=f"attachment: {exc}")
                    failed += 1
                    _err(ob, f"attachment: {exc}")
                    continue
                await self._mark_attachments_uploaded(ob, actor=who)
            elif atts:
                # 旗標已 true(前次已全數上傳、僅 comment 未成)→ 重建內嵌名、只重送 comment。
                photo_names = [_attachment_filename(note, att) for att in atts]
            try:
                cid = await forwarder.append_mrq_comment(
                    external_key=ob.external_key,
                    body=_comment_body(note, photo_names or None),
                    idempotency_key=f"note:{note.id}",
                )
            except JiraForwardError as exc:
                await self._mark(ob, status="failed", actor=who, error=str(exc))
                failed += 1
                _err(ob, f"jira: {exc}")
                continue
            await self._mark(ob, status="sent", actor=who, comment_id=cid)
            sent += 1
        return FlushResult(processed=processed, sent=sent, failed=failed, errors=errors[:20])

    # ---- 批次 forward(初次開 MRQ + 逐則 comment)----

    async def forward_work_orders_to_mrq(
        self,
        *,
        work_order_nos: list[int],
        summary: str,
        description: str,
        acting_user: str,
        actor: Actor,
        dry_run: bool = True,
        idempotency_key: str | None = None,
    ) -> ForwardResult:
        """把多張工單綜合成一張 MRQ + 全域時序逐則 comment(ADR-020 決策 1/7)。

        `acting_user` = PAT 主人(bare user_id;其 PAT 寫 Jira,Jira 端原生歸屬本人)。`actor` = 稽核
        發起者(如 agent:hermes)。dry_run=True → **零寫入**,回預覽(工單/note 數 + readiness + 警語)。

        **create 防重**(Jira REST 無原生冪等):提供 `idempotency_key` 時,先查是否已有帶同
        forward_idem_key 的 forwarded link → 有則**復用既有 external_key、不重開 issue**
        (只 re-flush pending)。未提供 key → 不防重(雙擊可能重開;呼叫端〔MCP〕應帶 key)。

        執行(dry_run=False):create MRQ → 對每工單 record forwarded link(帶 forward_idem_key)→ 對
        每筆未軟刪 note 排 outbox(pending,occurred_at 序)→ 立即 flush(comment 按全域時序送)。
        """
        nos = list(dict.fromkeys(int(n) for n in work_order_nos))  # 去重、保序
        if not nos:
            raise JiraSyncError("no work orders given")
        if not summary.strip():
            raise JiraSyncError("summary is required")
        if not description.strip():
            raise JiraSyncError("description is required")
        missing = [n for n in nos if await self.session.get(WorkOrder, n) is None]
        if missing:
            raise JiraSyncError(f"work orders not found: {missing}")

        notes = list(
            (
                await self.session.scalars(
                    select(WorkOrderNote)
                    .where(
                        WorkOrderNote.work_order_no.in_(nos),
                        WorkOrderNote.deleted_at.is_(None),
                    )
                    .order_by(WorkOrderNote.occurred_at, WorkOrderNote.id)
                )
            ).all()
        )
        per_wo: dict[int, int] = dict.fromkeys(nos, 0)
        per_wo_photos: dict[int, int] = dict.fromkeys(nos, 0)
        att_map = await self._note_attachments([note.id for note in notes])
        for note in notes:
            per_wo[note.work_order_no] += 1
            per_wo_photos[note.work_order_no] += len(att_map.get(note.id, []))
        wo_summaries = [ForwardWoSummary(n, per_wo[n], per_wo_photos[n]) for n in nos]
        total_photos = sum(per_wo_photos.values())

        config_ready = get_settings().jira_forwarder_configured
        pat_ready = await self._pat_ready(acting_user)
        warnings: list[str] = []
        if not config_ready:
            warnings.append("jira forwarder not configured (base_url / project_key)")
        if not pat_ready:
            warnings.append(f"no active jira PAT for {acting_user} (or master key unset)")

        if dry_run:
            return ForwardResult(
                dry_run=True, external_key=None, work_orders=wo_summaries,
                total_comments=len(notes), total_photos=total_photos,
                summary=summary, description=description,
                pat_ready=pat_ready, config_ready=config_ready, warnings=warnings,
            )

        # ---- 執行 ----
        already = None
        if idempotency_key:
            already = await self.session.scalar(
                select(WorkOrderExternalLink).where(
                    WorkOrderExternalLink.link_type == "forwarded",
                    WorkOrderExternalLink.forward_idem_key == idempotency_key,
                    WorkOrderExternalLink.removed_at.is_(None),
                )
            )
        if already is not None:
            external_key = already.external_key
            already_forwarded = True
        else:
            pat = await self._get_pat_or_error(acting_user)  # 誠實 fail(不假成功)
            forwarder = self._forwarder_factory(pat)
            if forwarder is None:
                raise JiraSyncError("jira forwarder not configured (base_url / project_key)")
            external_key = await forwarder.create_mrq(
                summary=summary, body=description, idempotency_key=idempotency_key
            )
            wo_svc = WorkOrderService(self.session)
            for n in nos:  # 每工單記 forwarded link(冪等;帶 forward_idem_key 供重跑防重)
                await wo_svc.record_external_link(
                    work_order_no=n, external_key=external_key, link_type="forwarded",
                    actor=actor, on_behalf_of=f"human:{acting_user}", title=summary[:200],
                    forward_idem_key=idempotency_key,
                )
            already_forwarded = False

        # 排 outbox(occurred_at 序;冪等 on conflict do nothing)
        async with self.write(actor):
            for note in notes:
                await self.session.execute(
                    pg_insert(JiraOutbox)
                    .values(
                        note_id=note.id, work_order_no=note.work_order_no,
                        external_key=external_key, on_behalf_user=acting_user,
                        status="pending", created_by=actor.value, source_actor=actor.value,
                    )
                    .on_conflict_do_nothing(index_elements=["note_id", "external_key"])
                )
        # 立即 flush(comment 按全域時序送出)
        flush = await self.flush_outbox(actor=actor, limit=max(50, len(notes) + 5))
        return ForwardResult(
            dry_run=False, external_key=external_key, work_orders=wo_summaries,
            total_comments=len(notes), total_photos=total_photos,
            summary=summary, description=description,
            pat_ready=pat_ready, config_ready=config_ready,
            already_forwarded=already_forwarded, warnings=warnings, flush=flush,
        )

    async def _get_pat_or_error(self, user_id: str) -> str:
        try:
            pat = await self._get_pat(user_id)
        except VaultError as exc:
            raise JiraSyncError(f"cannot use jira PAT for {user_id}: {exc}") from exc
        if pat is None:
            raise JiraSyncError(f"no active jira PAT for {user_id}")
        return pat
